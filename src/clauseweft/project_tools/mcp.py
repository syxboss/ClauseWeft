"""Controlled MCP (Model Context Protocol) JSON-RPC client over stdio and HTTP."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit
from uuid import uuid4

from clauseweft import __version__
from clauseweft.storage import canonical
from clauseweft.workspace.credentials import redact

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_RISKS = ("read_only", "mutating", "external_effect")
_MAX_RESPONSE = 1024 * 1024


def is_mcp_tool(tool_id):
    return isinstance(tool_id, str) and tool_id.startswith("mcp:") and "/" in tool_id[4:]


def parse_mcp_tool(tool_id):
    if not is_mcp_tool(tool_id):
        raise ValueError("无效的 MCP 工具标识")
    server_id, _, name = tool_id[4:].partition("/")
    if not _ID.fullmatch(server_id) or not _ID.fullmatch(name):
        raise ValueError("无效的 MCP 服务或工具名称")
    return server_id, name


def load_mcp_servers(config):
    """Validate deployment-owned MCP server registrations; no network/process IO."""
    if not isinstance(config, dict) or len(config) > 32:
        raise ValueError("mcp_servers 须为最多 32 项的映射")
    servers = {}
    for server_id, spec in config.items():
        if not isinstance(server_id, str) or not _ID.fullmatch(server_id):
            raise ValueError("MCP 服务 ID 无效")
        if not isinstance(spec, dict):
            raise ValueError("MCP 服务配置须为对象")
        allowed = {"transport", "command", "url", "token_env", "data_scope", "timeout_seconds", "tools"}
        if spec.keys() - allowed:
            raise ValueError("MCP 服务配置包含不支持字段")
        transport = spec.get("transport")
        if transport not in ("stdio", "http"):
            raise ValueError("MCP transport 须为 stdio 或 http")
        scope = spec.get("data_scope", "local_only")
        if scope not in ("local_only", "public"):
            raise ValueError("MCP data_scope 须为 local_only 或 public")
        timeout = spec.get("timeout_seconds", 15)
        if type(timeout) is not int or not 1 <= timeout <= 60:
            raise ValueError("MCP timeout_seconds 须为 1 至 60 秒")
        token_env = spec.get("token_env")
        if token_env is not None and (
            not isinstance(token_env, str) or not _ENV.fullmatch(token_env)
            or token_env.startswith(("CLAUSEWEFT_", "DOCKER_"))
        ):
            raise ValueError("MCP token_env 无效")
        entry = {
            "id": server_id,
            "transport": transport,
            "data_scope": scope,
            "timeout_seconds": timeout,
            "token_env": token_env,
        }
        if transport == "stdio":
            command = spec.get("command")
            if (not isinstance(command, list) or not 1 <= len(command) <= 32
                    or any(not isinstance(a, str) or not a or "\0" in a or len(a) > 2048 for a in command)):
                raise ValueError("MCP stdio 须提供有效的 command 数组")
            if "url" in spec:
                raise ValueError("MCP stdio 不能同时配置 url")
            entry["command"] = list(command)
        else:
            url = spec.get("url")
            if not isinstance(url, str) or not url.isascii() or len(url) > 2048:
                raise ValueError("MCP http 须提供有效的 url")
            parsed = urlsplit(url)
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("MCP url 须为无内嵌凭据的 HTTP(S) 地址")
            if parsed.scheme == "http" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
                raise ValueError("非回环 MCP HTTP 服务须使用 HTTPS")
            if "command" in spec:
                raise ValueError("MCP http 不能同时配置 command")
            entry["url"] = url
        raw_tools = spec.get("tools")
        if not isinstance(raw_tools, dict) or not 1 <= len(raw_tools) <= 32:
            raise ValueError("MCP 服务须显式声明 1 至 32 个工具及风险等级")
        tools = {}
        for name, meta in raw_tools.items():
            if not isinstance(name, str) or not _ID.fullmatch(name) or not isinstance(meta, dict):
                raise ValueError("MCP 工具名称或配置无效")
            if meta.keys() - {"description", "risk", "parameters"}:
                raise ValueError("MCP 工具定义包含不支持字段")
            risk = meta.get("risk")
            if risk not in _RISKS:
                raise ValueError("MCP 工具须显式声明 risk（read_only / mutating / external_effect）")
            desc = meta.get("description", name)
            if not isinstance(desc, str) or not 1 <= len(desc.strip()) <= 400:
                raise ValueError("MCP 工具说明须为 1 至 400 字文本")
            params = meta.get("parameters")
            if params is not None and not isinstance(params, dict):
                raise ValueError("MCP 工具 parameters 须为 JSON Schema 对象")
            full_id = f"mcp:{server_id}/{name}"
            tools[name] = {
                "id": full_id,
                "server_id": server_id,
                "name": name,
                "description": desc.strip(),
                "risk": risk,
                "parameters": params or {"type": "object"},
                "data_scope": scope,
            }
        entry["tools"] = tools
        entry["digest"] = hashlib.sha256(canonical(entry).encode("utf-8")).hexdigest()
        servers[server_id] = entry
    return servers


def _rpc_result(payload, expected_id):
    if not isinstance(payload, dict) or payload.get("jsonrpc") != "2.0":
        raise RuntimeError("MCP 返回了无效的 JSON-RPC 响应")
    if payload.get("id") != expected_id:
        raise RuntimeError("MCP 响应 ID 不匹配")
    if "error" in payload and payload["error"] is not None:
        err = payload["error"]
        msg = err.get("message", "MCP 工具调用失败") if isinstance(err, dict) else str(err)
        raise RuntimeError(redact(str(msg))[:400])
    result = payload.get("result")
    if not isinstance(result, dict):
        raise RuntimeError("MCP 工具结果须为对象")
    return result


def _call_stdio(server, tool_name, arguments):
    req_id = uuid4().hex
    init_id = "init-" + req_id[:8]
    messages = [
        {"jsonrpc": "2.0", "id": init_id, "method": "initialize",
         "params": {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "clauseweft", "version": __version__}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": req_id, "method": "tools/call",
         "params": {"name": tool_name, "arguments": arguments}},
    ]
    wire = ("\n".join(json.dumps(m, ensure_ascii=False, separators=(",", ":")) for m in messages) + "\n").encode("utf-8")
    env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG", "SystemRoot") if k in os.environ}
    if server.get("token_env"):
        token = os.environ.get(server["token_env"], "")
        if not token:
            raise RuntimeError("MCP 服务凭据环境变量未设置")
        env["MCP_AUTH_TOKEN"] = token
    proc = subprocess.Popen(
        server["command"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=env, cwd=str(Path.home()),
    )
    try:
        stdout, _ = proc.communicate(input=wire, timeout=server["timeout_seconds"])
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        raise RuntimeError("MCP stdio 工具调用超时") from None
    if len(stdout) > _MAX_RESPONSE:
        raise RuntimeError("MCP 响应超过大小上限")
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict) and parsed.get("id") == req_id:
            return _rpc_result(parsed, req_id)
    raise RuntimeError("MCP stdio 未返回目标工具回执")


def _call_http(server, tool_name, arguments):
    req_id = uuid4().hex
    parsed = urlsplit(server["url"])
    conn_cls = http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(parsed.hostname, parsed.port, timeout=server["timeout_seconds"])
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Connection": "close",
    }
    if server.get("token_env"):
        token = os.environ.get(server["token_env"], "")
        if not token:
            raise RuntimeError("MCP 服务凭据环境变量未设置")
        headers["Authorization"] = f"Bearer {token}"
    body = json.dumps({
        "jsonrpc": "2.0", "id": req_id, "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    path = parsed.path or "/"
    if parsed.query:
        path += "?" + parsed.query
    try:
        conn.request("POST", path, body, headers)
        with conn.getresponse() as resp:
            if not 200 <= resp.status < 300:
                raise RuntimeError(f"MCP HTTP 返回状态 {resp.status}")
            raw = resp.read(_MAX_RESPONSE + 1)
            if len(raw) > _MAX_RESPONSE:
                raise RuntimeError("MCP HTTP 响应超过大小上限")
            payload = json.loads(raw.decode("utf-8"))
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise RuntimeError("MCP HTTP 调用失败") from exc
    finally:
        conn.close()
    return _rpc_result(payload, req_id)


def call_mcp_tool(server, tool_name, arguments):
    """Execute one registered MCP tool and return a redacted structured receipt."""
    if tool_name not in server["tools"]:
        raise ValueError("MCP 工具未登记")
    raw_result = (
        _call_stdio(server, tool_name, arguments)
        if server["transport"] == "stdio"
        else _call_http(server, tool_name, arguments)
    )
    sanitized_text = redact(json.dumps(raw_result, ensure_ascii=False))
    if len(sanitized_text.encode("utf-8")) > 131072:
        raise RuntimeError("MCP 工具输出超过 128 KiB 上限")
    cleaned = json.loads(sanitized_text)
    return {
        "server": server["id"],
        "tool": tool_name,
        "is_error": bool(cleaned.get("isError", False)),
        "content": cleaned.get("content", cleaned),
    }
