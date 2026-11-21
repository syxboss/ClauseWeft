"""HTTP transport for the local model gateway."""

import argparse
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import re
import socket
import sys
import threading
from urllib.parse import urlsplit
from uuid import uuid4

from .config import load_config
from .errors import GatewayError
from .providers import invoke, invoke_stream, validate_messages, validate_response_format, validate_tools


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def reject_constant(value):
    raise ValueError("invalid JSON constant")


def error_payload(request_id, code, message):
    return {"error": {"code": code, "message": message}, "request_id": request_id}


def loopback_literal(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class GatewayHandler(BaseHTTPRequestHandler):
    server_version = "ClauseWeftGateway"
    sys_version = ""

    def setup(self):
        super().setup()
        self.request.settimeout(15)
        self.request_id = uuid4().hex
        self.response_etag = None

    def log_message(self, format, *args):
        # Default logging includes untrusted paths and query strings.
        pass

    def reply(self, status, body):
        data = json.dumps(body, ensure_ascii=True, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("X-Request-ID", self.request_id)
        self.send_header("Cache-Control", "no-store")
        if self.response_etag is not None:
            self.send_header("ETag", self.response_etag)
        self.send_header("Connection", "close")
        if status == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)
        self.close_connection = True

    def send_error(self, code, message=None, explain=None):
        # The base parser may include request text in its default error page.
        self.reply(code, error_payload(self.request_id, "http_error", "HTTP 请求无效或方法不受支持"))

    def authorize(self):
        expected = self.server.token
        if expected is not None:
            headers = self.headers.get_all("Authorization", [])
            scheme, _, credential = headers[0].partition(" ") if len(headers) == 1 else ("", "", "")
            if scheme.casefold() != "bearer" or not hmac.compare_digest(credential.encode(), expected):
                raise GatewayError(401, "unauthorized", "需要有效的网关访问令牌")
            return
        # A tokenless loopback service should not accept a browser rebound to a
        # remote Host or requests issued by another web origin.
        hosts = self.headers.get_all("Host", [])
        if len(hosts) == 1:
            try:
                authority = urlsplit("//" + hosts[0])
                local = loopback_literal(authority.hostname or "") and authority.username is None
            except ValueError:
                local = False
            origins = self.headers.get_all("Origin", [])
            if local and (not origins or origins == ["http://" + hosts[0]]):
                return
        raise GatewayError(403, "forbidden", "无令牌的本地服务仅接受本机访问")

    def read_json(self):
        if self.headers.get("Transfer-Encoding") is not None:
            raise GatewayError(400, "invalid_request", "请求必须使用 Content-Length")
        sizes = self.headers.get_all("Content-Length", [])
        if not sizes:
            raise GatewayError(411, "length_required", "缺少 Content-Length")
        if len(sizes) != 1 or not re.fullmatch(r"[0-9]{1,18}", sizes[0]):
            raise GatewayError(400, "invalid_request", "Content-Length 无效")
        size = int(sizes[0])
        if size > self.server.config.max_request_bytes:
            raise GatewayError(413, "request_too_large", "请求超过网关大小限制")
        media_types = self.headers.get_all("Content-Type", [])
        if len(media_types) != 1 or self.headers.get_content_type() != "application/json":
            raise GatewayError(415, "unsupported_media_type", "需要 application/json")
        try:
            raw = self.rfile.read(size)
        except TimeoutError:
            raise GatewayError(408, "request_timeout", "读取请求超时") from None
        if len(raw) != size:
            raise GatewayError(400, "invalid_request", "请求体不完整")
        try:
            return json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object,
                              parse_constant=reject_constant)
        except (ValueError, RecursionError):
            raise GatewayError(400, "invalid_json", "请求体必须是有效且无重复字段的 UTF-8 JSON") from None

    def reply_stream(self, chunks):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("X-Request-ID", self.request_id)
        self.send_header("Cache-Control", "no-store")
        if self.response_etag is not None:
            self.send_header("ETag", self.response_etag)
        self.send_header("Connection", "close")
        self.end_headers()
        try:
            for chunk in chunks:
                line = "data: " + json.dumps(chunk, ensure_ascii=True, allow_nan=False) + "\n\n"
                self.wfile.write(line.encode("utf-8"))
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
        except OSError:
            pass
        self.close_connection = True

    def chat(self):
        body = self.read_json()
        allowed = {"model", "messages", "max_tokens", "stream", "tools", "tool_choice", "response_format"}
        if not isinstance(body, dict) or body.keys() - allowed:
            raise GatewayError(400, "invalid_request", "请求对象含不支持的字段或格式错误")
        stream = body.get("stream", False)
        if type(stream) is not bool:
            raise GatewayError(400, "invalid_request", "stream 须为布尔值")
        model = body.get("model")
        if not isinstance(model, str) or not model.strip():
            raise GatewayError(400, "invalid_request", "model 必须是端点 ID")
        config = self.server.config
        endpoint = config.endpoints.get(model)
        if endpoint is None:
            raise GatewayError(404, "model_not_found", "请求的模型端点未配置")
        tools, tool_choice = validate_tools(body.get("tools"), body.get("tool_choice"))
        response_format = validate_response_format(body.get("response_format"))
        messages = body.get("messages")
        validate_messages(messages, allow_tools=bool(tools))
        limit = min(config.max_tokens, endpoint.max_tokens or config.max_tokens)
        tokens = body.get("max_tokens", limit)
        if type(tokens) is not int or tokens < 1:
            raise GatewayError(400, "invalid_request", "max_tokens 超出配置范围")
        if tokens > limit and self.headers.get("X-ClauseWeft-Token-Limit") != "clamp":
            raise GatewayError(400, "invalid_request", "max_tokens 超出配置范围")
        effective_tokens = min(tokens, limit)
        json_decision = self.headers.get("X-ClauseWeft-Output-Format") == "json"
        if stream:
            chunks = invoke_stream(
                config, model, messages, effective_tokens, self.request_id,
                json_decision=json_decision, tools=tools, tool_choice=tool_choice,
                response_format=response_format,
            )
            self.reply_stream(chunks)
            return None
        result = invoke(
            config, model, messages, effective_tokens, self.request_id,
            json_decision=json_decision, tools=tools, tool_choice=tool_choice,
            response_format=response_format,
        )
        result.setdefault("gateway_metadata", {})["max_output_tokens"] = effective_tokens
        return result

    def dispatch(self):
        if self.command == "GET" and self.path == "/healthz":
            return {"status": "ok"}
        self.authorize()
        if self.command == "GET" and self.path == "/v1/models":
            self.response_etag = f'"{self.server.config.revision}"'
            return {"object": "list", "data": [
                {"id": name, "object": "model", "created": 0, "owned_by": "clauseweft",
                 "max_output_tokens": self.server.config.endpoints[name].max_tokens}
                for name in sorted(self.server.config.endpoints)
            ]}
        if self.command == "POST" and self.path == "/v1/chat/completions":
            self.response_etag = f'"{self.server.config.revision}"'
            conditions = self.headers.get_all("If-Match", [])
            if conditions and conditions != [self.response_etag]:
                raise GatewayError(412, "revision_mismatch", "配置版本已改变，请重新获取模型目录")
            permits = self.server.inference_slots
            if not permits.acquire(blocking=False):
                raise GatewayError(429, "concurrency_limit", "网关并发请求已满")
            try:
                return self.chat()
            finally:
                permits.release()
        if self.path in ("/healthz", "/v1/models", "/v1/chat/completions"):
            raise GatewayError(405, "method_not_allowed", "该路径不支持此 HTTP 方法")
        raise GatewayError(404, "not_found", "访问的接口不存在")

    def handle_api(self):
        try:
            body = self.dispatch()
        except GatewayError as err:
            self.reply(err.status, error_payload(self.request_id, err.code, err.message))
        except Exception:
            self.reply(500, error_payload(self.request_id, "internal_error", "网关内部错误"))
        else:
            if body is not None:
                self.reply(200, body)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = handle_api


class GatewayServer(ThreadingHTTPServer):
    def __init__(self, address, family, config, token):
        self.address_family = family
        self.config = config
        self.token = token
        self.inference_slots = threading.BoundedSemaphore(config.max_concurrency)
        # Leave a few connections for model catalog and health checks while all
        # inference slots are busy; also bound clients that stall in headers.
        self.connections = threading.BoundedSemaphore(config.max_concurrency + 8)
        super().__init__(address, GatewayHandler)

    def process_request(self, request, client_address):
        if not self.connections.acquire(blocking=False):
            request_id = uuid4().hex
            payload = json.dumps(error_payload(request_id, "server_busy", "网关连接数已满")).encode()
            try:
                headers = (f"HTTP/1.0 503 Service Unavailable\r\nConnection: close\r\n"
                           f"Content-Type: application/json\r\nX-Request-ID: {request_id}\r\n"
                           f"Content-Length: {len(payload)}\r\n\r\n")
                request.settimeout(0.1)
                request.sendall(headers.encode("ascii") + payload)
            except OSError:
                pass
            finally:
                self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.connections.release()
            raise

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.connections.release()

    def handle_error(self, request, client_address):
        # A client can disconnect while a response is being written. Avoid
        # traceback logging of request- or provider-derived values.
        pass


def create_server(config, host="127.0.0.1", port=8765):
    """Create a bound service. Use port=0 to ask the OS for a free test port."""
    if type(port) is not int or not 0 <= port <= 65535:
        raise ValueError("端口必须为 0 到 65535 之间的整数")
    addresses = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    family, _, _, _, address = min(addresses, key=lambda a: a[0] != socket.AF_INET)
    token = config.token
    if (config.token_env or not loopback_literal(address[0])) and not (token and token.strip()):
        raise ValueError("请配置 token_env 并设置对应环境变量，再启动网关")
    return GatewayServer(address, family, config, token.encode() if token else None)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="clauseweft-gateway", description="多模型 HTTP 网关")
    parser.add_argument("--config", required=True, metavar="FILE", help="端点配置 JSON")
    parser.add_argument("--check-config", action="store_true", help="仅验证配置并打印摘要")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
        if args.check_config:
            print(json.dumps({"status": "valid", "revision": config.revision,
                              "endpoint_count": len(config.endpoints)}))
            return 0
        with create_server(config, args.host, args.port) as server:
            host, port = server.server_address[:2]
            authority = f"[{host}]" if ":" in host else host
            print(f"网关正在监听 http://{authority}:{port}", flush=True)
            server.serve_forever()
    except KeyboardInterrupt:
        return 0
    except GatewayError as err:
        print(err.message, file=sys.stderr)
        return 2
    except (ValueError, OSError) as err:
        # Configuration errors contain field descriptions. OS exceptions can
        # include local paths; report only their type.
        print(str(err) if isinstance(err, ValueError) else type(err).__name__, file=sys.stderr)
        return 2
    return 0
