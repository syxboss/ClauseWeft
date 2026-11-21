"""External CLI adapter contracts and optional real-container boundary checks."""

import base64
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from clauseweft import agent_runtime as runtime
from clauseweft.agent_runtime import AgentRuntimeError, load_runners, run_agent
from clauseweft.agent_runtime.protocol import Response


def runner(kind="claude", image=None, seconds=10):
    return load_runners(
        {
            "worker": {
                "kind": kind,
                "token_env": "OFFLINE_TEST_PROVIDER_KEY",
                "image": image or "sha256:" + "a" * 64,
                "executable": "/opt/bin/cli",
                "timeout_seconds": seconds,
            }
        }
    )["worker"]


class RuntimeContract(unittest.TestCase):
    def test_only_registered_local_images_and_programs(self):
        for field, value in (
            ("executable", "./change"),
            ("executable", "/."),
            ("executable", "/work/app"),
            ("executable", "/usr/bin/../x"),
            ("kind", "shell"),
            ("image", "latest"),
            ("timeout_seconds", False),
            ("token_env", "CLAUSEWEFT_CONSOLE_TOKEN"),
            ("command", "echo"),
        ):
            config = vars(runner()) | {field: value}
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                load_runners({"cli": config})
        self.assertNotIn("Bash", runtime._cli(runner()))
        self.assertIn("--restricted", runtime._cli(runner()))
        self.assertIn("read-only", runtime._cli(runner("deepseek")))

    def test_protocol_filters_sensitive_values_and_suppresses_partial_success(self):
        events = []
        response = Response("claude", "fixture-key", events.append)
        response.consume(
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "name": "Read",
                            "input": {"text": "private material"},
                        }
                    ]
                },
            }
        )
        with self.assertRaises(AgentRuntimeError):
            response.result()
        response.consume(
            {
                "type": "result",
                "is_error": False,
                "subtype": "success",
                "session_id": "4d85f4",
                "result": "answer fixture-key",
                "usage": {"input_tokens": 12, "output_tokens": True},
            }
        )
        self.assertEqual(
            response.result(),
            {
                "text": "answer [redacted]",
                "session_id": "4d85f4",
                "usage": {"input_tokens": 12},
            },
        )
        self.assertEqual(
            events, [{"type": "tool_use", "tool": "Read"}, {"type": "progress"}]
        )
        with self.assertRaises(AgentRuntimeError):
            response.consume({"type": "result"})

    def test_deepseek_final_reason_and_accumulated_usage(self):
        response = Response("deepseek", "fixture-key", lambda e: None)
        response.consume({"type": "session", "sessionId": "session-1"})
        for n in (2, 7):
            response.consume(
                {"type": "status", "phase": "step_end", "usage": {"inputTokens": n}}
            )
        response.consume(
            {"type": "final", "reason": {"kind": "completed"}, "text": "done"}
        )
        self.assertEqual(response.result()["usage"], {"input_tokens": 9})
        for kind in ("error", "cancelled", "max-tokens"):
            with self.subTest(kind=kind), self.assertRaises(AgentRuntimeError):
                Response("deepseek", "fixture-key", lambda e: None).consume(
                    {"type": "final", "reason": {"kind": kind}, "text": "partial"}
                )

    def test_tree_rejects_aliases_file_directory_collisions_links_and_credentials(self):
        good = base64.b64encode(b"ok").decode()
        key = "fixture-provider-key"
        for files in (
            {"a": good, "a/b": good},
            {"a/b": good, "a": good},
            {"src/A": good, "src/a": good},
            {"../a": good},
            {".git/info": good},
            {"a": base64.b64encode(key.encode()).decode()},
            {"e\u0301": good, "é": good},
            {"/absolute": good},
            {"assets/CON.txt": good},
            {"trailing.": good},
        ):
            with self.subTest(paths=list(files)), self.assertRaises(AgentRuntimeError):
                runtime._validate_tree(files, key)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "link").symlink_to("/tmp")
            with self.assertRaises(AgentRuntimeError):
                runtime._read_tree(root, key)
            (root / "link").unlink()
            (root / "one").write_text("x")
            os.link(root / "one", root / "two")
            with self.assertRaises(AgentRuntimeError):
                runtime._read_tree(root, key)

    def test_cancelled_or_missing_key_do_not_start_docker(self):
        with tempfile.TemporaryDirectory() as folder, patch.dict(
            os.environ, {}, clear=True
        ):
            with patch.object(runtime, "_docker_context", side_effect=AssertionError):
                for cancelled, code in (
                    (True, "cancelled"),
                    (False, "needs_configuration"),
                ):
                    with self.subTest(code=code), self.assertRaises(
                        AgentRuntimeError
                    ) as error:
                        run_agent(
                            runner(), folder, "check", cancelled=lambda: cancelled
                        )
                    self.assertEqual(error.exception.code, code)

    def test_stream_bounds_silent_processes(self):
        import subprocess

        with subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1)"], stdout=subprocess.PIPE
        ) as p:
            try:
                with self.assertRaises(AgentRuntimeError) as error:
                    runtime._stream(
                        p, time.monotonic() - 1, None, lambda: False, "fixture", False
                    )
                self.assertEqual(error.exception.code, "agent_timeout")
            finally:
                p.kill()


# The fake executable runs INSIDE a real isolated container, sees ordinary
# stdin/stdout and has no provider calls. The parent still uses real Docker,
# configured limits, UID, read-only root and the production PID 1 adapter.
CLAUDE_FIXTURE = """
import json,os,sys
from pathlib import Path
assert sys.stdin.read() == 'review example'
assert 'NARB_DISABLE_NATIVE_CACHE' not in os.environ
assert os.getuid() != 0 and 'CLAUSEWEFT_CONSOLE_TOKEN' not in os.environ
assert not Path('/var/run/docker.sock').exists()
assert Path('original.txt').read_text() == 'one'
assert os.environ['ANTHROPIC_API_KEY'] == 'offline-sentinel'
Path('original.txt').unlink()
Path('edited.txt').write_text('two')
print(json.dumps({'type':'result', 'subtype':'success','is_error':False,'result':'OK'}))
"""
DEEPSEEK_FIXTURE = """
import json,os,re,sys
from pathlib import Path
assert os.environ['NARB_DISABLE_NATIVE_CACHE'] == '1'
assert any(line.split()[1] == '/tmp' and 'noexec' in line.split()[3].split(',')
           for line in Path('/proc/mounts').read_text().splitlines())
guard = Path('/tmp/review.mjs').read_text()
assert 'ctx.tools.guard(' in guard
print(re.search('clauseweft-review-[0-9a-f]+', guard).group(), flush=True)
packet = json.load(sys.stdin)
assert packet['source_files'] == {'original.txt':'one'}
assert packet['review_request'] == 'review example'
assert 'ANTHROPIC_API_KEY' not in os.environ and not Path('original.txt').exists()
try:
    Path('written').write_text('no')
except OSError:
    pass
else:
    sys.exit(3)
print(json.dumps({'type':'session','sessionId':'s-readonly'}))
print(json.dumps({'type':'final','reason':{'kind':'completed'},'text':'OK'}))
"""


@unittest.skipUnless(
    os.environ.get("CLAUSEWEFT_TEST_IMAGE"),
    "set CLAUSEWEFT_TEST_IMAGE to a local Python image ID",
)
class DockerRuntime(unittest.TestCase):
    def case(self, kind, fixture, after=None, cancelled=lambda: False, timeout=12):
        registration = runner(
            kind, os.environ["CLAUSEWEFT_TEST_IMAGE"], seconds=timeout
        )
        with tempfile.TemporaryDirectory() as td, patch.dict(
            os.environ, {"OFFLINE_TEST_PROVIDER_KEY": "offline-sentinel"}
        ):
            work = Path(td)
            (work / "original.txt").write_text("one")
            with patch.object(
                runtime,
                "_cli",
                return_value=["/usr/local/bin/python3", "-I", "-u", "-c", fixture],
            ):
                result = run_agent(
                    registration, work, "review example", cancelled=cancelled
                )
            self.assertEqual(
                {p.name: p.read_text() for p in work.iterdir()},
                after or {"original.txt": "one"},
            )
            return result

    def test_claude_and_readonly_deepseek_abi(self):
        self.assertEqual(
            self.case("claude", CLAUDE_FIXTURE, {"edited.txt": "two"})["text"], "OK"
        )
        self.assertEqual(
            self.case("deepseek", DEEPSEEK_FIXTURE)["session_id"], "s-readonly"
        )

    def test_unapproved_files_and_incomplete_provider_rejected(self):
        for extra in (
            "Path('bad').symlink_to('/etc/passwd')",
            "Path('edited.txt').write_text('offline-sentinel')",
            "sys.exit(3)",
        ):
            with self.subTest(extra=extra), self.assertRaises(AgentRuntimeError):
                self.case("claude", CLAUDE_FIXTURE + "\n" + extra)

    def test_docker_process_cancellation(self):
        start = time.monotonic()
        with self.assertRaises(AgentRuntimeError) as error:
            self.case(
                "claude",
                "import time; time.sleep(30)",
                cancelled=lambda: time.monotonic() > start + 1.5,
            )
        self.assertEqual(error.exception.code, "cancelled")


@unittest.skipUnless(
    os.environ.get("CLAUSEWEFT_TEST_CLAUDE"),
    "set CLAUSEWEFT_TEST_CLAUDE to a local Claude Code executable",
)
class NativeClaudeProtocol(unittest.TestCase):
    def test_real_cli_against_loopback_provider(self):
        from dataclasses import replace
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json
        import subprocess
        import threading

        seen = []

        class Messages(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                packet = json.loads(
                    self.rfile.read(int(self.headers["Content-Length"]))
                )
                seen.append(self.path)
                with_stream = packet.get("stream") is True
                message = {
                    "id": "msg_offline",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-fixture",
                    "content": [{"type": "text", "text": "offline-reply"}],
                    "stop_reason": "end_turn",
                    "stop_sequence": None,
                    "usage": {"input_tokens": 5, "output_tokens": 2},
                }
                if self.path.split("?")[0].endswith("/count_tokens"):
                    with_stream = False
                    payload = json.dumps({"input_tokens": 5}).encode()
                elif with_stream:
                    events = (
                        (
                            "message_start",
                            {"message": dict(message, content=[], stop_reason=None)},
                        ),
                        (
                            "content_block_start",
                            {"index": 0, "content_block": {"type": "text", "text": ""}},
                        ),
                        (
                            "content_block_delta",
                            {
                                "index": 0,
                                "delta": {
                                    "type": "text_delta",
                                    "text": "offline-reply",
                                },
                            },
                        ),
                        ("content_block_stop", {"index": 0}),
                        (
                            "message_delta",
                            {
                                "delta": {
                                    "stop_reason": "end_turn",
                                    "stop_sequence": None,
                                },
                                "usage": {"output_tokens": 2},
                            },
                        ),
                        ("message_stop", {}),
                    )
                    payload = "".join(
                        f"event: {event}\ndata: {json.dumps(dict(type=event, **args))}\n\n"
                        for event, args in events
                    ).encode()
                else:
                    payload = json.dumps(message).encode()
                self.send_response(200)
                self.send_header(
                    "Content-Type",
                    "text/event-stream" if with_stream else "application/json",
                )
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        executable = str(Path(os.environ["CLAUSEWEFT_TEST_CLAUDE"]).resolve())
        provider = ThreadingHTTPServer(("127.0.0.1", 0), Messages)
        thread = threading.Thread(target=provider.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as wd:
                key = "fixture-anthropic-offline"
                env = {
                    "PATH": os.defpath,
                    "HOME": wd,
                    "TMPDIR": wd,
                    "LANG": "C.UTF-8",
                    "ANTHROPIC_API_KEY": key,
                    "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{provider.server_port}",
                    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
                }
                cli = runtime._cli(replace(runner(), executable=executable))
                run = subprocess.run(
                    cli,
                    input=b"Reply once.",
                    cwd=wd,
                    env=env,
                    capture_output=True,
                    timeout=40,
                )
                self.assertEqual(run.returncode, 0, run.stderr[-600:])
                response = Response("claude", key, lambda e: None)
                for line in run.stdout.splitlines():
                    response.consume(json.loads(line))
                self.assertEqual(response.result()["text"], "offline-reply")
                self.assertTrue(any(path.startswith("/v1/messages") for path in seen))
        finally:
            provider.shutdown()
            provider.server_close()
            thread.join()


@unittest.skipUnless(
    os.environ.get("CLAUSEWEFT_TEST_DSH"),
    "set CLAUSEWEFT_TEST_DSH to the pinned DeepSeek Harness executable",
)
class NativeDeepSeekProtocol(unittest.TestCase):
    def test_real_cli_guard_and_offline_completion(self):
        from dataclasses import replace
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import json
        import shutil
        import subprocess
        import threading

        evidence = {"denied": False, "calls": 0}

        class Completions(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                evidence["calls"] += 1
                # Exercise the actual monotonic guard. Even the synthetic
                # command is read-only and contains no user-specific inputs.
                names = {tool.get("name") for tool in req.get("tools", [])}
                want_tool = evidence["calls"] == 1 and "bash" in names
                if "This review uses only the attached snapshot" in str(
                    req.get("messages")
                ):
                    evidence["denied"] = True
                block = (
                    {
                        "type": "tool_use",
                        "id": "call-offline",
                        "name": "bash",
                        "input": {},
                    }
                    if want_tool
                    else {"type": "text", "text": ""}
                )
                delta = (
                    {"type": "input_json_delta", "partial_json": '{"command":"pwd"}'}
                    if want_tool
                    else {"type": "text_delta", "text": "offline-review"}
                )
                message = {
                    "type": "message",
                    "model": req["model"],
                    "role": "assistant",
                    "id": "msg_fixture",
                    "content": [],
                    "usage": {"input_tokens": 12, "output_tokens": 0},
                }
                events = (
                    ("message_start", {"message": message}),
                    ("content_block_start", {"index": 0, "content_block": block}),
                    ("content_block_delta", {"index": 0, "delta": delta}),
                    ("content_block_stop", {"index": 0}),
                    (
                        "message_delta",
                        {
                            "delta": {
                                "stop_reason": "tool_use" if want_tool else "end_turn"
                            },
                            "usage": {"output_tokens": 3},
                        },
                    ),
                    ("message_stop", {}),
                )
                body = "".join(
                    f"event: {name}\ndata: {json.dumps(dict(type=name,**args))}\n\n"
                    for name, args in events
                ).encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        cli = str(Path(os.environ["CLAUSEWEFT_TEST_DSH"]).resolve())
        with tempfile.TemporaryDirectory() as folder, ThreadingHTTPServer(
            ("127.0.0.1", 0), Completions
        ) as provider:
            thread = threading.Thread(target=provider.serve_forever, daemon=True)
            thread.start()
            try:
                target = Path(folder)
                work = target / "project"
                work.mkdir()
                gate = runtime.review_gate(folder)
                (target / "review.mjs").write_text(gate["plugin"])
                base_url = f"http://127.0.0.1:{provider.server_port}"
                profile = target / "review.yml"
                profile.write_text(
                    gate["patch"]
                    + f"- id: llm-deepseek\n  config:\n    baseURL: {base_url}\n    apiKeyEnv: DEEPSEEK_API_KEY\n"
                )
                args = runtime._cli(replace(runner("deepseek"), executable=cli))
                args[args.index("/tmp/review.yml")] = str(profile)
                key = "fixture-deepseek-offline"
                env = {
                    "PATH": str(Path(shutil.which("node")).parent)
                    + os.pathsep
                    + os.defpath,
                    "HOME": folder,
                    "LANG": "C.UTF-8",
                    "TMPDIR": folder,
                    "DEEPSEEK_API_KEY": key,
                    "DEEPSEEK_BASE_URL": base_url,
                    "DSH_HOME": str(target / "home"),
                }
                with tempfile.TemporaryFile() as diagnostics, subprocess.Popen(
                    args,
                    cwd=work,
                    env=env,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=diagnostics,
                ) as proc:
                    try:
                        pending = bytearray()
                        response = Response("deepseek", key, lambda event: None)
                        ready = False
                        for block in runtime._output_chunks(
                            proc.stdout, time.monotonic() + 50, lambda: False
                        ):
                            pending.extend(block)
                            while b"\n" in pending:
                                row, _, rest = pending.partition(b"\n")
                                pending = bytearray(rest)
                                if (
                                    not ready
                                    and row.rstrip().decode() == gate["marker"]
                                ):
                                    ready = True
                                    proc.stdin.write(
                                        b'{"review_request":"check","source_files":{"sample":"hi"}}'
                                    )
                                    proc.stdin.close()
                                elif ready and row.strip():
                                    message = json.loads(row)
                                    try:
                                        response.consume(message)
                                    except AgentRuntimeError:
                                        self.fail(
                                            f"CLI fixture returned {message.get('type')}: {str(message.get('reason') or message.get('message'))[:1000]}"
                                        )
                        diagnostics.seek(0)
                        self.assertEqual(
                            proc.wait(timeout=5), 0, diagnostics.read()[-1500:]
                        )
                        self.assertTrue(ready)
                        self.assertEqual(response.result()["text"], "offline-review")
                        self.assertTrue(
                            evidence["denied"],
                            "the provider must receive the tool refusal",
                        )
                    finally:
                        if proc.poll() is None:
                            proc.kill()
            finally:
                provider.shutdown()
                thread.join()
