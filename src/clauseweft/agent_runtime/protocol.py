"""Bounded provider events. Never retain intermediate text, tool arguments or stderr."""

import re

from clauseweft.workspace.credentials import redact
from .config import AgentRuntimeError


class Response:
    def __init__(self, kind, key, on_event):
        self.kind = kind
        self.key = key
        self.on_event = on_event
        self.session = None
        self.text = None
        self.usage = {}
        self.events = 0

    def emit(self, name, tool=None):
        if self.events < 256:
            event = {"type": name}
            if tool is not None:
                event["tool"] = (
                    tool
                    if tool in {"Read", "Glob", "Grep", "Edit", "Write"}
                    else "tool"
                )
            self.on_event(event)
            self.events += 1

    def consume(self, msg):
        if not isinstance(msg, dict) or not isinstance(msg.get("type"), str):
            raise AgentRuntimeError("agent_protocol", "Agent 返回了无效事件")
        kind = msg["type"]
        if self.text is not None:
            if kind in ("result", "final", "error"):
                raise AgentRuntimeError("agent_protocol", "Agent 返回了冲突的结束事件")
            return
        if self.kind == "claude":
            sid = msg.get("session_id")
            if kind == "assistant":
                for block in msg.get("message", {}).get("content", []):
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        self.emit("tool_use", block.get("name") or "tool")
                self.emit("progress")
            if kind == "result":
                if msg.get("is_error") is not False or msg.get("subtype") != "success":
                    raise AgentRuntimeError("agent_failed", "Claude Code 未能完成任务")
                self.finish(msg.get("result"))
                self.count(
                    msg.get("usage"),
                    {
                        "input_tokens": "input_tokens",
                        "output_tokens": "output_tokens",
                        "cache_read_input_tokens": "cache_read_tokens",
                        "cache_creation_input_tokens": "cache_write_tokens",
                    },
                )
        else:
            sid = msg.get("sessionId") if kind == "session" else None
            if kind == "status":
                self.emit("progress")
                if msg.get("phase") == "step_end":
                    self.count(
                        msg.get("usage"),
                        {
                            "inputTokens": "input_tokens",
                            "outputTokens": "output_tokens",
                            "cacheReadTokens": "cache_read_tokens",
                            "cacheWriteTokens": "cache_write_tokens",
                        },
                    )
            if kind == "tool_call":
                self.emit("tool_use", msg.get("tool") or "tool")
            if kind == "error" or (
                kind == "final" and msg.get("reason", {}).get("kind") != "completed"
            ):
                raise AgentRuntimeError("agent_failed", "DeepSeek Harness 未能完成任务")
            if kind == "final":
                self.finish(msg.get("text"))
        if (
            isinstance(sid, str)
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", sid)
            and self.key not in sid
            and redact(sid) == sid
        ):
            self.session = sid

    def count(self, values, names):
        if not isinstance(values, dict):
            return
        for provider_field, field in names.items():
            value = values.get(provider_field)
            if type(value) is int and 0 <= value <= 10**10:
                self.usage[field] = self.usage.get(field, 0) + value

    def finish(self, value):
        if (
            not isinstance(value, str)
            or not value.strip()
            or len(value.encode()) > 131072
        ):
            raise AgentRuntimeError("agent_protocol", "Agent 结果为空或超过限制")
        self.text = redact(value.replace(self.key, "[redacted]"))

    def result(self):
        if self.text is None:
            raise AgentRuntimeError("agent_protocol", "缺少 Agent 成功结束回执")
        return {"text": self.text, "session_id": self.session, "usage": self.usage}
