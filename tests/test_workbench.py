"""Exercise the public control API, independent gateway, Git, and Docker together."""
from base64 import b64encode
from hashlib import sha256
import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import uuid4

from clauseweft.api.application import Application
from clauseweft.api.server import Handler
from test_delivery import TEST_IMAGE, change, config as suite_config, git
from test_gateway import gateway, request
from platform_fixture import prepare_team


class ScriptedModel(BaseHTTPRequestHandler):
    def do_POST(self):
        data = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        role = data["messages"][0]["content"]
        if '只返回 JSON {"check":"ok"}' in role:
            answer = {"check": "ok"}
        elif "撰写可验证" in role:
            answer = {"clauses": [{"id": "CSV-01", "text": "普通成员不可导出", "acceptance": "输出 denied"}]}
        elif "unified diff" in role:
            if "实施代码" in role:
                answer = {"patch": change("print('base')", "print('denied')")}
            else:
                answer = {"patch": change("baseline", "member => denied", "cases.txt")}
        else:
            answer = {"findings": []}
        text = json.dumps(answer, ensure_ascii=False)
        reason = "stop"
        messages = [m["content"] for m in data["messages"]]
        if "cut off" in messages and data["max_tokens"] <= 12:
            text, reason = "partial answer", "length"
        if "filter" in messages:
            text, reason = None, "content_filter"
        if "blank" in messages:
            text = " \n"
        reply = (
            {
                "choices": [
                    {
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": reason,
                    }
                ],
                "model": data["model"],
                "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
            }
        )
        if "blank" in messages:
            del reply["usage"]
        body = json.dumps(reply).encode()
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class WorkbenchHTTP(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        git(self.repo, "init", "--template=")
        for name, text in [("app.py", "print('base')"), ("cases.txt", "baseline")]:
            (self.repo / name).write_text(text + "\n")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "fixture")
        self.base = git(self.repo, "rev-parse", "HEAD").decode().strip()
        provider = self.listen(ScriptedModel)
        transport = dict(
            endpoints=[
                dict(
                    id=name,
                    model=name,
                    protocol="chat_completions",
                    base_url=f"http://127.0.0.1:{provider.server_port}/v1",
                    max_tokens=64 if name == "B" else 1024,
                )
                for name in ("A", "B")
            ]
        )
        local = gateway(transport, self.root, with_token=False)
        gw_port = local.__enter__()
        self.addCleanup(local.__exit__, None, None, None)
        self.etag = request(gw_port, "/v1/models")[1]["ETag"]
        cfg = self.root / "console.json"
        cfg.write_text(
            json.dumps(
                {
                    "gateways": {"g": {"url": f"http://127.0.0.1:{gw_port}"}},
                    "repositories": {
                        "demo": {
                            "path": str(self.repo),
                            "allowlist": ["app.py", "cases.txt"],
                            "suite_id": "s",
                            "paths": {"code": ["app.py"], "test-design": ["cases.txt"]},
                        }
                    },
                    "suites": {
                        "s": suite_config(TEST_IMAGE or "sha256:" + "0" * 64)
                    },
                }
            )
        )
        self.app = Application(self.root / "state", cfg)
        prepare_team(self.app)
        server = self.listen(Handler)
        self.port = server.server_port
        server.authorization = b"Bearer fixture"
        server.app = self.app

    def listen(self, handler):
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server

    def call(self, path, data=None, *, status=200, key=None, rev=None, headers=None, method=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            auth = {
                "Authorization": "Bearer fixture",
                "Content-Type": "application/json",
                "Idempotency-Key": key or uuid4().hex,
            }
            if rev is not None:
                auth["If-Match"] = f'"{rev}"'
            conn.request(
                method or ("POST" if data is not None else "GET"),
                "/api/v1/" + path,
                json.dumps(data).encode() if data is not None else None,
                dict(auth, **(headers or {})),
            )
            res = conn.getresponse()
            body = json.loads(res.read())
            self.assertEqual(res.status, status, body)
            if path == "endpoints" and data is not None and res.status == 201:
                prepare_team(self.app)
            return body
        finally:
            conn.close()

    def model(self, eid):
        return dict(
            endpoint_id=eid,
            model_identity="provider-" + eid,
            identity_verified=True,
            gateway_id="g",
            gateway_etag=self.etag,
            enabled=True,
            allowed=True,
            context_tokens=30000,
            location="local",
            fit=dict(code=4, test=4, review=4, requirements=4),
            priority=1,
            capabilities=[],
            input_micros_per_million=0,
            output_micros_per_million=0,
        )

    def test_console_policy_matches_only_the_shipped_script(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        self.addCleanup(conn.close)
        conn.request("GET", "/")
        res = conn.getresponse()
        self.assertEqual(res.status, 200)
        page = res.read().decode()
        script = page.split("<script>", 1)[1].split("</script>", 1)[0]
        expected = b64encode(sha256(script.encode()).digest()).decode()
        directives = res.getheader("Content-Security-Policy").split("; ")
        self.assertIn(f"script-src 'sha256-{expected}'", directives)
        self.assertIn("frame-ancestors 'none'", directives)
        self.assertEqual(sum(d.startswith("script-src") for d in directives), 1)

    def test_access_idempotency_and_revisions(self):
        self.call("runs", headers={"Authorization": "Bearer wrong"}, status=401)
        self.call("workspace", headers={"Origin": "http://foreign.invalid"}, status=403)
        model = self.model("A")
        new = self.call("endpoints", model, key="create", status=201)
        self.assertEqual(new, self.call("endpoints", model, key="create", status=201))
        self.call("endpoints", self.model("B"), key="create", status=409)
        self.call("endpoints/A", {"enabled": False}, method="PATCH", status=428)
        self.call("endpoints/A", {"enabled": False}, method="PATCH", rev=0, status=412)
        self.assertTrue(self.call("endpoints/A/probes", {}, rev=1)["etag_matches"])
        self.call("runs", {"task": {"text": "评审", "data_scope": "public"}}, status=403)
        self.call("runs", {"task": {"text": "评审", "input_tokens": 0}}, status=422)
        plan = {"task": {"text": "评审"}}
        r = self.call("runs", plan, status=201, key="start")
        self.assertTrue(self.app.runs.work_once())
        current = self.call("runs/" + r["run_id"])
        self.assertEqual(current["status"], "completed", current)
        self.assertEqual(r, self.call("runs", plan, status=201, key="start"))
        other = self.call("runs", plan, status=201)
        sha = current["nodes"][0]["artifact"]
        self.call("runs/" + other["run_id"] + "/artifacts/" + sha, status=404)
        self.assertGreater(len(self.call("runs/" + r["run_id"] + "/events")["items"]), 1)

    def test_incomplete_answers_stop_bill_once_and_require_manual_recovery(self):
        self.call("endpoints", dict(self.model("B"), output_micros_per_million=1_000_000), status=201)
        self.assertEqual(self.call("endpoints/B/probes", {}, rev=1)["max_output_tokens"], 64)
        for text, limit, error, reason in (("cut off", 12, "output_length_limit", "length"),
                                           ("filter", 1024, "output_content_filtered", "content_filter"),
                                           ("blank", 1024, "empty_model_output", "stop")):
            with self.subTest(text=text):
                run = self.call("runs", {"task": {"text": text, "stage": "general", "max_output_tokens": limit}}, status=201)
                path = "runs/" + run["run_id"]
                self.assertTrue(self.app.runs.work_once())
                current = self.call(path)
                self.assertEqual(current["status"], "needs_input")
                self.assertIsNone(current["nodes"][0]["artifact"])
                self.assertEqual(current["nodes"][0]["reason"], error)
                self.assertFalse(self.app.runs.work_once())
                attempt, = self.call(path + "/attempts")["items"]
                self.assertTrue(attempt["receipt"])
                self.assertEqual(attempt["completion_metadata"], {"finish_reason": reason, "max_output_tokens": min(64, limit)})
                if text == "blank":
                    self.assertEqual(current["budget"]["held_micros"], limit)
                    self.assertEqual(attempt["usage"]["status"], "missing")
                    still_waiting = self.call(path + "/resume", {}, rev=current["revision"])
                    self.assertEqual(still_waiting["status"], "needs_input")
                    self.assertFalse(self.app.runs.work_once())
                else:
                    self.assertEqual(current["budget"]["settled_micros"], 5)
                    self.assertEqual(attempt["usage"]["total_tokens"], 16)
                    self.assertEqual(current["budget"]["held_micros"], 0)
                if text == "cut off":
                    self.assertEqual(self.call(path + "/artifacts/" + attempt["artifact"])["content"], "partial answer")
                    new = self.call(path + "/continuations", {"action": "regenerate", "max_output_tokens": 100},
                                    rev=current["revision"], status=201)
                    self.assertTrue(self.app.runs.work_once())
                    result = self.call("runs/" + new["run_id"])
                    self.assertEqual(result["status"], "completed")
                    self.assertEqual(self.call(path)["status"], "needs_input")

    def test_registered_endpoint_limit_sets_preview_budget_and_dispatch_ceiling(self):
        self.call("endpoints", dict(self.model("B"), max_output_tokens=64,
                                    output_micros_per_million=1_000_000), status=201)
        plan = {"task": {"text": "评审", "max_output_tokens": 1024}}
        preview = self.call("routing/previews", plan)
        self.assertEqual(preview["budget"]["limit_micros"], 64)
        self.assertEqual(preview["candidates"][0]["max_output_tokens"], 64)
        run = self.call("runs", plan, status=201)
        self.assertTrue(self.app.runs.work_once())
        path = "runs/" + run["run_id"]
        self.assertEqual(self.call(path)["status"], "completed")
        attempt, = self.call(path + "/attempts")["items"]
        self.assertEqual(attempt["completion_metadata"]["max_output_tokens"], 64)

    def test_probe_charges_a_versioned_run_and_preserves_historical_evidence(self):
        self.call("endpoints", self.model("A"), status=201)
        probe = self.call(
            "endpoints/A/probes", {"kind": "structured_output"}, rev=1, status=202, key="probe"
        )
        self.assertTrue(self.app.runs.work_once())
        run = self.call("runs/" + probe["run_id"])
        self.assertEqual(run["status"], "completed")
        attempts = self.call("runs/" + probe["run_id"] + "/attempts")["items"]
        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0]["receipt"])
        history = self.call("endpoints/A/probes")["items"]
        self.assertEqual(history[0]["created"], run["created"])
        self.assertTrue(history[0]["verified"] and history[0]["current"])
        self.call("endpoints/A", {"enabled": False}, method="PATCH", rev=1)
        history = self.call("endpoints/A/probes")["items"]
        self.assertFalse(history[0]["current"])
        self.assertTrue(history[0]["verified"])
        self.assertEqual(
            probe,
            self.call("endpoints/A/probes", {"kind": "structured_output"}, rev=1, status=202, key="probe"),
        )

    def test_preview_reacts_to_task_length_and_endpoint_availability(self):
        self.call("endpoints", dict(self.model("A"), context_tokens=1024), status=201)
        self.call("endpoints", dict(self.model("B"), output_micros_per_million=1_000_000), status=201)
        plan = {"task": {"text": "请评审", "max_output_tokens": 100},
                "budget": {"currency": "USD", "limit_micros": 100}}
        short = self.call("routing/previews", plan)
        self.assertEqual(short["selected_endpoint"], "A")
        plan["task"]["text"] += "权限说明" * 100
        long = self.call("routing/previews", plan)
        self.assertEqual(long["selected_endpoint"], "B")
        candidates = {item["endpoint_id"]: item for item in long["candidates"]}
        self.assertIn("context_limit", candidates["A"]["reasons"])
        self.assertEqual(candidates["B"]["location"], "local")
        self.call("endpoints/B", {"enabled": False}, method="PATCH", rev=1)
        blocked = self.call("routing/previews", plan)
        self.assertEqual(blocked["status"], "blocked")
        self.assertIsNone(blocked["selected_endpoint"])
        self.assertEqual(self.call("runs")["items"], [])

    def test_repository_preview_uses_current_binding_without_inference(self):
        self.call("endpoints", self.model("A"), status=201)
        plan = {"task": {"text": "CSV"}, "repo_id": "demo", "base_sha": self.base, "mode": "workflow"}
        preview = self.call("routing/previews", plan)
        self.assertEqual(preview["selected_endpoint"], "A")
        self.assertEqual(preview["preview_node"], "draft")
        self.assertEqual(self.call("runs")["items"], [])
        self.call("routing/previews", dict(plan, repo_id="missing"), status=403)

    @unittest.skipUnless(TEST_IMAGE, "requires a local pinned Python image")
    def test_full_delivery_from_http(self):
        for m in ("A", "B"):
            self.call("endpoints", self.model(m), status=201)
        plan = {
            "task": {"text": "限制 CSV 导出", "max_output_tokens": 400},
            "mode": "workflow",
            "repo_id": "demo",
            "base_sha": self.base,
        }
        r = self.call("runs", plan, status=201)
        prefix = "runs/" + r["run_id"]
        stop = threading.Event()
        workers = [threading.Thread(target=self.app.work, args=(stop,), daemon=True) for _ in range(2)]
        for w in workers:
            w.start()

        def cleanup():
            stop.set()
            for w in workers:
                w.join(3)

        self.addCleanup(cleanup)

        def until(predicate, timeout=20):
            end = time.monotonic() + timeout
            while True:
                run = self.call(prefix)
                if predicate(run):
                    return run
                if time.monotonic() > end:
                    self.fail(json.dumps(run, ensure_ascii=False))
                time.sleep(0.07)

        def state(run, key):
            return next(n["state"] for n in run["nodes"] if n["node_id"] == key)

        def action(name, data, code):
            # Worker events can race a human action; fetch the latest resource revision again.
            for _ in range(12):
                rev = self.call(prefix)["revision"]
                try:
                    return self.call(prefix + "/" + name, data, rev=rev, status=code)
                except AssertionError as exc:
                    if "REVISION_CHANGED" not in str(exc):
                        raise
            self.fail("持续版本冲突")

        run = until(lambda x: state(x, "approve-spec") == "needs_input" and x["spec_version_id"])
        # Wait for the committed review imports, separately from worker completion.
        until(
            lambda x: sum(e["kind"] == "artifact_checked" for e in self.call(prefix + "/events")["items"])
            >= 3
        )
        version = run["spec_version_id"]
        action("approvals", {"kind": "baseline", "version": version, "reason": "核对条款与验收"}, 200)
        until(lambda x: state(x, "integrate") == "needs_input")
        action("integrations", {"selected": ["code", "test-design"], "reason": "采用两个互补工作包"}, 202)
        run = until(lambda x: x["candidate_sha"] and state(x, "execute-tests") == "needs_input")
        sha = run["candidate_sha"]
        action("tests", {"candidate_sha": sha}, 202)
        until(lambda x: state(x, "accept") == "needs_input")
        action(
            "approvals",
            {"kind": "delivery", "version": version, "candidate_sha": sha, "reason": "核对实测证据"},
            200,
        )
        delivery = action("exports", {}, 200)
        self.assertEqual(delivery["candidate_sha"], sha)
        self.assertEqual(delivery["report"]["status"], "passed")
        self.assertEqual(delivery["report"]["cases"][0]["exit_code"], 0)
        self.assertIn("print('denied')", delivery["manifest"]["candidate"]["patch"])
        self.assertEqual(git(self.repo, "status", "--porcelain"), b"")
