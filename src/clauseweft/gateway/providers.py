"""Upstream chat transports (one-shot and SSE streaming) for configured models."""

import http.client
import json
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit
from uuid import uuid4

from .config import Config, Endpoint
from .errors import GatewayError

_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")


def _bad_response():
    return GatewayError(502, "invalid_upstream_response", "上游未返回有效的文本对话结果")


def _check_utf8(value, message):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise GatewayError(400, "invalid_messages", message) from None


def validate_tools(tools, tool_choice=None):
    if tools is None:
        if tool_choice is not None:
            raise GatewayError(400, "invalid_request", "tool_choice 需要同时提供 tools")
        return None, None
    if not isinstance(tools, list) or not 1 <= len(tools) <= 64:
        raise GatewayError(400, "invalid_request", "tools 须包含 1 到 64 项工具定义")
    names = set()
    cleaned = []
    for item in tools:
        if not isinstance(item, dict) or item.get("type") != "function" or set(item) != {"type", "function"}:
            raise GatewayError(400, "invalid_request", "工具定义仅支持 function 类型")
        fn = item["function"]
        if not isinstance(fn, dict) or fn.keys() - {"name", "description", "parameters", "strict"}:
            raise GatewayError(400, "invalid_request", "工具函数定义包含不支持字段")
        name = fn.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name) or name in names:
            raise GatewayError(400, "invalid_request", "工具名称无效或重复")
        names.add(name)
        if "description" in fn and not isinstance(fn["description"], str):
            raise GatewayError(400, "invalid_request", "工具描述须为文本")
        if "parameters" in fn and not isinstance(fn["parameters"], dict):
            raise GatewayError(400, "invalid_request", "工具参数 schema 须为对象")
        if "strict" in fn and type(fn["strict"]) is not bool:
            raise GatewayError(400, "invalid_request", "strict 须为布尔值")
        cleaned.append({"type": "function", "function": dict(fn)})
    if tool_choice is not None:
        if isinstance(tool_choice, str):
            if tool_choice not in ("auto", "none", "required"):
                raise GatewayError(400, "invalid_request", "tool_choice 仅支持 auto、none、required 或指定函数")
        elif (isinstance(tool_choice, dict) and set(tool_choice) == {"type", "function"}
              and tool_choice.get("type") == "function"
              and isinstance(tool_choice.get("function"), dict)
              and set(tool_choice["function"]) == {"name"}
              and tool_choice["function"].get("name") in names):
            pass
        else:
            raise GatewayError(400, "invalid_request", "tool_choice 指定了无效工具")
    return cleaned, tool_choice


def validate_response_format(value):
    if value is None:
        return None
    if not isinstance(value, dict) or not isinstance(value.get("type"), str):
        raise GatewayError(400, "invalid_request", "response_format 须为含 type 的对象")
    kind = value["type"]
    if kind in ("text", "json_object"):
        if set(value) != {"type"}:
            raise GatewayError(400, "invalid_request", "response_format 包含多余字段")
        return {"type": kind}
    if kind == "json_schema":
        schema = value.get("json_schema")
        if (set(value) != {"type", "json_schema"} or not isinstance(schema, dict)
                or not isinstance(schema.get("name"), str) or not _TOOL_NAME.fullmatch(schema["name"])
                or not isinstance(schema.get("schema"), dict)):
            raise GatewayError(400, "invalid_request", "json_schema 须提供有效 name 和 schema 对象")
        if "strict" in schema and type(schema["strict"]) is not bool:
            raise GatewayError(400, "invalid_request", "json_schema.strict 须为布尔值")
        return {"type": "json_schema", "json_schema": dict(schema)}
    raise GatewayError(400, "invalid_request", "不支持的 response_format.type")


def validate_messages(messages, *, allow_tools=False):
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise GatewayError(400, "invalid_messages", "messages 须包含 1 到 256 条消息")
    for msg in messages:
        if not isinstance(msg, dict) or not isinstance(msg.get("role"), str):
            raise GatewayError(400, "invalid_messages", "消息格式无效")
        role = msg["role"]
        if role in ("system", "user"):
            if set(msg) != {"role", "content"} or not isinstance(msg["content"], str) or not msg["content"].strip():
                raise GatewayError(400, "invalid_messages", "消息应仅包含受支持的 role 与非空文本 content")
            _check_utf8(msg["content"], "消息包含无效的 Unicode 文本")
        elif role == "assistant":
            keys = set(msg)
            if keys == {"role", "content"}:
                if not isinstance(msg["content"], str) or not msg["content"].strip():
                    raise GatewayError(400, "invalid_messages", "消息应仅包含受支持的 role 与非空文本 content")
                _check_utf8(msg["content"], "消息包含无效的 Unicode 文本")
            elif allow_tools and keys <= {"role", "content", "tool_calls"} and "tool_calls" in keys:
                content = msg.get("content")
                if content is not None and not isinstance(content, str):
                    raise GatewayError(400, "invalid_messages", "assistant content 须为文本或 null")
                if isinstance(content, str):
                    _check_utf8(content, "消息包含无效的 Unicode 文本")
                calls = msg["tool_calls"]
                if not isinstance(calls, list) or not 1 <= len(calls) <= 32:
                    raise GatewayError(400, "invalid_messages", "tool_calls 须包含 1 到 32 项")
                for call in calls:
                    if (not isinstance(call, dict) or call.get("type") != "function"
                            or not isinstance(call.get("id"), str) or not call["id"].strip()
                            or not isinstance(call.get("function"), dict)
                            or not isinstance(call["function"].get("name"), str)
                            or not _TOOL_NAME.fullmatch(call["function"]["name"])
                            or not isinstance(call["function"].get("arguments"), str)):
                        raise GatewayError(400, "invalid_messages", "assistant tool_calls 格式无效")
            else:
                raise GatewayError(400, "invalid_messages", "消息仅支持受支持字段")
        elif role == "tool" and allow_tools:
            if (set(msg) != {"role", "tool_call_id", "content"}
                    or not isinstance(msg["tool_call_id"], str) or not msg["tool_call_id"].strip()
                    or not isinstance(msg["content"], str)):
                raise GatewayError(400, "invalid_messages", "tool 消息须包含 tool_call_id 与文本 content")
            _check_utf8(msg["content"], "消息包含无效的 Unicode 文本")
        else:
            raise GatewayError(400, "invalid_messages", "消息包含不支持的角色")


def _ollama_messages(messages):
    converted = []
    for msg in messages:
        if msg["role"] == "tool":
            converted.append({"role": "tool", "content": msg["content"]})
        elif msg["role"] == "assistant" and "tool_calls" in msg:
            calls = []
            for call in msg["tool_calls"]:
                raw_args = call["function"]["arguments"]
                try:
                    parsed = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except ValueError:
                    parsed = {}
                calls.append({"function": {"name": call["function"]["name"], "arguments": parsed}})
            converted.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        else:
            converted.append(msg)
    return converted


def _payload(endpoint, messages, max_tokens, request_id, json_decision=False, *,
             tools=None, tool_choice=None, response_format=None, stream=False):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", request_id):
        raise GatewayError(400, "invalid_request_id", "请求 ID 无效")
    if type(max_tokens) is not int or not 1 <= max_tokens <= endpoint.max_tokens:
        raise GatewayError(400, "invalid_max_tokens", "输出上限超出端点允许范围")
    validate_messages(messages, allow_tools=bool(tools))
    tools, tool_choice = validate_tools(tools, tool_choice)
    response_format = validate_response_format(response_format)
    body = {
        "model": endpoint.model,
        "messages": _ollama_messages(messages) if endpoint.protocol == "ollama" else messages,
        "stream": bool(stream),
    }
    if endpoint.protocol == "ollama":
        body["options"] = {"num_predict": max_tokens}
        if response_format is not None:
            if response_format["type"] == "json_object":
                body["format"] = "json"
            elif response_format["type"] == "json_schema":
                body["format"] = response_format["json_schema"]["schema"]
        elif json_decision:
            body["format"] = "json"
            body["options"]["temperature"] = 0
        if tools is not None:
            body["tools"] = tools
    else:
        body["max_tokens"] = max_tokens
        if stream:
            body["stream_options"] = {"include_usage": True}
        if tools is not None:
            body["tools"] = tools
            if tool_choice is not None:
                body["tool_choice"] = tool_choice
        if response_format is not None:
            body["response_format"] = response_format
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        raise GatewayError(400, "invalid_messages", "消息包含无效的 Unicode 字符") from None


def _headers(endpoint, request_id, *, stream=False):
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream and endpoint.protocol != "ollama" else "application/json",
        "X-Request-ID": request_id, "Connection": "close",
    }
    if endpoint.api_key_env:
        key = os.environ.get(endpoint.api_key_env, "")
        if not key or len(key) > 8192 or not all(33 <= ord(c) <= 126 for c in key):
            raise GatewayError(503, "upstream_credentials_unavailable", "上游凭据未就绪")
        headers["Authorization"] = f"Bearer {key}"
    return headers


def _check_status(status):
    if status == 429:
        raise GatewayError(429, "upstream_rate_limited", "上游请求达到限额")
    if status in (408, 504):
        raise GatewayError(504, "upstream_timeout", "上游请求超时")
    if status in (401, 403):
        raise GatewayError(502, "upstream_authentication_failed", "上游认证失败")
    if not 200 <= status < 300:
        raise GatewayError(502, "upstream_error", "上游未成功完成请求")


def _read_response(response, limit):
    _check_status(response.status)
    length = response.getheader("Content-Length")
    if length is not None:
        if not re.fullmatch(r"[0-9]{1,20}", length):
            raise _bad_response()
        length = int(length)
        if length > limit:
            raise GatewayError(502, "upstream_response_too_large", "上游响应超过配置大小")
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise GatewayError(502, "upstream_response_too_large", "上游响应超过配置大小")
    if length is not None and len(raw) != length:
        raise _bad_response()
    try:
        result = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise _bad_response() from None
    if not isinstance(result, dict) or "error" in result:
        raise _bad_response()
    request_id = response.getheader("X-Request-ID") or response.getheader("Request-ID")
    return result, request_id


def _post(config, endpoint, body, headers):
    url = urlsplit(endpoint.base_url)
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=config.timeout_seconds)
    suffix = "/api/chat" if endpoint.protocol == "ollama" else "/chat/completions"
    deadline = time.monotonic() + config.timeout_seconds
    timer = None
    expired = threading.Event()
    try:
        connection.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        transport = connection.sock

        def interrupt():
            expired.set()
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        connection.request("POST", url.path + suffix, body, headers)
        with connection.getresponse() as response:
            return _read_response(response, config.max_response_bytes)
    except TimeoutError:
        raise GatewayError(504, "upstream_timeout", "上游请求超时") from None
    except (http.client.HTTPException, OSError, UnicodeError):
        raise GatewayError(502, "upstream_unavailable", "无法获得完整的上游响应") from None
    finally:
        if timer is not None:
            timer.cancel()
        connection.close()
        if expired.is_set():
            raise GatewayError(504, "upstream_timeout", "上游请求超时") from None


def _usage(prompt, completion, total, *, cached=None, reasoning=None):
    # Some providers omit usage. Unknown usage must not appear as zero.
    if any(type(n) is not int or n < 0 for n in (prompt, completion, total)):
        return None
    if total != prompt + completion:
        return None
    result = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}
    if type(cached) is int and 0 <= cached <= prompt:
        result["cached_tokens"] = cached
    if type(reasoning) is int and 0 <= reasoning <= completion:
        result["reasoning_tokens"] = reasoning
    return result


def _extract_usage(counts):
    if not isinstance(counts, dict):
        return None
    prompt_details = counts.get("prompt_tokens_details")
    comp_details = counts.get("completion_tokens_details")
    cached = counts.get("cached_tokens")
    if cached is None and isinstance(prompt_details, dict):
        cached = prompt_details.get("cached_tokens")
    reasoning = counts.get("reasoning_tokens")
    if reasoning is None and isinstance(comp_details, dict):
        reasoning = comp_details.get("reasoning_tokens")
    return _usage(counts.get("prompt_tokens"), counts.get("completion_tokens"), counts.get("total_tokens"),
                  cached=cached, reasoning=reasoning)


def _normalize_tool_calls(raw_calls, *, is_ollama=False):
    if not isinstance(raw_calls, list) or not 1 <= len(raw_calls) <= 32:
        raise _bad_response()
    normalized = []
    for index, call in enumerate(raw_calls):
        if not isinstance(call, dict) or not isinstance(call.get("function"), dict):
            raise _bad_response()
        fn = call["function"]
        name = fn.get("name")
        if not isinstance(name, str) or not _TOOL_NAME.fullmatch(name):
            raise _bad_response()
        raw_args = fn.get("arguments")
        if is_ollama and isinstance(raw_args, dict):
            args_text = json.dumps(raw_args, ensure_ascii=False, separators=(",", ":"))
        elif isinstance(raw_args, str):
            args_text = raw_args
        else:
            raise _bad_response()
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id.strip():
            call_id = f"call_{uuid4().hex[:16]}_{index}"
        normalized.append({
            "id": call_id,
            "type": "function",
            "function": {"name": name, "arguments": args_text},
        })
    return normalized


def _completion(endpoint: Endpoint, response: dict, request_id: str, upstream_request_id: str | None,
                *, allow_tool_calls: bool = False):
    if endpoint.protocol == "ollama":
        if response.get("done") is not True:
            raise _bad_response()
        message = response.get("message")
        reason = response.get("done_reason") or "stop"
        prompt = response.get("prompt_eval_count")
        completion = response.get("eval_count")
        total = prompt + completion if type(prompt) is int and type(completion) is int else None
        usage = _usage(prompt, completion, total)
    else:
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _bad_response()
        message = choices[0].get("message")
        reason = choices[0].get("finish_reason")
        usage = _extract_usage(response.get("usage"))
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _bad_response()
    raw_calls = message.get("tool_calls")
    has_calls = bool(raw_calls)
    if has_calls and not allow_tool_calls:
        raise _bad_response()
    if has_calls:
        tool_calls = _normalize_tool_calls(raw_calls, is_ollama=endpoint.protocol == "ollama")
        if endpoint.protocol == "ollama" and reason == "stop":
            reason = "tool_calls"
    else:
        tool_calls = None
        if "content" not in message:
            raise _bad_response()
    valid_reasons = ("stop", "length", "content_filter", "tool_calls") if has_calls else ("stop", "length", "content_filter")
    if reason not in valid_reasons:
        raise _bad_response()
    content = message.get("content")
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise _bad_response()
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad_response() from None
    out_message = {"role": "assistant", "content": content}
    if tool_calls is not None:
        out_message["tool_calls"] = tool_calls
    result = {
        "id": f"chatcmpl-{request_id}", "object": "chat.completion", "created": int(time.time()),
        "model": endpoint.id,
        "choices": [{"index": 0, "message": out_message, "finish_reason": reason}],
    }
    if usage is not None:
        result["usage"] = usage
    identifiers = {
        "upstream_model": response.get("model"),
        "upstream_request_id": upstream_request_id,
        "upstream_completion_id": response.get("id") if endpoint.protocol == "chat_completions" else None,
    }
    metadata = {
        key: value for key, value in identifiers.items()
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_./:@+-]{0,255}", value)
    }
    if metadata:
        result["gateway_metadata"] = metadata
    return result


def invoke(config: Config, endpoint_id: str, messages: list, max_tokens: int, request_id: str,
           *, json_decision: bool = False, tools=None, tool_choice=None, response_format=None) -> dict:
    """Use one configured endpoint for one completion; never retry or switch."""
    endpoint = config.endpoints.get(endpoint_id) if isinstance(endpoint_id, str) else None
    if endpoint is None:
        raise GatewayError(404, "model_not_found", "模型别名未配置")
    body = _payload(endpoint, messages, max_tokens, request_id, json_decision,
                    tools=tools, tool_choice=tool_choice, response_format=response_format, stream=False)
    if len(body) > config.max_request_bytes:
        raise GatewayError(413, "request_too_large", "上游请求超过配置大小")
    response, upstream_id = _post(config, endpoint, body, _headers(endpoint, request_id))
    return _completion(endpoint, response, request_id, upstream_id, allow_tool_calls=bool(tools))


def invoke_stream(config: Config, endpoint_id: str, messages: list, max_tokens: int, request_id: str,
                  *, json_decision: bool = False, tools=None, tool_choice=None, response_format=None):
    """Stream normalized OpenAI SSE chunks from one configured endpoint."""
    endpoint = config.endpoints.get(endpoint_id) if isinstance(endpoint_id, str) else None
    if endpoint is None:
        raise GatewayError(404, "model_not_found", "模型别名未配置")
    body = _payload(endpoint, messages, max_tokens, request_id, json_decision,
                    tools=tools, tool_choice=tool_choice, response_format=response_format, stream=True)
    if len(body) > config.max_request_bytes:
        raise GatewayError(413, "request_too_large", "上游请求超过配置大小")
    url = urlsplit(endpoint.base_url)
    connection_type = http.client.HTTPSConnection if url.scheme == "https" else http.client.HTTPConnection
    connection = connection_type(url.hostname, url.port, timeout=config.timeout_seconds)
    suffix = "/api/chat" if endpoint.protocol == "ollama" else "/chat/completions"
    deadline = time.monotonic() + config.timeout_seconds
    expired = threading.Event()
    timer = None
    try:
        connection.connect()
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        transport = connection.sock

        def interrupt():
            expired.set()
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass

        timer = threading.Timer(remaining, interrupt)
        timer.daemon = True
        timer.start()
        connection.request("POST", url.path + suffix, body, _headers(endpoint, request_id, stream=True))
        response = connection.getresponse()
        _check_status(response.status)
    except GatewayError:
        if timer is not None:
            timer.cancel()
        connection.close()
        raise
    except TimeoutError:
        if timer is not None:
            timer.cancel()
        connection.close()
        raise GatewayError(504, "upstream_timeout", "上游请求超时") from None
    except (http.client.HTTPException, OSError, UnicodeError):
        if timer is not None:
            timer.cancel()
        connection.close()
        raise GatewayError(502, "upstream_unavailable", "无法获得完整的上游响应") from None

    def generator():
        created = int(time.time())
        chunk_id = f"chatcmpl-{request_id}"
        seen_bytes = 0
        try:
            with response:
                while True:
                    line = response.readline(65536)
                    if not line:
                        break
                    seen_bytes += len(line)
                    if seen_bytes > config.max_response_bytes:
                        break
                    text = line.decode("utf-8", errors="replace").strip()
                    if not text:
                        continue
                    if endpoint.protocol == "ollama":
                        item = json.loads(text)
                        if not isinstance(item, dict) or "error" in item:
                            break
                        msg = item.get("message") or {}
                        delta = {}
                        if isinstance(msg.get("content"), str) and msg["content"]:
                            delta["content"] = msg["content"]
                        if tools and msg.get("tool_calls"):
                            delta["tool_calls"] = _normalize_tool_calls(msg["tool_calls"], is_ollama=True)
                        done = item.get("done") is True
                        reason = None
                        usage = None
                        if done:
                            reason = item.get("done_reason") or ("tool_calls" if "tool_calls" in delta else "stop")
                            prompt = item.get("prompt_eval_count")
                            completion = item.get("eval_count")
                            total = prompt + completion if type(prompt) is int and type(completion) is int else None
                            usage = _usage(prompt, completion, total)
                        if delta or done:
                            out = {
                                "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                "model": endpoint.id,
                                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                            }
                            if usage is not None:
                                out["usage"] = usage
                            yield out
                        if done:
                            break
                    else:
                        if not text.startswith("data:"):
                            continue
                        payload = text[5:].strip()
                        if payload == "[DONE]":
                            break
                        item = json.loads(payload)
                        if not isinstance(item, dict) or "error" in item:
                            break
                        choices = item.get("choices") or []
                        out_choices = []
                        if choices and isinstance(choices[0], dict):
                            raw_delta = choices[0].get("delta") or {}
                            delta = {}
                            if isinstance(raw_delta.get("role"), str):
                                delta["role"] = raw_delta["role"]
                            if isinstance(raw_delta.get("content"), str):
                                delta["content"] = raw_delta["content"]
                            if tools and isinstance(raw_delta.get("tool_calls"), list):
                                delta["tool_calls"] = raw_delta["tool_calls"]
                            out_choices.append({
                                "index": 0, "delta": delta,
                                "finish_reason": choices[0].get("finish_reason"),
                            })
                        usage = _extract_usage(item.get("usage"))
                        if out_choices or usage is not None:
                            out = {
                                "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                "model": endpoint.id, "choices": out_choices,
                            }
                            if usage is not None:
                                out["usage"] = usage
                            yield out
        except (ValueError, OSError, http.client.HTTPException, GatewayError):
            return
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()

    return generator()
