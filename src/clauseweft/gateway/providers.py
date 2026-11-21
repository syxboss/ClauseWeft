"""One-shot upstream chat transports for trusted, explicitly configured models."""

import http.client
import json
import os
import re
import socket
import threading
import time
from urllib.parse import urlsplit

from .config import Config, Endpoint
from .errors import GatewayError


def _bad_response():
    return GatewayError(502, "invalid_upstream_response", "上游未返回有效的文本对话结果")


def _payload(endpoint, messages, max_tokens, request_id, json_decision=False):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", request_id):
        raise GatewayError(400, "invalid_request_id", "请求 ID 无效")
    if type(max_tokens) is not int or not 1 <= max_tokens <= endpoint.max_tokens:
        raise GatewayError(400, "invalid_max_tokens", "输出上限超出端点允许范围")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 256:
        raise GatewayError(400, "invalid_messages", "messages 须包含 1 到 256 条纯文本消息")
    for msg in messages:
        if (not isinstance(msg, dict) or msg.keys() != {"role", "content"}
                or msg["role"] not in ("user", "assistant", "system")
                or not isinstance(msg["content"], str)):
            raise GatewayError(400, "invalid_messages", "消息仅支持角色与纯文本内容")
    body = {"model": endpoint.model, "messages": messages, "stream": False}
    if endpoint.protocol == "ollama":
        body["options"] = {"num_predict": max_tokens}
        if json_decision:
            body["format"] = "json"
            body["options"]["temperature"] = 0
    else:
        body["max_tokens"] = max_tokens
    try:
        return json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except UnicodeEncodeError:
        raise GatewayError(400, "invalid_messages", "消息包含无效的 Unicode 字符") from None


def _headers(endpoint, request_id):
    headers = {
        "Content-Type": "application/json", "Accept": "application/json",
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
    # Reading at most limit + 1 works even for chunked responses with no length.
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
    # http.client connects directly: no proxy, redirects, cookies or retries.
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
        # Also bound a peer that trickles response headers/body just before
        # each socket timeout. Capture the socket: getresponse may detach it.
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


def _usage(prompt, completion, total):
    # Some providers omit usage. Unknown usage must not appear as zero.
    if any(type(n) is not int or n < 0 for n in (prompt, completion, total)):
        return None
    if total != prompt + completion:
        return None
    return {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": total}


def _completion(endpoint: Endpoint, response: dict, request_id: str, upstream_request_id: str | None):
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
        counts = response.get("usage")
        usage = None
        if isinstance(counts, dict):
            usage = _usage(counts.get("prompt_tokens"), counts.get("completion_tokens"), counts.get("total_tokens"))
    if (not isinstance(message, dict) or message.get("role") != "assistant"
            or "content" not in message or message.get("tool_calls")
            or reason not in ("stop", "length", "content_filter")):
        raise _bad_response()
    content = message["content"]
    if content is None:
        content = ""
    if not isinstance(content, str):
        raise _bad_response()
    try:
        content.encode("utf-8")
    except UnicodeEncodeError:
        raise _bad_response() from None
    result = {
        "id": f"chatcmpl-{request_id}", "object": "chat.completion", "created": int(time.time()),
        "model": endpoint.id,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content},
                     "finish_reason": reason}],
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
           *, json_decision: bool = False) -> dict:
    """Use one configured endpoint for one completion; never retry or switch."""
    endpoint = config.endpoints.get(endpoint_id) if isinstance(endpoint_id, str) else None
    if endpoint is None:
        raise GatewayError(404, "model_not_found", "模型别名未配置")
    body = _payload(endpoint, messages, max_tokens, request_id, json_decision)
    if len(body) > config.max_request_bytes:
        raise GatewayError(413, "request_too_large", "上游请求超过配置大小")
    response, upstream_id = _post(config, endpoint, body, _headers(endpoint, request_id))
    return _completion(endpoint, response, request_id, upstream_id)
