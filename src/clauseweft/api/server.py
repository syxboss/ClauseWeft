"""Loopback HTTP entry point for a single trusted workspace."""

from contextlib import contextmanager
from pathlib import Path
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
from uuid import uuid4
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit

from clauseweft.gateway.server import reject_constant, unique_object

from .http_contracts import Problem


class Handler(BaseHTTPRequestHandler):
    server_version = "ClauseWeft"
    sys_version = ""

    def setup(self):
        super().setup()
        self.connection.settimeout(15)
        self.correlation_id = uuid4().hex
        self.application_handled = False
        self.authenticated_actor = None

    def audit_http(self, status, outcome, code=None):
        from clauseweft import audit

        try:
            resource = urlsplit(getattr(self, "path", "invalid_path")).path
        except ValueError:
            resource = "invalid_path"
        with self.server.app.store.transaction() as db:
            audit.record(
                db,
                self.authenticated_actor
                or {"id": "anonymous", "display_name": "未登录"},
                "http." + (getattr(self, "command", None) or "invalid").lower(),
                resource,
                outcome=outcome,
                correlation_id=self.correlation_id,
                details={"status": status, "code": code},
            )

    def log_message(self, *args):
        pass

    def respond(self, status, body, html=False):
        data = (
            body.encode()
            if html
            else json.dumps(body, ensure_ascii=True, allow_nan=False).encode()
        )
        self.send_response(status)
        self.send_header(
            "Content-Type",
            "text/html; charset=utf-8" if html else "application/json; charset=utf-8",
        )
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Request-ID", self.correlation_id)
        policy = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
        if html:
            from .console import SCRIPT_HASH

            policy += f"; script-src '{SCRIPT_HASH}'; style-src 'unsafe-inline'; connect-src 'self'"
        self.send_header("Content-Security-Policy", policy)
        if not html and "revision" in body:
            self.send_header("ETag", f'"{body["revision"]}"')
        if status == 201 and "run_id" in body and self.path == "/api/v1/runs":
            self.send_header("Location", "/api/v1/runs/" + body["run_id"])
        if status == 503:
            self.send_header("Retry-After", "1")
        self.end_headers()
        self.wfile.write(data)

    def handle_request(self):
        hosts = self.headers.get_all("Host", [])
        port = self.server.server_port
        accepted = {f"localhost:{port}", f"127.0.0.1:{port}"}
        if port == 80:
            accepted.update(("localhost", "127.0.0.1"))
        if len(hosts) != 1 or hosts[0] not in accepted:
            raise Problem(403, "FORBIDDEN", "Host 与监听地址不匹配")
        origins = self.headers.get_all("Origin", [])
        if origins and origins != ["http://" + hosts[0]]:
            raise Problem(403, "FORBIDDEN", "Origin 与本机工作台不匹配")
        url = urlsplit(self.path)
        if url.netloc or url.scheme or url.fragment:
            raise Problem(400, "INVALID_PATH", "路径无效")
        if self.command == "GET" and url.path == "/":
            from .console import PAGE

            self.audit_http(200, "read")
            return self.respond(200, PAGE, html=True)
        if not url.path.startswith("/api/v1/"):
            raise Problem(404, "NOT_FOUND", "资源不存在")
        parts = tuple(unquote(part) for part in url.path[8:].split("/"))
        pairing = self.command == "POST" and parts == ("auth", "pair")
        login = self.command == "POST" and parts == ("auth", "login")
        auth = self.headers.get_all("Authorization", [])
        principal = None
        credential = ""
        if not login:
            if len(auth) != 1 or not auth[0].startswith("Bearer "):
                raise Problem(401, "UNAUTHORIZED", "请登录平台或提供管理令牌")
            credential = auth[0][7:]
            if not hmac.compare_digest(auth[0].encode(), self.server.authorization):
                principal = self.server.app.platform.authenticate(credential)
            from .platform import BOOTSTRAP

            self.authenticated_actor = principal or BOOTSTRAP
        args = parse_qs(url.query, max_num_fields=10, keep_blank_values=True)
        if any(len(v) != 1 for v in args.values()):
            raise ValueError("查询参数重复")
        if (
            self.command == "GET"
            and len(parts) == 3
            and parts[2] == "stream"
            and parts[0] in ("runs", "agent-jobs")
        ):
            return self.stream_events(parts[0], parts[1], {k: v[0] for k, v in args.items()}, principal)
        data = {}
        key = None
        revision = None
        if self.command != "GET":
            length = self.headers.get_all("Content-Length", [])
            if (
                self.headers.get("Transfer-Encoding")
                or len(length) != 1
                or not re.fullmatch(r"[0-9]{1,10}", length[0])
            ):
                raise Problem(411, "LENGTH_REQUIRED", "需要有效的 Content-Length")
            if int(length[0]) > 1_048_576:
                raise Problem(413, "CONTENT_TOO_LARGE", "请求体超过 1 MiB")
            if self.headers.get_content_type() != "application/json":
                raise Problem(415, "INVALID_MEDIA_TYPE", "需要 application/json")
            raw = self.rfile.read(int(length[0]))
            data = json.loads(
                raw.decode(),
                object_pairs_hook=unique_object,
                parse_constant=reject_constant,
            )
            if not isinstance(data, dict):
                raise ValueError("请求正文须为对象")
            keys = self.headers.get_all("Idempotency-Key", [])
            if parts not in (("routing", "previews"), ("auth", "login"), ("auth", "pair")):
                if len(keys) != 1 or not re.fullmatch(
                    r"[a-zA-Z0-9._:-]{1,128}", keys[0]
                ):
                    raise ValueError("写入需提供 Idempotency-Key")
                key = keys[0]
            versions = self.headers.get_all("If-Match", [])
            if versions:
                if len(versions) != 1 or not re.fullmatch(
                    r'"[0-9]{1,18}"', versions[0]
                ):
                    raise ValueError("If-Match 须使用带引号的版本号")
                revision = int(versions[0][1:-1])
        if pairing:
            from .personal_pairing import pair

            if data:
                raise ValueError("配对使用空 JSON 对象")
            result = pair(self.server.app.platform, self.authenticated_actor)
            self.authenticated_actor = result["user"]
            self.audit_http(200, "succeeded")
            return self.respond(200, result)
        if login:
            if data.keys() != {"username", "password"}:
                raise ValueError("登录需要用户名和密码")
            result = self.server.app.platform.login(data["username"], data["password"])
            self.authenticated_actor = result["user"]
            self.audit_http(200, "succeeded")
            return self.respond(200, result)
        self.application_handled = True
        status, body = self.server.app.handle(
            self.command,
            parts,
            {k: v[0] for k, v in args.items()},
            data,
            key,
            revision,
            principal,
            self.correlation_id,
        )
        if parts == ("auth", "logout") and status == 200:
            self.server.app.platform.logout(credential)
        self.respond(status, body)

    def stream_events(self, kind, resource_id, query, principal):
        from .platform import BOOTSTRAP

        after = int(query.get("after", "0"))
        timeout = float(query.get("timeout", "30"))
        if after < 0 or not 0.1 <= timeout <= 60.0:
            raise ValueError("after 或 timeout 参数无效")
        stop_on_input = query.get("stop_on_input") == "1"
        app = self.server.app
        actor = principal or BOOTSTRAP
        app.request_context.principal = actor
        app.request_context.correlation_id = self.correlation_id
        app.store.local.audit_actor = actor
        app.store.local.audit_correlation_id = self.correlation_id
        with app.store.transaction() as db:
            actor = app.platform.public_actor(db, actor)
            app.request_context.principal = actor
            app.authorize(db, "GET", (kind, resource_id), {}, {})
        self.application_handled = True
        self.audit_http(200, "read")
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Request-ID", self.correlation_id)
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def send_sse(event_name, payload, event_id=None):
            lines = []
            if event_id is not None:
                lines.append(f"id: {event_id}")
            lines.append(f"event: {event_name}")
            lines.append("data: " + json.dumps(payload, ensure_ascii=True, allow_nan=False))
            self.wfile.write(("\n".join(lines) + "\n\n").encode("utf-8"))
            self.wfile.flush()

        deadline = time.monotonic() + timeout
        last_ping = time.monotonic()
        cursor = after
        try:
            while True:
                with app.store.transaction() as db:
                    try:
                        current_actor = app.platform.public_actor(db, actor)
                        app.request_context.principal = current_actor
                        app.authorize(db, "GET", (kind, resource_id), {}, {})
                    except Problem:
                        send_sse("revoked", {"reason": "authorization_changed"})
                        return
                    if kind == "runs":
                        rows = db.execute(
                            "SELECT seq, at, kind, data FROM events WHERE run_id=? AND seq>? ORDER BY seq",
                            (resource_id, cursor),
                        ).fetchall()
                        status_row = db.execute(
                            "SELECT status FROM runs WHERE run_id=?", (resource_id,)
                        ).fetchone()
                        status = status_row["status"] if status_row else "cancelled"
                    else:
                        rows = db.execute(
                            "SELECT seq, event FROM agent_job_events WHERE job_id=? AND seq>? ORDER BY seq",
                            (resource_id, cursor),
                        ).fetchall()
                        status_row = db.execute(
                            "SELECT status FROM agent_jobs WHERE id=?", (resource_id,)
                        ).fetchone()
                        status = status_row["status"] if status_row else "cancelled"
                for row in rows:
                    cursor = row["seq"]
                    if kind == "runs":
                        send_sse(
                            row["kind"],
                            {"seq": row["seq"], "at": row["at"], "kind": row["kind"], "data": json.loads(row["data"])},
                            event_id=row["seq"],
                        )
                    else:
                        ev = json.loads(row["event"])
                        send_sse(ev.get("type", "event"), dict(ev, seq=row["seq"]), event_id=row["seq"])
                terminal = (
                    status in ("completed", "cancelled") or (stop_on_input and status == "needs_input")
                    if kind == "runs"
                    else status in ("succeeded", "failed", "cancelled", "interrupted")
                )
                if terminal:
                    send_sse("status", {"status": status, "after": cursor})
                    return
                now = time.monotonic()
                if now >= deadline:
                    send_sse("timeout", {"status": status, "after": cursor})
                    return
                if now - last_ping >= 5.0:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = now
                time.sleep(0.15)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return

    def serve(self):
        try:
            self.handle_request()
            return
        except Problem as exc:
            status, code, details = exc.status, exc.code, str(exc)
        except (ValueError, UnicodeError, RecursionError, OverflowError) as exc:
            status, code, details = 422, "INVALID_INPUT", str(exc)[:300]
        except KeyError:
            status, code, details = 422, "MISSING_FIELD", "缺少所需字段"
        except sqlite3.OperationalError as exc:
            if getattr(exc, "sqlite_errorcode", None) in (
                sqlite3.SQLITE_BUSY,
                sqlite3.SQLITE_LOCKED,
            ):
                status, code, details = 503, "BUSY", "数据库暂忙"
            else:
                status, code, details = 500, "STORAGE_ERROR", "数据库操作失败"
        except TimeoutError:
            status, code, details = 408, "TIMEOUT", "请求超时"
        except (BrokenPipeError, ConnectionResetError):
            self.audit_http(499, "failed", "CLIENT_DISCONNECTED")
            return
        except Exception as exc:
            print(f"工作台内部错误 {type(exc).__name__}", file=sys.stderr)
            status, code, details = 500, "INTERNAL_ERROR", "工作台内部错误"
        if not self.application_handled:
            self.audit_http(
                status, "denied" if status in (401, 403) else "failed", code
            )
        self.respond(
            status,
            {"error": {"code": code, "details": details, "retryable": status == 503}},
        )

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = do_OPTIONS = do_HEAD = serve


def serve(app, port, token, workers):
    with ThreadingHTTPServer(("127.0.0.1", port), Handler) as server:
        server.app = app
        server.authorization = ("Bearer " + token).encode()
        stop = threading.Event()
        threads = [
            threading.Thread(target=app.work, args=(stop,), daemon=True)
            for _ in range(workers)
        ]
        for thread in threads:
            thread.start()
        try:
            print(f"工作台已启动 http://127.0.0.1:{server.server_port}/", flush=True)
            server.serve_forever()
        finally:
            stop.set()
            for thread in threads:
                thread.join(timeout=1)


@contextmanager
def control_lock(directory):
    # One controller owns the worker pool and startup recovery for this database.
    root = Path(directory).resolve()
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "controller.lock").open("a+b") as handle:
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise ValueError("已有控制进程使用该状态目录") from exc
        yield


def main(args):
    from .application import Application

    token = os.environ.get("CLAUSEWEFT_CONSOLE_TOKEN")
    if not token:
        if not sys.stderr.isatty():
            print("后台运行请先设置 CLAUSEWEFT_CONSOLE_TOKEN", file=sys.stderr)
            return 2
        token = secrets.token_urlsafe(32)
        print("本次工作台访问令牌 " + token, file=sys.stderr)
    if not token.isascii() or not all(32 < ord(c) < 127 for c in token):
        print("工作台令牌须为可见 ASCII 字符", file=sys.stderr)
        return 2
    try:
        if not 1 <= args.workers <= 32 or not 0 <= args.port <= 65535:
            raise ValueError("端口或 worker 数量无效")
        with control_lock(args.state):
            app = Application(args.state, args.config)
            serve(app, args.port, token, args.workers)
    except (OSError, ValueError) as exc:
        print(f"工作台启动失败：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        pass
    return 0
