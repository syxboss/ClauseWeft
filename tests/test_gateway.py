"""Black-box gateway contracts with two local HTTP provider fixtures."""

from contextlib import contextmanager
import hashlib
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import unittest
from urllib.parse import urlsplit


class Provider(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.put((self.path, self.headers.get("Authorization"), payload))
        name = payload["model"]
        if name == "delayed":
            self.server.started.set()
            self.server.release.wait(4)
        if name == "redirect":
            self.send_response(307)
            self.send_header("Location", "/unexpected")
            self.end_headers()
            return
        if self.path == "/api/chat":
            if payload.get("stream"):
                chunks = [
                    {"model": name, "done": False, "message": {"role": "assistant", "content": "hello "}},
                    {"model": name, "done": True, "done_reason": "stop",
                     "message": {"role": "assistant", "content": "stream"},
                     "prompt_eval_count": 5, "eval_count": 3},
                ]
                body = ("\n".join(json.dumps(c) for c in chunks) + "\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/x-ndjson")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            data = {"model": name, "done": True, "done_reason": "stop",
                    "message": {"role": "assistant", "content": "fixture reply"},
                    "prompt_eval_count": 5, "eval_count": 3}
            if payload.get("tools"):
                data["message"] = {
                    "role": "assistant", "content": "",
                    "tool_calls": [{"function": {"name": payload["tools"][0]["function"]["name"],
                                                 "arguments": {"query": "refund"}}}],
                }
        elif self.path == "/v1/chat/completions":
            if payload.get("stream"):
                chunks = [
                    {"id": "upstream-stream", "object": "chat.completion.chunk", "model": name,
                     "choices": [{"index": 0, "delta": {"role": "assistant", "content": "hello "}, "finish_reason": None}]},
                    {"id": "upstream-stream", "object": "chat.completion.chunk", "model": name,
                     "choices": [{"index": 0, "delta": {"content": "stream"}, "finish_reason": "stop"}],
                     "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8,
                               "prompt_tokens_details": {"cached_tokens": 2},
                               "completion_tokens_details": {"reasoning_tokens": 1}}},
                ]
                body = ("".join("data: " + json.dumps(c) + "\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            data = {"id": "upstream-id", "model": name,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": "fixture reply"},
                                 "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 3, "total_tokens": 8}}
            if payload.get("tools"):
                data["choices"][0]["message"] = {
                    "role": "assistant", "content": None,
                    "tool_calls": [{"id": "call_1", "type": "function",
                                    "function": {"name": payload["tools"][0]["function"]["name"],
                                                 "arguments": '{"query":"refund"}'}}],
                }
                data["choices"][0]["finish_reason"] = "tool_calls"
            if payload["messages"][0].get("content") == "with details":
                data["usage"]["prompt_tokens_details"] = {"cached_tokens": 3}
                data["usage"]["completion_tokens_details"] = {"reasoning_tokens": 2}
        else:
            self.send_error(404)
            return
        if payload["messages"][0]["content"] == "no usage":
            data.pop("usage", None)
        if payload["messages"][0]["content"] == "inconsistent usage":
            data["usage"]["total_tokens"] = 0
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("X-Request-ID", "upstream-request")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@contextmanager
def gateway(config, directory, with_token=True):
    path = Path(directory) / "gateway.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    env = dict(os.environ)
    # Only synthetic fixture values go into the child.
    for key in ("CLAUSEWEFT_GATEWAY_TOKEN", "FIXTURE_GATEWAY_TOKEN", "FIXTURE_PROVIDER_TOKEN"):
        env.pop(key, None)
    if with_token:
        env["FIXTURE_GATEWAY_TOKEN"] = "client-fixture-token"
    env["FIXTURE_PROVIDER_TOKEN"] = "provider-fixture-token"
    source = Path(__file__).resolve().parents[1] / "src"
    if source.is_dir():
        env["PYTHONPATH"] = str(source)
    command = ([env["TEST_GATEWAY_BINARY"]] if env.get("TEST_GATEWAY_BINARY")
               else [sys.executable, "-m", "clauseweft.gateway"])
    process = subprocess.Popen([*command, "--config", str(path), "--port", "0"],
                               cwd=directory, env=env, text=True,
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        line = process.stdout.readline()
        if not line:
            raise RuntimeError("Gateway startup failed: " + process.stderr.read())
        yield urlsplit(line.strip().split()[-1]).port
    finally:
        process.terminate()
        process.communicate(timeout=10)


def request(port, path, body=None, headers=None):
    connection = HTTPConnection("127.0.0.1", port, timeout=8)
    payload = json.dumps(body).encode() if body is not None else None
    try:
        connection.request("POST" if body is not None else "GET", path, payload,
                           {"Content-Type": "application/json", **(headers or {})})
        response = connection.getresponse()
        return response.status, dict(response.getheaders()), json.loads(response.read())
    finally:
        connection.close()


class GatewayContracts(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        cls.addClassCleanup(upstream.server_close)
        cls.upstream = upstream
        upstream.calls = queue.Queue()
        upstream.started, upstream.release = threading.Event(), threading.Event()
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        cls.addClassCleanup(upstream.shutdown)
        base = f"http://127.0.0.1:{upstream.server_port}"
        config = {
            "token_env": "FIXTURE_GATEWAY_TOKEN", "max_concurrency": 1, "timeout_seconds": 5,
            "endpoints": [
                {"id": "a", "model": "provider-a", "protocol": "ollama", "base_url": base},
                *[{"id": key, "model": name, "protocol": "chat_completions",
                   "base_url": base + "/v1", "api_key_env": "FIXTURE_PROVIDER_TOKEN"}
                  for key, name in (("b", "provider-b"), ("slow", "delayed"), ("move", "redirect"))],
            ],
        }
        service = gateway(config, cls.temp.name)
        cls.port = service.__enter__()
        cls.addClassCleanup(service.__exit__, None, None, None)
        cls.auth = {"Authorization": "Bearer client-fixture-token"}

    def chat(self, name, *, headers=None, **extra):
        return request(self.port, "/v1/chat/completions",
                       {"model": name, "messages": [{"role": "user", "content": "hello"}],
                        "max_tokens": 12, **extra}, self.auth if headers is None else headers)

    def test_health_catalog_and_revision_guard(self):
        self.assertEqual(request(self.port, "/healthz")[0], 200)
        self.assertEqual(request(self.port, "/v1/models")[0], 401)
        status, headers, catalog = request(self.port, "/v1/models", headers=self.auth)
        self.assertEqual(status, 200)
        self.assertEqual({model["id"] for model in catalog["data"]}, {"a", "b", "slow", "move"})
        self.assertNotIn("base_url", json.dumps(catalog))
        self.assertRegex(headers["ETag"], r'^"[0-9a-f]{64}"$')
        self.assertEqual(self.chat("a", headers={**self.auth, "If-Match": '"old"'})[0], 412)
        self.assertTrue(self.upstream.calls.empty())

    def test_both_adapters_and_credential_separation(self):
        _, catalog_headers, _ = request(self.port, "/v1/models", headers=self.auth)
        for name, path, auth in (("a", "/api/chat", None), ("b", "/v1/chat/completions", "Bearer provider-fixture-token")):
            with self.subTest(alias=name):
                status, headers, reply = self.chat(name, headers={**self.auth, "If-Match": catalog_headers["ETag"]})
                self.assertEqual(status, 200, reply)
                self.assertTrue(headers["X-Request-ID"])
                self.assertEqual(reply["model"], name)
                self.assertEqual(reply["choices"][0]["message"]["content"], "fixture reply")
                self.assertEqual(reply["usage"]["total_tokens"], 8)
                metadata = reply["gateway_metadata"]
                self.assertEqual(metadata["upstream_model"], "provider-" + name)
                self.assertEqual(metadata["upstream_request_id"], "upstream-request")
                if name == "b":
                    self.assertEqual(metadata["upstream_completion_id"], "upstream-id")
                actual_path, actual_auth, sent = self.upstream.calls.get(timeout=1)
                self.assertEqual((actual_path, actual_auth), (path, auth))
                self.assertEqual(sent["model"], "provider-" + name)
                self.assertIs(sent["stream"], False)
                self.assertEqual(sent.get("options", {}).get("num_predict", sent.get("max_tokens")), 12)

    def test_token_limit_negotiation_and_ollama_json_format(self):
        headers = {**self.auth, "X-ClauseWeft-Token-Limit": "clamp", "X-ClauseWeft-Output-Format": "json"}
        _, _, models = request(self.port, "/v1/models", headers=self.auth)
        limits = {m["id"]: m["max_output_tokens"] for m in models["data"]}
        self.assertEqual(self.chat("a", max_tokens=limits["a"] + 1)[0], 400)
        self.assertTrue(self.upstream.calls.empty())
        for alias in ("a", "b"):
            limit = limits[alias]
            status, _, reply = self.chat(alias, max_tokens=limit + 1, headers=headers)
            self.assertEqual(status, 200, reply)
            self.assertEqual(reply["gateway_metadata"]["max_output_tokens"], limit)
            sent = self.upstream.calls.get(timeout=1)[2]
            if alias == "a":
                self.assertEqual(sent["format"], "json")
                self.assertEqual(sent["options"]["temperature"], 0)
                self.assertEqual(sent["options"]["num_predict"], limit)
            else:
                self.assertNotIn("response_format", sent)
                self.assertNotIn("format", sent)
                self.assertEqual(sent["max_tokens"], limit)

    def test_missing_or_inconsistent_usage_remains_unknown(self):
        for content in ("no usage", "inconsistent usage"):
            status, _, reply = self.chat("b", messages=[{"role": "user", "content": content}])
            self.assertEqual(status, 200)
            self.assertNotIn("usage", reply)
            self.assertEqual(self.upstream.calls.get(timeout=1)[2]["model"], "provider-b")

    def test_rejected_requests_do_not_reach_upstream(self):
        self.assertEqual(self.chat("b", headers={})[0], 401)
        self.assertEqual(self.chat("absent")[0], 404)
        for option in (
            {"stream": "yes"},
            {"base_url": "http://localhost"},
            {"max_tokens": -1},
            {"tool_choice": "auto"},
            {"response_format": {"type": "xml"}},
        ):
            self.assertEqual(self.chat("a", **option)[0], 400)
        self.assertTrue(self.upstream.calls.empty())

    def test_native_tool_calling_response_format_and_token_details(self):
        tools = [{"type": "function", "function": {
            "name": "search_requirements",
            "description": "Search requirements",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
        }}]
        fmt = {"type": "json_schema", "json_schema": {"name": "plan", "schema": {"type": "object"}}}
        for alias in ("a", "b"):
            status, _, reply = self.chat(alias, tools=tools, tool_choice="auto", response_format=fmt)
            self.assertEqual(status, 200, reply)
            choice = reply["choices"][0]
            self.assertEqual(choice["finish_reason"], "tool_calls")
            call = choice["message"]["tool_calls"][0]
            self.assertEqual(call["function"]["name"], "search_requirements")
            self.assertEqual(json.loads(call["function"]["arguments"]), {"query": "refund"})
            sent = self.upstream.calls.get(timeout=1)[2]
            self.assertEqual(sent["tools"], tools)
            if alias == "a":
                self.assertEqual(sent["format"], {"type": "object"})
            else:
                self.assertEqual(sent["response_format"], fmt)
                self.assertEqual(sent["tool_choice"], "auto")
        # Multi-turn follow-up containing assistant tool_calls + tool result message
        follow_messages = [
            {"role": "user", "content": "find refund"},
            {"role": "assistant", "content": None, "tool_calls": [
                {"id": "call_1", "type": "function", "function": {"name": "search_requirements", "arguments": '{"query":"refund"}'}}
            ]},
            {"role": "tool", "tool_call_id": "call_1", "content": '{"items":[]}'},
        ]
        status, _, _ = self.chat("b", messages=follow_messages, tools=tools)
        self.assertEqual(status, 200)
        self.upstream.calls.get(timeout=1)
        # Cached and reasoning token details
        status, _, detailed = self.chat("b", messages=[{"role": "user", "content": "with details"}])
        self.assertEqual(status, 200)
        self.assertEqual(detailed["usage"]["cached_tokens"], 3)
        self.assertEqual(detailed["usage"]["reasoning_tokens"], 2)
        self.upstream.calls.get(timeout=1)

    def test_streaming_sse_for_both_protocols(self):
        for alias in ("a", "b"):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=8)
            try:
                body = json.dumps({
                    "model": alias, "messages": [{"role": "user", "content": "hello"}],
                    "max_tokens": 12, "stream": True,
                }).encode()
                conn.request("POST", "/v1/chat/completions", body,
                             {"Content-Type": "application/json", **self.auth})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                self.assertTrue(resp.getheader("Content-Type", "").startswith("text/event-stream"))
                raw = resp.read().decode("utf-8")
            finally:
                conn.close()
            self.upstream.calls.get(timeout=1)
            events = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
            self.assertEqual(events[-1], "[DONE]")
            parsed = [json.loads(item) for item in events[:-1]]
            text = "".join(c["choices"][0]["delta"].get("content", "") for c in parsed if c.get("choices"))
            self.assertEqual(text, "hello stream")
            self.assertEqual(parsed[-1]["usage"]["total_tokens"], 8)
            if alias == "b":
                self.assertEqual(parsed[-1]["usage"]["cached_tokens"], 2)
                self.assertEqual(parsed[-1]["usage"]["reasoning_tokens"], 1)

    def test_preflight_needs_no_port_or_credentials(self):
        config = Path(self.temp.name) / "gateway.json"
        binary = os.environ.get("TEST_GATEWAY_BINARY")
        command = [binary] if binary else [sys.executable, "-m", "clauseweft.gateway"]
        env = os.environ.copy()
        env.pop("FIXTURE_GATEWAY_TOKEN", None)
        args = ["--check-config", "--port", str(self.port), "--config"]
        result = subprocess.run(command + args + [str(config)], capture_output=True,
                                text=True, env=env, check=True, timeout=10)
        expected = hashlib.sha256(config.read_bytes()).hexdigest()
        self.assertEqual(json.loads(result.stdout), {"status": "valid", "revision": expected,
                                                    "endpoint_count": 4})
        self.assertEqual(request(self.port, "/v1/models", headers=self.auth)[1]["ETag"],
                         f'"{expected}"')
        invalid = Path(self.temp.name) / "invalid.json"
        invalid.write_text('{"endpoints":[]}', encoding="utf-8")
        result = subprocess.run(command + args + [str(invalid)], capture_output=True,
                                text=True, env=env, timeout=10)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(self.upstream.calls.empty())

    def test_tokenless_local_profile_and_explicit_missing_token(self):
        config = {"endpoints": [{"id": "a", "model": "test", "protocol": "ollama",
                                 "base_url": "http://127.0.0.1:11434"}]}
        with tempfile.TemporaryDirectory() as directory:
            with gateway(config, directory, with_token=False) as port:
                self.assertEqual(request(port, "/v1/models")[0], 200)
            config["token_env"] = "FIXTURE_GATEWAY_TOKEN"
            with self.assertRaisesRegex(RuntimeError, "Gateway startup failed"):
                with gateway(config, directory, with_token=False):
                    self.fail("missing explicitly configured token allowed startup")

    def test_redirect_is_not_followed(self):
        self.assertEqual(self.chat("move")[0], 502)
        self.assertEqual(self.upstream.calls.get(timeout=1)[2]["model"], "redirect")
        self.assertTrue(self.upstream.calls.empty())

    def test_busy_request_keeps_health_available(self):
        result = []
        thread = threading.Thread(target=lambda: result.append(self.chat("slow")[0]))
        thread.start()
        try:
            self.assertTrue(self.upstream.started.wait(2))
            self.assertEqual(self.chat("a")[0], 429)
            self.assertEqual(request(self.port, "/healthz")[0], 200)
        finally:
            self.upstream.release.set()
            thread.join(7)
        self.assertEqual(result, [200])
        self.assertEqual(self.upstream.calls.get(timeout=1)[2]["model"], "delayed")
        self.assertTrue(self.upstream.calls.empty())


class ExtendedProvider(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.server.calls.put((
            self.path,
            {
                "Authorization": self.headers.get("Authorization"),
                "x-api-key": self.headers.get("x-api-key"),
                "anthropic-version": self.headers.get("anthropic-version"),
                "x-goog-api-key": self.headers.get("x-goog-api-key"),
            },
            payload,
        ))
        if self.path == "/v1/messages":
            if payload.get("stream"):
                events = [
                    {"type": "message_start", "message": {
                        "id": "msg_stream_1", "model": payload["model"],
                        "usage": {"input_tokens": 10, "cache_read_input_tokens": 4},
                    }},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "anth "}},
                    {"type": "content_block_delta", "index": 0,
                     "delta": {"type": "text_delta", "text": "stream"}},
                    {"type": "message_delta", "delta": {"stop_reason": "end_turn"},
                     "usage": {"output_tokens": 6}},
                    {"type": "message_stop"},
                ]
                body = "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            content = [{"type": "text", "text": "anthropic reply"}]
            stop_reason = "end_turn"
            if payload.get("tools"):
                content = [{
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": payload["tools"][0]["name"],
                    "input": {"query": "sla"},
                }]
                stop_reason = "tool_use"
            data = {
                "id": "msg_01",
                "type": "message",
                "role": "assistant",
                "model": payload["model"],
                "content": content,
                "stop_reason": stop_reason,
                "usage": {"input_tokens": 10, "cache_read_input_tokens": 5, "output_tokens": 4},
            }
        elif self.path.startswith("/v1beta/models/"):
            if ":streamGenerateContent" in self.path:
                chunks = [
                    {"candidates": [{"content": {"role": "model", "parts": [{"text": "gemini "}]} }]},
                    {"candidates": [{"content": {"role": "model", "parts": [{"text": "stream"}]},
                                     "finishReason": "STOP"}],
                     "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 5,
                                       "cachedContentTokenCount": 3, "thoughtsTokenCount": 2,
                                       "totalTokenCount": 19}},
                ]
                body = "".join("data: " + json.dumps(c) + "\n\n" for c in chunks).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            parts = [{"text": "gemini reply"}]
            if payload.get("tools"):
                decl = payload["tools"][0]["functionDeclarations"][0]
                parts = [{"functionCall": {"name": decl["name"], "args": {"query": "sla"}}}]
            data = {
                "responseId": "gem_resp_1",
                "modelVersion": "gemini-2.5-pro",
                "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP"}],
                "usageMetadata": {
                    "promptTokenCount": 12,
                    "candidatesTokenCount": 5,
                    "cachedContentTokenCount": 4,
                    "thoughtsTokenCount": 3,
                    "totalTokenCount": 20,
                },
            }
        elif self.path == "/api/chat":
            data = {
                "model": payload["model"], "done": True, "done_reason": "stop",
                "message": {"role": "assistant", "content": "ollama vision ok"},
                "prompt_eval_count": 8, "eval_count": 2,
            }
        elif self.path == "/v1/chat/completions":
            data = {
                "id": "oai_1", "model": payload["model"],
                "choices": [{"index": 0, "message": {"role": "assistant", "content": "oai vision ok"},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 8, "completion_tokens": 2, "total_tokens": 10},
            }
        else:
            self.send_error(404)
            return
        body = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("X-Request-ID", "ext-req-1")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class GatewayExtendedProtocolsAndMultimodal(unittest.TestCase):
    PNG_1X1 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )

    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.temp.cleanup)
        upstream = ThreadingHTTPServer(("127.0.0.1", 0), ExtendedProvider)
        cls.addClassCleanup(upstream.server_close)
        cls.upstream = upstream
        upstream.calls = queue.Queue()
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        cls.addClassCleanup(upstream.shutdown)
        base = f"http://127.0.0.1:{upstream.server_port}"
        config = {
            "token_env": "FIXTURE_GATEWAY_TOKEN",
            "timeout_seconds": 5,
            "endpoints": [
                {"id": "claude-sonnet", "model": "claude-sonnet-4-5", "protocol": "anthropic",
                 "base_url": base + "/v1", "api_key_env": "FIXTURE_PROVIDER_TOKEN"},
                {"id": "gemini-pro", "model": "gemini-2.5-pro", "protocol": "gemini",
                 "base_url": base + "/v1beta", "api_key_env": "FIXTURE_PROVIDER_TOKEN"},
                {"id": "ollama-vl", "model": "qwen-vl", "protocol": "ollama", "base_url": base},
                {"id": "oai-vl", "model": "gpt-4o", "protocol": "chat_completions",
                 "base_url": base + "/v1", "api_key_env": "FIXTURE_PROVIDER_TOKEN"},
            ],
        }
        service = gateway(config, cls.temp.name)
        cls.port = service.__enter__()
        cls.addClassCleanup(service.__exit__, None, None, None)
        cls.auth = {"Authorization": "Bearer client-fixture-token"}

    def test_anthropic_and_gemini_completion_tools_and_usage(self):
        # Anthropic one-shot + system extraction + x-api-key + cached_tokens
        status, _, reply = request(
            self.port, "/v1/chat/completions",
            {
                "model": "claude-sonnet",
                "messages": [
                    {"role": "system", "content": "Be concise."},
                    {"role": "user", "content": "Check SLA"},
                ],
                "max_tokens": 64,
            },
            self.auth,
        )
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["choices"][0]["message"]["content"], "anthropic reply")
        self.assertEqual(reply["usage"], {
            "prompt_tokens": 15, "completion_tokens": 4, "total_tokens": 19, "cached_tokens": 5,
        })
        path, hdrs, sent = self.upstream.calls.get(timeout=1)
        self.assertEqual(path, "/v1/messages")
        self.assertEqual(hdrs["x-api-key"], "provider-fixture-token")
        self.assertEqual(hdrs["anthropic-version"], "2023-06-01")
        self.assertIsNone(hdrs["Authorization"])
        self.assertEqual(sent["system"], "Be concise.")
        self.assertEqual(sent["messages"], [{"role": "user", "content": "Check SLA"}])

        # Gemini one-shot + x-goog-api-key + json_schema + cached & reasoning tokens
        fmt = {"type": "json_schema", "json_schema": {"name": "out", "schema": {"type": "object"}}}
        status, _, reply = request(
            self.port, "/v1/chat/completions",
            {
                "model": "gemini-pro",
                "messages": [
                    {"role": "system", "content": "Output JSON"},
                    {"role": "user", "content": "Check SLA"},
                ],
                "max_tokens": 64,
                "response_format": fmt,
            },
            self.auth,
        )
        self.assertEqual(status, 200, reply)
        self.assertEqual(reply["choices"][0]["message"]["content"], "gemini reply")
        self.assertEqual(reply["usage"], {
            "prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20,
            "cached_tokens": 4, "reasoning_tokens": 3,
        })
        path, hdrs, sent = self.upstream.calls.get(timeout=1)
        self.assertEqual(path, "/v1beta/models/gemini-2.5-pro:generateContent")
        self.assertEqual(hdrs["x-goog-api-key"], "provider-fixture-token")
        self.assertIsNone(hdrs["Authorization"])
        self.assertEqual(sent["systemInstruction"], {"parts": [{"text": "Output JSON"}]})
        self.assertEqual(sent["generationConfig"]["responseMimeType"], "application/json")

        # Tool calls + multi-turn tool_result roundtrip for both Anthropic and Gemini
        tools = [{"type": "function", "function": {
            "name": "lookup_sla",
            "description": "Lookup SLA",
            "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
        }}]
        for alias in ("claude-sonnet", "gemini-pro"):
            status, _, reply = request(
                self.port, "/v1/chat/completions",
                {
                    "model": alias,
                    "messages": [{"role": "user", "content": "Find SLA"}],
                    "max_tokens": 64,
                    "tools": tools,
                    "tool_choice": "required",
                },
                self.auth,
            )
            self.assertEqual(status, 200, reply)
            self.assertEqual(reply["choices"][0]["finish_reason"], "tool_calls")
            tc = reply["choices"][0]["message"]["tool_calls"][0]
            self.assertEqual(tc["function"]["name"], "lookup_sla")
            self.assertEqual(json.loads(tc["function"]["arguments"]), {"query": "sla"})
            self.upstream.calls.get(timeout=1)

            # Follow-up with assistant tool_calls + tool response
            followup = [
                {"role": "user", "content": "Find SLA"},
                {"role": "assistant", "content": None, "tool_calls": [tc]},
                {"role": "tool", "tool_call_id": tc["id"], "content": '{"sla":"99.9%"}'},
            ]
            status, _, _ = request(
                self.port, "/v1/chat/completions",
                {"model": alias, "messages": followup, "max_tokens": 64, "tools": tools},
                self.auth,
            )
            self.assertEqual(status, 200)
            self.upstream.calls.get(timeout=1)

    def test_anthropic_and_gemini_sse_streaming(self):
        for alias, expected_text in (("claude-sonnet", "anth stream"), ("gemini-pro", "gemini stream")):
            conn = HTTPConnection("127.0.0.1", self.port, timeout=8)
            try:
                body = json.dumps({
                    "model": alias,
                    "messages": [{"role": "user", "content": "stream please"}],
                    "max_tokens": 32,
                    "stream": True,
                }).encode()
                conn.request("POST", "/v1/chat/completions", body,
                             {"Content-Type": "application/json", **self.auth})
                resp = conn.getresponse()
                self.assertEqual(resp.status, 200)
                raw = resp.read().decode("utf-8")
            finally:
                conn.close()
            self.upstream.calls.get(timeout=1)
            events = [line[6:] for line in raw.splitlines() if line.startswith("data: ")]
            self.assertEqual(events[-1], "[DONE]")
            chunks = [json.loads(e) for e in events[:-1]]
            text = "".join(c["choices"][0]["delta"].get("content", "") for c in chunks if c.get("choices"))
            self.assertEqual(text, expected_text)
            self.assertIsNotNone(chunks[-1].get("usage"))

    def test_multimodal_data_uri_conversion_and_ssrf_url_rejection(self):
        # Reject remote HTTP/HTTPS URLs to prevent SSRF
        bad_msg = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect"},
                {"type": "image_url", "image_url": {"url": "https://evil.example.com/pixel.png"}},
            ],
        }]
        status, _, err = request(
            self.port, "/v1/chat/completions",
            {"model": "oai-vl", "messages": bad_msg, "max_tokens": 32},
            self.auth,
        )
        self.assertEqual(status, 400)
        self.assertEqual(err["error"]["code"], "invalid_messages")
        self.assertTrue(self.upstream.calls.empty())

        # Valid base64 data URI converts cleanly across all 4 protocols
        good_msg = [{
            "role": "user",
            "content": [
                {"type": "text", "text": "Inspect UI screenshot"},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{self.PNG_1X1}"}},
            ],
        }]
        for alias in ("claude-sonnet", "gemini-pro", "ollama-vl", "oai-vl"):
            status, _, reply = request(
                self.port, "/v1/chat/completions",
                {"model": alias, "messages": good_msg, "max_tokens": 32},
                self.auth,
            )
            self.assertEqual(status, 200, reply)
            _, _, sent = self.upstream.calls.get(timeout=1)
            if alias == "claude-sonnet":
                img_block = sent["messages"][0]["content"][1]
                self.assertEqual(img_block["type"], "image")
                self.assertEqual(img_block["source"]["media_type"], "image/png")
                self.assertEqual(img_block["source"]["data"], self.PNG_1X1)
            elif alias == "gemini-pro":
                inline = sent["contents"][0]["parts"][1]["inlineData"]
                self.assertEqual(inline["mimeType"], "image/png")
                self.assertEqual(inline["data"], self.PNG_1X1)
            elif alias == "ollama-vl":
                self.assertEqual(sent["messages"][0]["images"], [self.PNG_1X1])
            else:
                self.assertEqual(sent["messages"][0]["content"][1]["type"], "image_url")


if __name__ == "__main__":
    unittest.main()
