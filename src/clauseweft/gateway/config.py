"""Load the gateway's trusted endpoint registry from a local JSON file."""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import re
from urllib.parse import urlsplit


_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
_ENDPOINT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}")
_PRIVATE_NETWORKS = tuple(ipaddress.ip_network(block) for block in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7",
))
_CONFIG_FIELDS = {
    "endpoints", "token_env", "timeout_seconds", "max_concurrency",
    "max_request_bytes", "max_response_bytes", "max_tokens",
}
_ENDPOINT_FIELDS = {
    "id", "model", "protocol", "base_url", "api_key_env", "max_tokens",
    "allow_insecure_http",
}


@dataclass(frozen=True)
class Endpoint:
    id: str
    model: str
    protocol: str
    base_url: str
    api_key_env: str | None
    max_tokens: int
    allow_insecure_http: bool


@dataclass(frozen=True)
class Config:
    endpoints: dict[str, Endpoint]
    revision: str
    token_env: str | None
    timeout_seconds: float
    max_concurrency: int
    max_request_bytes: int
    max_response_bytes: int
    max_tokens: int

    @property
    def token(self) -> str | None:
        if self.token_env:
            return os.environ.get(self.token_env) or None
        return os.environ.get("CLAUSEWEFT_GATEWAY_TOKEN") or None


def _object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("配置内不允许重复的 JSON 字段")
        result[name] = value
    return result


def _integer(values, name, default, maximum):
    value = values.get(name, default)
    if type(value) is not int or not 1 <= value <= maximum:
        raise ValueError(f"配置 {name} 必须是 1 到 {maximum} 之间的整数")
    return value


def _env_name(value):
    if not isinstance(value, str) or not _ENV_NAME.fullmatch(value):
        raise ValueError("凭据引用必须是有效的环境变量名")
    return value


def _base_url(value, allow_insecure, authenticated):
    if (not isinstance(value, str) or not value.isascii() or len(value) > 2048
            or any(ord(c) < 33 or ord(c) > 126 for c in value)
            or any(c in value for c in ("\\", "?", "#"))):
        raise ValueError("base_url 需要不带查询、片段或凭据的 HTTP(S) 地址")
    try:
        url = urlsplit(value)
        port = url.port
        host = url.hostname
    except ValueError:
        raise ValueError("base_url 主机或端口无效") from None
    if (url.scheme not in {"http", "https"} or not host or url.username is not None
            or url.password is not None or port == 0 or "%" in host):
        raise ValueError("base_url 协议、主机或端口无效，不可内嵌凭据")
    if url.scheme == "http":
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        loopback = host == "localhost" or (address and address.is_loopback)
        private = host == "host.docker.internal" or (
            address and any(address in network for network in _PRIVATE_NETWORKS)
        )
        if not loopback and not (allow_insecure and private and not authenticated):
            raise ValueError("非回环 HTTP 仅接受显式允许且未配置密钥的私网端点")
    return value.rstrip("/")


def _endpoint(raw, max_tokens):
    if not isinstance(raw, dict) or raw.keys() - _ENDPOINT_FIELDS:
        raise ValueError("端点配置含非预期字段或格式错误")
    endpoint_id = raw.get("id")
    model = raw.get("model")
    if not isinstance(endpoint_id, str) or not _ENDPOINT_ID.fullmatch(endpoint_id):
        raise ValueError("端点 id 无效")
    if (not isinstance(model, str) or not model.strip() or len(model) > 256
            or any(ord(c) < 32 for c in model)):
        raise ValueError("端点 model 必须是非空模型标识")
    protocol = raw.get("protocol")
    if protocol not in ("ollama", "chat_completions"):
        raise ValueError("protocol 必须是 ollama 或 chat_completions")
    allow_insecure = raw.get("allow_insecure_http", False)
    if type(allow_insecure) is not bool:
        raise ValueError("allow_insecure_http 必须是布尔值")
    key_env = _env_name(raw["api_key_env"]) if "api_key_env" in raw else None
    return Endpoint(
        id=endpoint_id, model=model, protocol=protocol,
        base_url=_base_url(raw.get("base_url"), allow_insecure, key_env is not None),
        api_key_env=key_env,
        max_tokens=_integer(raw, "max_tokens", max_tokens, max_tokens),
        allow_insecure_http=allow_insecure,
    )


def load_config(path: str | Path) -> Config:
    """Validate an endpoint registry. No network requests or credential reads."""
    with Path(path).open("rb") as stream:
        contents = stream.read(1_048_577)
    if len(contents) > 1_048_576:
        raise ValueError("配置文件超过 1 MiB")
    try:
        raw = json.loads(contents.decode("utf-8"), object_pairs_hook=_object)
    except (UnicodeError, json.JSONDecodeError, RecursionError):
        raise ValueError("配置文件必须是有效的 UTF-8 JSON") from None
    if not isinstance(raw, dict) or raw.keys() - _CONFIG_FIELDS:
        raise ValueError("网关配置含非预期字段或格式错误")
    models = raw.get("endpoints")
    if not isinstance(models, list) or not 1 <= len(models) <= 256:
        raise ValueError("endpoints 需要 1 到 256 项配置")
    max_tokens = _integer(raw, "max_tokens", 4096, 1_048_576)
    endpoints = {}
    for item in models:
        endpoint = _endpoint(item, max_tokens)
        if endpoint.id in endpoints:
            raise ValueError("端点 id 不可重复")
        endpoints[endpoint.id] = endpoint
    timeout = raw.get("timeout_seconds", 60)
    if type(timeout) not in (float, int) or not 0 < timeout <= 600 or not math.isfinite(timeout):
        raise ValueError("timeout_seconds 须大于 0 且不超过 600 秒")
    return Config(
        endpoints=endpoints, revision=hashlib.sha256(contents).hexdigest(),
        token_env=_env_name(raw["token_env"]) if "token_env" in raw else None,
        timeout_seconds=float(timeout), max_tokens=max_tokens,
        max_concurrency=_integer(raw, "max_concurrency", 4, 256),
        max_request_bytes=_integer(raw, "max_request_bytes", 1_048_576, 16_777_216),
        max_response_bytes=_integer(raw, "max_response_bytes", 4_194_304, 33_554_432),
    )
