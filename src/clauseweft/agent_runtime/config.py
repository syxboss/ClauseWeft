"""Trusted, deployment-owned registrations for external coding agents."""

from dataclasses import dataclass
from pathlib import PurePosixPath
import re


@dataclass(frozen=True)
class Runner:
    kind: str
    image: str
    executable: str
    token_env: str
    timeout_seconds: int


class AgentRuntimeError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def load_runners(config):
    if not isinstance(config, dict) or len(config) > 32:
        raise ValueError("agent_runners 应为最多 32 项的映射")
    runners = {}
    for name, entry in config.items():
        if not isinstance(name, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name
        ):
            raise ValueError("执行器 ID 无效")
        if not isinstance(entry, dict) or set(entry) - set(Runner.__dataclass_fields__):
            raise ValueError("执行器配置格式或字段无效")
        kind, image, executable, variable = (
            entry.get(k) for k in ("kind", "image", "executable", "token_env")
        )
        if not all(isinstance(x, str) for x in (kind, image, executable, variable)):
            raise ValueError("执行器缺少配置")
        if kind not in ("claude", "deepseek") or not re.fullmatch(
            "sha256:[0-9a-f]{64}", image
        ):
            raise ValueError("请指定受支持的 Agent 和本地镜像 ID")
        if (
            not re.fullmatch(r"/(?:[a-zA-Z0-9._+-]+/)*[a-zA-Z0-9._+-]+", executable)
            or len(executable) > 256
            or len(PurePosixPath(executable).parts) < 3
            or any(part in {".", ".."} for part in executable.split("/"))
            or PurePosixPath(executable).parts[1] not in {"usr", "opt", "bin"}
        ):
            raise ValueError("执行器须登记镜像内的绝对程序路径")
        if not re.fullmatch(r"[A-Z_][A-Z0-9_]{0,95}", variable) or variable.startswith(
            ("CLAUSEWEFT_", "DOCKER_")
        ):
            raise ValueError("请为执行器登记独立的供应商凭据变量")
        timeout = entry.get("timeout_seconds", 180)
        if type(timeout) is not int or not 1 <= timeout <= 900:
            raise ValueError("执行器时限须为 1 至 900 秒")
        runners[name] = Runner(kind, image, executable, variable, timeout)
    return runners
