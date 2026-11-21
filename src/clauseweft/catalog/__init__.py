"""Versioned routing metadata and direct clients for owner-configured gateways."""

import http.client
import json
import os
import re
import socket
import threading
from urllib.parse import urlsplit

from clauseweft.routing.preview import model_candidate
from clauseweft.storage import canonical, digest


MODEL_FIELDS = {
    "endpoint_id",
    "model_identity",
    "location",
    "enabled",
    "allowed",
    "context_tokens",
    "max_output_tokens",
    "capabilities",
    "fit",
    "priority",
    "input_micros_per_million",
    "output_micros_per_million",
    "latency_ms",
    "quality_bp",
    "gateway_id",
    "endpoint_alias",
    "gateway_etag",
    "identity_verified",
    "currency",
    "max_concurrency",
}


class GatewayFailure(Exception):
    def __init__(self, code, receipt=None):
        super().__init__(code)
        self.code = code
        self.receipt = receipt


class Catalog:
    def __init__(self, store, gateways):
        self.store = store
        self.gateways = dict(gateways)
        for config in self.gateways.values():
            if (
                not isinstance(config, dict)
                or config.keys() - {"url", "token_env"}
                or not isinstance(config.get("url"), str)
            ):
                raise ValueError("网关配置仅接受 URL 及客户端令牌环境变量引用")
            url = urlsplit(config["url"])
            if url.port == 0:
                raise ValueError("网关端口无效")
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or url.username is not None
                or url.password is not None
                or url.query
                or url.fragment
                or url.path not in ("", "/")
            ):
                raise ValueError("网关 URL 必须是无凭据的 HTTP(S) 服务根地址")
            if url.scheme == "http" and url.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise ValueError("非回环网关须使用 HTTPS")
            if "token_env" in config and (
                not isinstance(config["token_env"], str)
                or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", config["token_env"])
            ):
                raise ValueError("网关令牌环境变量名无效")

    def _bound(self, row):
        info = json.loads(row["data"])
        binding = info.pop("_gateway_binding", None)
        config = self.gateways.get(info["gateway_id"])
        if config is None or binding != digest(config):
            info["enabled"] = False
        return dict(info, revision=row["revision"])

    def save(self, model):
        if not isinstance(model, dict) or model.keys() - (MODEL_FIELDS | {"revision"}):
            raise ValueError("未知端点配置字段")
        info = {k: v for k, v in model.items() if k != "revision"}
        info.setdefault("currency", "USD")
        info.setdefault("identity_verified", False)
        info.setdefault("endpoint_alias", info.get("endpoint_id"))
        info.setdefault("max_concurrency", 4)
        if (
            type(info["identity_verified"]) is not bool
            or not isinstance(info.get("gateway_id"), str)
            or info["gateway_id"] not in self.gateways
            or not isinstance(info.get("gateway_etag"), str)
            or not re.fullmatch(r'"[A-Za-z0-9._-]{1,128}"', info["gateway_etag"])
            or not isinstance(info.get("endpoint_alias"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}", info["endpoint_alias"]
            )
            or not isinstance(info.get("endpoint_id"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", info["endpoint_id"]
            )
            or not isinstance(info["currency"], str)
            or not re.fullmatch(r"[A-Z]{3}", info["currency"])
            or type(info["max_concurrency"]) is not int
            or not 1 <= info["max_concurrency"] <= 256
        ):
            raise ValueError("网关绑定、端点标识、币种、并发或模型身份验证字段不合法")
        model_candidate(
            info,
            dict(
                input_tokens=1,
                max_output_tokens=1,
                required_capabilities=[],
                data_scope="local_only",
                budget_micros=0,
            ),
            "review",
        )
        with self.store.transaction() as db:
            from clauseweft import audit

            previous = db.execute(
                "SELECT data FROM endpoint_revisions WHERE endpoint_id=? ORDER BY revision DESC LIMIT 1",
                (info["endpoint_id"],),
            ).fetchone()
            revision = db.execute(
                "SELECT coalesce(max(revision),0)+1 FROM endpoint_revisions WHERE endpoint_id=?",
                (info["endpoint_id"],),
            ).fetchone()[0]
            db.execute(
                "INSERT INTO endpoint_revisions VALUES(?,?,?)",
                (
                    info["endpoint_id"],
                    revision,
                    canonical(
                        dict(
                            info,
                            _gateway_binding=digest(self.gateways[info["gateway_id"]]),
                        )
                    ),
                ),
            )
            audit.save_revision(
                db,
                "endpoints",
                info["endpoint_id"],
                revision,
                info,
                actor=getattr(self.store.local, "audit_actor", audit.SYSTEM),
                before=json.loads(previous[0]) if previous else None,
                correlation_id=getattr(self.store.local, "audit_correlation_id", None),
            )
        return dict(info, revision=revision)

    def get(self, endpoint_id, revision=None):
        with self.store.transaction() as db:
            row = db.execute(
                """SELECT * FROM endpoint_revisions WHERE endpoint_id=? AND (? IS NULL OR revision=?)
                                ORDER BY revision DESC LIMIT 1""",
                (endpoint_id, revision, revision),
            ).fetchone()
            if not row:
                raise KeyError(endpoint_id)
            return self._bound(row)

    def list(self):
        with self.store.transaction() as db:
            return [
                self._bound(r)
                for r in db.execute(
                    """SELECT data,revision FROM endpoint_revisions e WHERE revision=(SELECT max(revision)
                    FROM endpoint_revisions WHERE endpoint_id=e.endpoint_id) ORDER BY endpoint_id"""
                )
            ]

    def probe(self, endpoint_id):
        endpoint = self.get(endpoint_id)
        data, receipt, etag = self.request(endpoint, "GET", "/v1/models")
        if not isinstance(data.get("data"), list) or not isinstance(etag, str):
            raise GatewayFailure("invalid_gateway_response", receipt)
        model = next((m for m in data["data"] if isinstance(m, dict)
                      and m.get("id") == endpoint["endpoint_alias"]), None)
        limit = model.get("max_output_tokens") if model else None
        if limit is not None and (type(limit) is not int or not 1 <= limit <= 1_048_576):
            raise GatewayFailure("invalid_gateway_response", receipt)
        return dict(
            endpoint_id=endpoint_id,
            revision=endpoint["revision"],
            connected=True,
            alias_present=model is not None,
            max_output_tokens=limit,
            etag_matches=etag == endpoint["gateway_etag"],
            gateway_etag=etag,
            request_id=receipt,
            capabilities_verified=False,
        )

    def generate(self, endpoint, messages, max_tokens):
        body = dict(
            model=endpoint["endpoint_alias"], messages=messages, max_tokens=max_tokens
        )
        response, receipt, etag = self.request(
            endpoint, "POST", "/v1/chat/completions", body
        )
        try:
            choice = response["choices"][0]
            limit = response.get("gateway_metadata", {}).get("max_output_tokens")
            if (
                response["model"] != endpoint["endpoint_alias"]
                or etag != endpoint["gateway_etag"]
                or not receipt
                or not isinstance(response["choices"], list) or len(response["choices"]) != 1
                or choice["message"]["role"] != "assistant"
                or choice["message"].get("tool_calls")
                or not isinstance(choice["message"]["content"], str)
                or choice["finish_reason"] not in ("stop", "length", "content_filter")
                or (limit is not None and (type(limit) is not int or not 1 <= limit <= max_tokens))
            ):
                raise ValueError
            return (
                choice["message"]["content"],
                response.get("usage"),
                receipt,
                choice["finish_reason"],
                limit,
            )
        except (AttributeError, IndexError, KeyError, ValueError, TypeError):
            raise GatewayFailure("invalid_gateway_response", receipt) from None

    def request(self, endpoint, method, path, body=None):
        config = self.gateways.get(endpoint["gateway_id"])
        if config is None:
            raise GatewayFailure("gateway_not_configured")
        url = urlsplit(config["url"])
        connection = (
            http.client.HTTPSConnection
            if url.scheme == "https"
            else http.client.HTTPConnection
        )(url.hostname, url.port, timeout=30)
        headers = {
            "Content-Type": "application/json",
            "If-Match": endpoint["gateway_etag"],
        }
        if method == "POST" and path == "/v1/chat/completions":
            headers["X-ClauseWeft-Token-Limit"] = "clamp"
            if endpoint.get("json_decision") is True:
                headers["X-ClauseWeft-Output-Format"] = "json"
        if config.get("token_env"):
            token = os.environ.get(config["token_env"])
            if not token:
                raise GatewayFailure("gateway_credentials_unavailable")
            headers["Authorization"] = "Bearer " + token
        timer = None
        try:
            connection.connect()
            sock = connection.sock

            def abort():
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass

            timer = threading.Timer(30, abort)
            timer.daemon = True
            timer.start()
            connection.request(
                method, path, canonical(body).encode() if body else None, headers
            )
            with connection.getresponse() as response:
                raw = response.read(4_194_305)
                if len(raw) > 4_194_304:
                    raise GatewayFailure("invalid_gateway_response")
                data = json.loads(raw)
                receipt = response.getheader("X-Request-ID")
                if not isinstance(data, dict):
                    raise GatewayFailure("invalid_gateway_response")
                if response.status != 200:
                    error = data.get("error", {})
                    code = error.get("code") if isinstance(error, dict) else None
                    if not isinstance(code, str) or not re.fullmatch(
                        r"[a-z_]{1,80}", code
                    ):
                        code = "gateway_error"
                    raise GatewayFailure(code, receipt)
                return data, receipt, response.getheader("ETag")
        except (OSError, ValueError, http.client.HTTPException, RecursionError):
            raise GatewayFailure("gateway_unavailable") from None
        finally:
            if timer is not None:
                timer.cancel()
            connection.close()
