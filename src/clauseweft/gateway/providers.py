"""Upstream chat transports (one-shot and SSE streaming) for configured models."""

import base64
import binascii
import http.client
import json
import os
import re
import socket
import threading
import time
from urllib.parse import quote, urlsplit
from uuid import uuid4

from .config import Config, Endpoint
from .errors import GatewayError

_TOOL_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}")
_DATA_IMAGE_URI = re.compile(r"^data:(image/(?:png|jpeg|webp|gif));base64,([A-Za-z0-9+/=\s]+)$")
_MAX_IMAGE_BYTES = 2 * 1024 * 1024


def _bad_response():
    return GatewayError(502, "invalid_upstream_response", "上游未返回有效的文本对话结果")


def _check_utf8(value, message):
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise GatewayError(400, "invalid_messages", message) from None


def _parse_data_image_url(url):
    if not isinstance(url, str) or not url.isascii():
        raise GatewayError(400, "invalid_messages", "图片 URL 格式无效")
    matched = _DATA_IMAGE_URI.fullmatch(url.strip())
    if not matched:
        raise GatewayError(400, "invalid_messages", "图片仅支持受控的 base64 data URI，拒绝外部 URL")
    media_type, encoded = matched.group(1), re.sub(r"\s+", "", matched.group(2))
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise GatewayError(400, "invalid_messages", "图片 base64 内容无效") from None
    if not 1 <= len(raw) <= _MAX_IMAGE_BYTES:
        raise GatewayError(400, "invalid_messages", "单张图片解码大小须在 1 B 至 2 MiB 之间")
    return media_type, encoded


def _validate_user_parts(parts):
    if not 1 <= len(parts) <= 8:
        raise GatewayError(400, "invalid_messages", "多模态消息内容须包含 1 到 8 个片段")
    text_count = 0
    image_count = 0
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("type"), str):
            raise GatewayError(400, "invalid_messages", "多模态片段格式无效")
        ptype = part["type"]
        if ptype == "text":
            if set(part) != {"type", "text"} or not isinstance(part["text"], str) or not part["text"].strip():
                raise GatewayError(400, "invalid_messages", "多模态文本片段须包含非空 text")
            _check_utf8(part["text"], "消息包含无效的 Unicode 文本")
            text_count += 1
        elif ptype == "image_url":
            img = part.get("image_url")
            if set(part) != {"type", "image_url"} or not isinstance(img, dict) or set(img) != {"url"}:
                raise GatewayError(400, "invalid_messages", "image_url 片段仅支持 url 字段")
            _parse_data_image_url(img["url"])
            image_count += 1
            if image_count > 4:
                raise GatewayError(400, "invalid_messages", "单条消息最多包含 4 张图片")
        else:
            raise GatewayError(400, "invalid_messages", "不支持的多模态片段类型")
    if text_count < 1:
        raise GatewayError(400, "invalid_messages", "多模态消息至少需要一个非空文本片段")


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
        if role == "system":
            if set(msg) != {"role", "content"} or not isinstance(msg["content"], str) or not msg["content"].strip():
                raise GatewayError(400, "invalid_messages", "消息应仅包含受支持的 role 与非空文本 content")
            _check_utf8(msg["content"], "消息包含无效的 Unicode 文本")
        elif role == "user":
            if set(msg) != {"role", "content"}:
                raise GatewayError(400, "invalid_messages", "消息应仅包含受支持的 role 与 content")
            content = msg["content"]
            if isinstance(content, list):
                _validate_user_parts(content)
            elif isinstance(content, str) and content.strip():
                _check_utf8(content, "消息包含无效的 Unicode 文本")
            else:
                raise GatewayError(400, "invalid_messages", "消息应仅包含受支持的 role 与非空文本或多模态 content")
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


def _safe_json_obj(raw):
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return parsed
        except ValueError:
            pass
    return {}


def _ollama_messages(messages):
    converted = []
    for msg in messages:
        if msg["role"] == "tool":
            converted.append({"role": "tool", "content": msg["content"]})
        elif msg["role"] == "assistant" and "tool_calls" in msg:
            calls = []
            for call in msg["tool_calls"]:
                calls.append({
                    "function": {
                        "name": call["function"]["name"],
                        "arguments": _safe_json_obj(call["function"]["arguments"]),
                    }
                })
            converted.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        elif msg["role"] == "user" and isinstance(msg["content"], list):
            texts = []
            images = []
            for part in msg["content"]:
                if part["type"] == "text":
                    texts.append(part["text"])
                elif part["type"] == "image_url":
                    _, encoded = _parse_data_image_url(part["image_url"]["url"])
                    images.append(encoded)
            entry = {"role": "user", "content": "\n".join(texts)}
            if images:
                entry["images"] = images
            converted.append(entry)
        else:
            converted.append(msg)
    return converted


def _anthropic_payload(endpoint, messages, max_tokens, *, tools, tool_choice, stream):
    system_parts = []
    conv = []
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_parts.append(msg["content"])
        elif role == "user":
            if isinstance(msg["content"], list):
                blocks = []
                for part in msg["content"]:
                    if part["type"] == "text":
                        blocks.append({"type": "text", "text": part["text"]})
                    elif part["type"] == "image_url":
                        media_type, encoded = _parse_data_image_url(part["image_url"]["url"])
                        blocks.append({
                            "type": "image",
                            "source": {"type": "base64", "media_type": media_type, "data": encoded},
                        })
                conv.append({"role": "user", "content": blocks})
            else:
                conv.append({"role": "user", "content": msg["content"]})
        elif role == "assistant":
            if "tool_calls" in msg:
                blocks = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for call in msg["tool_calls"]:
                    blocks.append({
                        "type": "tool_use",
                        "id": call["id"],
                        "name": call["function"]["name"],
                        "input": _safe_json_obj(call["function"]["arguments"]),
                    })
                conv.append({"role": "assistant", "content": blocks})
            else:
                conv.append({"role": "assistant", "content": msg["content"]})
        elif role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": msg["tool_call_id"],
                "content": msg["content"],
            }
            if (conv and conv[-1]["role"] == "user" and isinstance(conv[-1]["content"], list)
                    and all(b.get("type") == "tool_result" for b in conv[-1]["content"])):
                conv[-1]["content"].append(block)
            else:
                conv.append({"role": "user", "content": [block]})
    if not conv:
        raise GatewayError(400, "invalid_messages", "Anthropic 协议至少需要一条非 system 消息")
    body = {
        "model": endpoint.model,
        "max_tokens": max_tokens,
        "messages": conv,
        "stream": bool(stream),
    }
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if tools is not None:
        body["tools"] = [
            {
                "name": item["function"]["name"],
                **({"description": item["function"]["description"]} if "description" in item["function"] else {}),
                "input_schema": item["function"].get("parameters", {"type": "object", "properties": {}}),
            }
            for item in tools
        ]
        if tool_choice is not None:
            if tool_choice == "auto":
                body["tool_choice"] = {"type": "auto"}
            elif tool_choice == "required":
                body["tool_choice"] = {"type": "any"}
            elif tool_choice == "none":
                body["tool_choice"] = {"type": "none"}
            elif isinstance(tool_choice, dict):
                body["tool_choice"] = {"type": "tool", "name": tool_choice["function"]["name"]}
    return body


def _gemini_payload(endpoint, messages, max_tokens, *, json_decision, tools, tool_choice, response_format):
    system_parts = []
    contents = []
    call_names = {}
    for msg in messages:
        role = msg["role"]
        if role == "system":
            system_parts.append(msg["content"])
        elif role == "user":
            if isinstance(msg["content"], list):
                parts = []
                for part in msg["content"]:
                    if part["type"] == "text":
                        parts.append({"text": part["text"]})
                    elif part["type"] == "image_url":
                        media_type, encoded = _parse_data_image_url(part["image_url"]["url"])
                        parts.append({"inlineData": {"mimeType": media_type, "data": encoded}})
                contents.append({"role": "user", "parts": parts})
            else:
                contents.append({"role": "user", "parts": [{"text": msg["content"]}]})
        elif role == "assistant":
            parts = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})
            for call in msg.get("tool_calls", []):
                call_names[call["id"]] = call["function"]["name"]
                parts.append({
                    "functionCall": {
                        "name": call["function"]["name"],
                        "args": _safe_json_obj(call["function"]["arguments"]),
                    }
                })
            contents.append({"role": "model", "parts": parts})
        elif role == "tool":
            fn_name = call_names.get(msg["tool_call_id"], msg["tool_call_id"])
            part = {"functionResponse": {"name": fn_name, "response": {"content": msg["content"]}}}
            if (contents and contents[-1]["role"] == "user"
                    and all("functionResponse" in p for p in contents[-1]["parts"])):
                contents[-1]["parts"].append(part)
            else:
                contents.append({"role": "user", "parts": [part]})
    if not contents:
        raise GatewayError(400, "invalid_messages", "Gemini 协议至少需要一条非 system 消息")
    gen_config = {"maxOutputTokens": max_tokens}
    if response_format is not None:
        if response_format["type"] == "json_object":
            gen_config["responseMimeType"] = "application/json"
        elif response_format["type"] == "json_schema":
            gen_config["responseMimeType"] = "application/json"
            gen_config["responseSchema"] = response_format["json_schema"]["schema"]
    elif json_decision:
        gen_config["responseMimeType"] = "application/json"
        gen_config["temperature"] = 0
    body = {"contents": contents, "generationConfig": gen_config}
    if system_parts:
        body["systemInstruction"] = {"parts": [{"text": "\n\n".join(system_parts)}]}
    if tools is not None:
        body["tools"] = [{
            "functionDeclarations": [
                {
                    "name": item["function"]["name"],
                    **({"description": item["function"]["description"]} if "description" in item["function"] else {}),
                    **({"parameters": item["function"]["parameters"]} if "parameters" in item["function"] else {}),
                }
                for item in tools
            ]
        }]
        if tool_choice is not None:
            if tool_choice == "auto":
                body["toolConfig"] = {"functionCallingConfig": {"mode": "AUTO"}}
            elif tool_choice == "none":
                body["toolConfig"] = {"functionCallingConfig": {"mode": "NONE"}}
            elif tool_choice == "required":
                body["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
            elif isinstance(tool_choice, dict):
                body["toolConfig"] = {
                    "functionCallingConfig": {
                        "mode": "ANY",
                        "allowedFunctionNames": [tool_choice["function"]["name"]],
                    }
                }
    return body


def _payload(endpoint, messages, max_tokens, request_id, json_decision=False, *,
             tools=None, tool_choice=None, response_format=None, stream=False):
    if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,128}", request_id):
        raise GatewayError(400, "invalid_request_id", "请求 ID 无效")
    if type(max_tokens) is not int or not 1 <= max_tokens <= endpoint.max_tokens:
        raise GatewayError(400, "invalid_max_tokens", "输出上限超出端点允许范围")
    validate_messages(messages, allow_tools=bool(tools))
    tools, tool_choice = validate_tools(tools, tool_choice)
    response_format = validate_response_format(response_format)
    if endpoint.protocol == "anthropic":
        body = _anthropic_payload(endpoint, messages, max_tokens,
                                  tools=tools, tool_choice=tool_choice, stream=stream)
    elif endpoint.protocol == "gemini":
        body = _gemini_payload(endpoint, messages, max_tokens,
                               json_decision=json_decision, tools=tools,
                               tool_choice=tool_choice, response_format=response_format)
    elif endpoint.protocol == "ollama":
        body = {
            "model": endpoint.model,
            "messages": _ollama_messages(messages),
            "stream": bool(stream),
            "options": {"num_predict": max_tokens},
        }
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
        body = {
            "model": endpoint.model,
            "messages": messages,
            "stream": bool(stream),
            "max_tokens": max_tokens,
        }
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


def _upstream_suffix(endpoint, *, stream=False):
    if endpoint.protocol == "ollama":
        return "/api/chat"
    if endpoint.protocol == "anthropic":
        return "/messages"
    if endpoint.protocol == "gemini":
        model_seg = quote(endpoint.model, safe="._-")
        action = "streamGenerateContent?alt=sse" if stream else "generateContent"
        return f"/models/{model_seg}:{action}"
    return "/chat/completions"


def _headers(endpoint, request_id, *, stream=False):
    headers = {
        "Content-Type": "application/json",
        "Accept": "text/event-stream" if stream and endpoint.protocol != "ollama" else "application/json",
        "X-Request-ID": request_id,
        "Connection": "close",
    }
    if endpoint.protocol == "anthropic":
        headers["anthropic-version"] = "2023-06-01"
    if endpoint.api_key_env:
        key = os.environ.get(endpoint.api_key_env, "")
        if not key or len(key) > 8192 or not all(33 <= ord(c) <= 126 for c in key):
            raise GatewayError(503, "upstream_credentials_unavailable", "上游凭据未就绪")
        if endpoint.protocol == "anthropic":
            headers["x-api-key"] = key
        elif endpoint.protocol == "gemini":
            headers["x-goog-api-key"] = key
        else:
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
    suffix = _upstream_suffix(endpoint, stream=False)
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


def _anthropic_usage(inp, out, cache_read=None, cache_create=None):
    if type(inp) is not int or type(out) is not int or inp < 0 or out < 0:
        return None
    read_tok = cache_read if type(cache_read) is int and cache_read >= 0 else 0
    create_tok = cache_create if type(cache_create) is int and cache_create >= 0 else 0
    prompt = inp + read_tok + create_tok
    return _usage(
        prompt, out, prompt + out,
        cached=cache_read if type(cache_read) is int and cache_read >= 0 else None,
    )


def _gemini_usage(meta):
    if not isinstance(meta, dict):
        return None
    prompt = meta.get("promptTokenCount")
    cand = meta.get("candidatesTokenCount", 0) if prompt is not None else None
    if type(prompt) is not int or type(cand) is not int or prompt < 0 or cand < 0:
        return None
    thoughts = meta.get("thoughtsTokenCount")
    cached = meta.get("cachedContentTokenCount")
    extra_thoughts = thoughts if type(thoughts) is int and thoughts > 0 else 0
    comp = cand + extra_thoughts
    raw_total = meta.get("totalTokenCount")
    if type(raw_total) is int and raw_total == prompt + cand and extra_thoughts == 0:
        total = raw_total
    else:
        total = prompt + comp
    return _usage(prompt, comp, total, cached=cached, reasoning=thoughts)


def _anthropic_reason(stop_reason, has_calls):
    if stop_reason == "tool_use":
        return "tool_calls"
    if stop_reason in ("end_turn", "stop_sequence", None):
        return "tool_calls" if has_calls else "stop"
    if stop_reason == "max_tokens":
        return "length"
    if stop_reason == "refusal":
        return "content_filter"
    return None


def _gemini_reason(finish_reason, has_calls):
    if finish_reason in ("STOP", None):
        return "tool_calls" if has_calls else "stop"
    if finish_reason == "MAX_TOKENS":
        return "length"
    if finish_reason in ("SAFETY", "RECITATION", "BLOCKLIST", "PROHIBITED_CONTENT"):
        return "content_filter"
    return None


def _normalize_tool_calls(raw_calls, *, allow_dict_args=False):
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
        if allow_dict_args and isinstance(raw_args, dict):
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


def _parse_anthropic_message(response):
    blocks = response.get("content")
    if response.get("type") != "message" or not isinstance(blocks, list):
        raise _bad_response()
    texts = []
    calls = []
    for block in blocks:
        if not isinstance(block, dict):
            raise _bad_response()
        btype = block.get("type")
        if btype == "text":
            if not isinstance(block.get("text"), str):
                raise _bad_response()
            texts.append(block["text"])
        elif btype == "tool_use":
            calls.append({
                "id": block.get("id"),
                "function": {"name": block.get("name"), "arguments": block.get("input", {})},
            })
    u = response.get("usage") if isinstance(response.get("usage"), dict) else {}
    usage = _anthropic_usage(
        u.get("input_tokens"), u.get("output_tokens"),
        u.get("cache_read_input_tokens"), u.get("cache_creation_input_tokens"),
    )
    reason = _anthropic_reason(response.get("stop_reason"), bool(calls))
    msg = {"role": "assistant", "content": "".join(texts)}
    if calls:
        msg["tool_calls"] = calls
    return msg, reason, usage


def _parse_gemini_message(response):
    candidates = response.get("candidates")
    if not isinstance(candidates, list) or not candidates or not isinstance(candidates[0], dict):
        raise _bad_response()
    cand = candidates[0]
    content_obj = cand.get("content")
    if not isinstance(content_obj, dict) or not isinstance(content_obj.get("parts"), list):
        raise _bad_response()
    texts = []
    calls = []
    for part in content_obj["parts"]:
        if not isinstance(part, dict):
            raise _bad_response()
        if isinstance(part.get("text"), str) and not part.get("thought"):
            texts.append(part["text"])
        elif isinstance(part.get("functionCall"), dict):
            fc = part["functionCall"]
            calls.append({
                "id": fc.get("id"),
                "function": {"name": fc.get("name"), "arguments": fc.get("args", {})},
            })
    usage = _gemini_usage(response.get("usageMetadata"))
    reason = _gemini_reason(cand.get("finishReason"), bool(calls))
    msg = {"role": "assistant", "content": "".join(texts)}
    if calls:
        msg["tool_calls"] = calls
    return msg, reason, usage


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
        allow_dict_args = True
    elif endpoint.protocol == "anthropic":
        message, reason, usage = _parse_anthropic_message(response)
        allow_dict_args = True
    elif endpoint.protocol == "gemini":
        message, reason, usage = _parse_gemini_message(response)
        allow_dict_args = True
    else:
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise _bad_response()
        message = choices[0].get("message")
        reason = choices[0].get("finish_reason")
        usage = _extract_usage(response.get("usage"))
        allow_dict_args = False
    if not isinstance(message, dict) or message.get("role") != "assistant":
        raise _bad_response()
    raw_calls = message.get("tool_calls")
    has_calls = bool(raw_calls)
    if has_calls and not allow_tool_calls:
        raise _bad_response()
    if has_calls:
        tool_calls = _normalize_tool_calls(raw_calls, allow_dict_args=allow_dict_args)
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
    upstream_model = response.get("model") or response.get("modelVersion")
    upstream_cid = (
        response.get("id") if endpoint.protocol in ("chat_completions", "anthropic")
        else response.get("responseId") if endpoint.protocol == "gemini"
        else None
    )
    identifiers = {
        "upstream_model": upstream_model,
        "upstream_request_id": upstream_request_id,
        "upstream_completion_id": upstream_cid,
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
    suffix = _upstream_suffix(endpoint, stream=True)
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
        anth_in = None
        anth_cache_read = None
        anth_cache_create = None
        anth_tool_idx = -1
        anth_has_calls = False
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
                            delta["tool_calls"] = _normalize_tool_calls(msg["tool_calls"], allow_dict_args=True)
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
                    elif endpoint.protocol == "anthropic":
                        if not text.startswith("data:"):
                            continue
                        payload = text[5:].strip()
                        if payload == "[DONE]":
                            break
                        item = json.loads(payload)
                        if not isinstance(item, dict) or item.get("type") == "error":
                            break
                        etype = item.get("type")
                        if etype == "message_start":
                            msg_u = (item.get("message") or {}).get("usage") or {}
                            anth_in = msg_u.get("input_tokens")
                            anth_cache_read = msg_u.get("cache_read_input_tokens")
                            anth_cache_create = msg_u.get("cache_creation_input_tokens")
                            yield {
                                "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                "model": endpoint.id,
                                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                            }
                        elif etype == "content_block_start":
                            cb = item.get("content_block") or {}
                            if tools and cb.get("type") == "tool_use":
                                anth_tool_idx += 1
                                anth_has_calls = True
                                yield {
                                    "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                    "model": endpoint.id,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"tool_calls": [{
                                            "index": anth_tool_idx,
                                            "id": cb.get("id") or f"call_{anth_tool_idx}",
                                            "type": "function",
                                            "function": {"name": cb.get("name", ""), "arguments": ""},
                                        }]},
                                        "finish_reason": None,
                                    }],
                                }
                        elif etype == "content_block_delta":
                            d = item.get("delta") or {}
                            if d.get("type") == "text_delta" and isinstance(d.get("text"), str) and d["text"]:
                                yield {
                                    "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                    "model": endpoint.id,
                                    "choices": [{"index": 0, "delta": {"content": d["text"]}, "finish_reason": None}],
                                }
                            elif (tools and d.get("type") == "input_json_delta"
                                  and isinstance(d.get("partial_json"), str)):
                                yield {
                                    "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                    "model": endpoint.id,
                                    "choices": [{
                                        "index": 0,
                                        "delta": {"tool_calls": [{
                                            "index": max(0, anth_tool_idx),
                                            "function": {"arguments": d["partial_json"]},
                                        }]},
                                        "finish_reason": None,
                                    }],
                                }
                        elif etype == "message_delta":
                            stop_reason = (item.get("delta") or {}).get("stop_reason")
                            reason = _anthropic_reason(stop_reason, anth_has_calls) or "stop"
                            out_u = item.get("usage") or {}
                            usage = _anthropic_usage(
                                anth_in, out_u.get("output_tokens"), anth_cache_read, anth_cache_create,
                            )
                            out = {
                                "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                "model": endpoint.id,
                                "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
                            }
                            if usage is not None:
                                out["usage"] = usage
                            yield out
                        elif etype == "message_stop":
                            break
                    elif endpoint.protocol == "gemini":
                        if not text.startswith("data:"):
                            continue
                        payload = text[5:].strip()
                        if payload == "[DONE]":
                            break
                        item = json.loads(payload)
                        if not isinstance(item, dict) or "error" in item:
                            break
                        candidates = item.get("candidates") or []
                        delta = {}
                        reason = None
                        if candidates and isinstance(candidates[0], dict):
                            cand = candidates[0]
                            parts = ((cand.get("content") or {}).get("parts")) or []
                            texts = []
                            raw_calls = []
                            for p in parts:
                                if not isinstance(p, dict):
                                    continue
                                if isinstance(p.get("text"), str) and not p.get("thought") and p["text"]:
                                    texts.append(p["text"])
                                elif tools and isinstance(p.get("functionCall"), dict):
                                    fc = p["functionCall"]
                                    raw_calls.append({
                                        "id": fc.get("id"),
                                        "function": {"name": fc.get("name"), "arguments": fc.get("args", {})},
                                    })
                            if texts:
                                delta["content"] = "".join(texts)
                            if raw_calls:
                                delta["tool_calls"] = _normalize_tool_calls(raw_calls, allow_dict_args=True)
                            if cand.get("finishReason"):
                                reason = _gemini_reason(cand.get("finishReason"), bool(raw_calls))
                        usage = _gemini_usage(item.get("usageMetadata"))
                        if delta or reason is not None or usage is not None:
                            out = {
                                "id": chunk_id, "object": "chat.completion.chunk", "created": created,
                                "model": endpoint.id,
                                "choices": [{"index": 0, "delta": delta, "finish_reason": reason}],
                            }
                            if usage is not None:
                                out["usage"] = usage
                            yield out
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
