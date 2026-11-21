"""Run provider-owned coding CLIs inside a bounded, single-use local container."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import re
import queue
import shutil
import stat
import subprocess
import tempfile
import threading
import time
import unicodedata
from uuid import uuid4

from clauseweft.workspace import portable_path
from clauseweft.workspace.credentials import contains
from .config import AgentRuntimeError, Runner, load_runners
from .container import SCRIPT, review_gate
from .protocol import Response

__all__ = ["AgentRuntimeError", "Runner", "load_runners", "run_agent"]

_MAX_TREE = 2 * 1024 * 1024


def _bad_tree():
    return AgentRuntimeError(
        "agent_files", "工作副本含有受限文件、路径冲突或超过容量限制"
    )


def _path(name):
    if not portable_path(name):
        raise _bad_tree()
    return unicodedata.normalize("NFD", name).casefold()


def _validate_tree(files, key):
    if not isinstance(files, dict) or len(files) > 512:
        raise _bad_tree()
    size = 0
    seen = {}
    contents = {}
    for name, encoded in files.items():
        _path(name)
        if key in name or contains(name.encode()):
            raise _bad_tree()
        prefix = ""
        for part in name.split("/"):
            prefix = prefix + "/" + part if prefix else part
            entry = (prefix, prefix == name)
            if seen.setdefault(_path(prefix), entry) != entry:
                raise _bad_tree()
        if not isinstance(encoded, str) or len(encoded) > (_MAX_TREE * 4 // 3) + 4:
            raise _bad_tree()
        try:
            raw = base64.b64decode(encoded, validate=True)
            raw.decode("utf-8")
        except (ValueError, UnicodeError):
            raise _bad_tree() from None
        size += len(raw)
        if size > _MAX_TREE or b"\0" in raw or key.encode() in raw or contains(raw):
            raise _bad_tree()
        contents[name] = raw
    return contents


def _read_tree(workspace, key):
    if workspace.is_symlink() or not workspace.is_dir():
        raise _bad_tree()
    files = {}
    size = count = 0
    for path in workspace.rglob("*"):
        count += 1
        if count > 2048:
            raise _bad_tree()
        item = path.lstat()
        _path(str(path.relative_to(workspace)))
        if stat.S_ISDIR(item.st_mode):
            continue
        size += item.st_size
        if (
            len(files) >= 512
            or not stat.S_ISREG(item.st_mode)
            or item.st_nlink != 1
            or size > _MAX_TREE
        ):
            raise _bad_tree()
        files[path.relative_to(workspace).as_posix()] = base64.b64encode(
            path.read_bytes()
        ).decode("ascii")
    _validate_tree(files, key)
    return files


def _store_tree(workspace, before, after):
    # Only the job owner can access the private staging directory. The
    # container has no host mounts and is already gone when this is called.
    for path in sorted(workspace.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir():
            if not any(path.iterdir()):
                path.rmdir()
        elif path.relative_to(workspace).as_posix() not in after:
            path.unlink()
    for name, raw in after.items():
        if before.get(name) == base64.b64encode(raw).decode("ascii"):
            continue
        path = workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)


def _cli(runner):
    if runner.kind == "claude":
        tools = "Read,Glob,Grep,Edit,Write"
        return [
            runner.executable,
            "--bare",
            "--restricted",
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--tools",
            tools,
            "--allowedTools",
            tools,
            "--permission-mode",
            "dontAsk",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
        ]
    return [
        runner.executable,
        "--profile",
        "headless",
        "--patch",
        "/tmp/review.yml",
        "--json",
        "--permission-mode",
        "read-only",
        "--approval-policy",
        "never",
        "-",
    ]


def _docker_context(runner):
    docker = shutil.which("docker")
    if not docker:
        raise AgentRuntimeError(
            "needs_configuration", "需要本机 Docker 和已登记的固定镜像"
        )
    # Only the client needs its host Docker context. Payload and provider key
    # never enter this process environment or its command line.
    env = {
        k: os.environ[k]
        for k in (
            "PATH",
            "HOME",
            "DOCKER_HOST",
            "DOCKER_CONTEXT",
            "DOCKER_CONFIG",
            "SystemRoot",
        )
        if k in os.environ
    }
    endpoint = env.get("DOCKER_HOST")
    if not endpoint:
        probe = subprocess.run(
            [docker, "context", "inspect", "--format", "{{.Endpoints.docker.Host}}"],
            env=env,
            capture_output=True,
            timeout=5,
        )
        endpoint = probe.stdout.decode().strip()
    if not endpoint.startswith(("unix:///", "npipe:////./pipe/")) or "\n" in endpoint:
        raise AgentRuntimeError("needs_configuration", "执行器需要本机 Docker 套接字")
    env.pop("DOCKER_HOST", None)
    env.pop("DOCKER_CONTEXT", None)
    client = [docker, "--host", endpoint]
    probe = subprocess.run(
        [
            *client,
            "image",
            "inspect",
            "--format",
            "{{.Id}} {{json .Config.Volumes}}",
            runner.image,
        ],
        env=env,
        capture_output=True,
        timeout=5,
    )
    if probe.returncode or probe.stdout.decode().strip() not in (
        runner.image + " null",
        runner.image + " {}",
    ):
        raise AgentRuntimeError(
            "needs_configuration", "本机缺少固定镜像或镜像声明了数据卷"
        )
    return client, env


def _output_chunks(stream, deadline, cancelled):
    # select() cannot wait on subprocess pipes on Windows. A bounded queue
    # lets the controller check cancellation even when the CLI stays silent.
    chunks = queue.Queue(maxsize=4)
    stop = threading.Event()
    fd = os.dup(stream.fileno())

    def read_pipe():
        with os.fdopen(fd, "rb", buffering=0) as pipe:
            while not stop.is_set():
                try:
                    block = pipe.read(65536)
                except OSError:
                    block = None
                while not stop.is_set():
                    try:
                        chunks.put(block, timeout=0.1)
                        break
                    except queue.Full:
                        continue
                if not block:
                    break

    reader = threading.Thread(target=read_pipe, daemon=True)
    reader.start()
    next_check = 0
    try:
        while True:
            now = time.monotonic()
            if now >= deadline:
                raise AgentRuntimeError("agent_timeout", "Agent 超过本次时限")
            if now >= next_check:
                if cancelled():
                    raise AgentRuntimeError("cancelled", "已停止 Agent 任务")
                next_check = now + 0.2
            try:
                chunk = chunks.get(timeout=0.2)
            except queue.Empty:
                continue
            if chunk is None:
                raise AgentRuntimeError("agent_protocol", "读取容器回执失败")
            if not chunk:
                break
            yield chunk
    finally:
        stop.set()


def _stream(proc, deadline, response, cancelled, key, editable):
    buffer = bytearray()
    total = 0
    files = None
    for chunk in _output_chunks(proc.stdout, deadline, cancelled):
        total += len(chunk)
        if total > 24 * 1024 * 1024:
            raise AgentRuntimeError("agent_output_limit", "Agent 总输出超过限制")
        buffer.extend(chunk)
        start = 0
        while (end := buffer.find(b"\n", start)) != -1:
            channel, value = json.loads(buffer[start:end])
            if files is not None:
                raise AgentRuntimeError("agent_protocol", "结束后收到额外回执")
            if channel == "cli":
                response.consume(value)
            elif channel == "files" and editable:
                files = _validate_tree(value, key)
            else:
                raise AgentRuntimeError("agent_protocol", "无效的容器回执")
            start = end + 1
        del buffer[:start]
        if len(buffer) > 4 * 1024 * 1024:
            raise AgentRuntimeError("agent_output_limit", "Agent 单条输出超过限制")
    if buffer:
        raise AgentRuntimeError("agent_protocol", "容器输出未完整结束")
    return files


def run_agent(
    runner, workspace, prompt, *, on_event=lambda event: None, cancelled=lambda: False
):
    if not isinstance(runner, Runner):
        raise ValueError("需要已登记的执行器")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or len(prompt.encode()) > 65536
    ):
        raise ValueError("Agent 任务说明须为 1 至 65536 字节文本")
    if cancelled():
        raise AgentRuntimeError("cancelled", "已停止 Agent 任务")
    key = os.environ.get(runner.token_env)
    if not key or not key.isascii() or not re.fullmatch(r"[!-~]{1,8192}", key):
        raise AgentRuntimeError(
            "needs_configuration", "请在工作台进程环境配置执行器凭据"
        )
    workspace = Path(workspace)
    before = _read_tree(workspace, key)
    name = "clauseweft-agent-" + uuid4().hex
    editable = runner.kind == "claude"
    response = Response(runner.kind, key, on_event)
    try:
        client, env = _docker_context(runner)
        cmd = [
            *client,
            "run",
            "--rm",
            "--name",
            name,
            "--pull=never",
            "--read-only",
            "--cap-drop=ALL",
            "--security-opt=no-new-privileges",
            "--user=65534:65534",
            "--network=bridge",
            "--pids-limit=96",
            "--cpus=1",
            "--memory=1g",
            "--memory-swap=1g",
            "--log-driver=none",
            "--tmpfs",
            "/tmp:rw,nosuid,nodev,noexec,size=128m,mode=1777",
            "--workdir=/work",
            "-i",
            "--entrypoint=/usr/local/bin/python3",
        ]
        if editable:
            cmd.extend(["--tmpfs", "/work:rw,nosuid,nodev,noexec,size=32m,mode=1777"])
        cmd.extend(
            [runner.image, "-I", "-u", "-c", SCRIPT, str(runner.timeout_seconds)]
        )
        # A seekable, unlinked file avoids a blocking stdin write on a full
        # pipe (notably while a container is still starting).
        with tempfile.TemporaryFile() as packet:
            packet.write(
                json.dumps(
                    {
                        "kind": runner.kind,
                        "argv": _cli(runner),
                        "review_gate": review_gate() if not editable else None,
                        "key": key,
                        "prompt": prompt,
                        "files": before,
                    }
                ).encode()
            )
            packet.seek(0)
            if cancelled():
                raise AgentRuntimeError("cancelled", "已停止 Agent 任务")
            proc = subprocess.Popen(
                cmd,
                env=env,
                stdin=packet,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            try:
                deadline = time.monotonic() + runner.timeout_seconds
                after = _stream(proc, deadline, response, cancelled, key, editable)
                code = proc.wait(timeout=max(0.01, deadline - time.monotonic()))
                if code:
                    raise AgentRuntimeError(
                        "agent_timeout" if code == 124 else "agent_failed",
                        "执行器退出，未取得完整结果",
                    )
                result = response.result()
                if editable and after is None:
                    raise AgentRuntimeError("agent_protocol", "缺少工作副本回执")
            finally:
                proc.stdout.close()
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)
                if proc.returncode != 0:
                    subprocess.run(
                        [*client, "rm", "-f", "-v", name],
                        env=env,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
                    )
        if cancelled():
            raise AgentRuntimeError("cancelled", "已停止 Agent 任务")
        if editable:
            _store_tree(workspace, before, after)
        return result
    except subprocess.TimeoutExpired:
        raise AgentRuntimeError("agent_timeout", "执行器操作超时") from None
    except (OSError, ValueError, TypeError, AttributeError, RecursionError):
        raise AgentRuntimeError(
            "agent_protocol", "执行器、工作副本或事件格式无效"
        ) from None
