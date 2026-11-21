"""Tests for P0 AI Agent capabilities: tiered tool approval, SSE streams, token details,
external agent testing/continuation, and bounded auto test-fix reflection loops."""

from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from clauseweft import audit, testing
from clauseweft.api.application import Application
from clauseweft.api.server import Handler
from platform_fixture import prepare_team


TEST_IMAGE = os.environ.get("CLAUSEWEFT_TEST_IMAGE")


def _git(repo, *args):
    subprocess.run(
        [
            "git", "-C", str(repo),
            "-c", "user.name=Fixture",
            "-c", "user.email=fixture@example.invalid",
            "-c", f"core.hooksPath={os.devnull}",
            *args,
        ],
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": os.devnull},
    )


def _fake_suite_report(candidate, suite, files):
    """Deterministic host-side test stub matching testing.verify() contract."""
    text = files.get("app.py", b"").decode("utf-8", errors="replace")
    passed = "fixed" in text
    case = suite["cases"][0]
    import hashlib
    stdout = case["stdout"] if passed else "wrong\n"
    row = {
        "id": case["id"],
        "clause_id": case["clause_id"],
        "command_id": case["command_id"],
        "argv": suite["commands"][case["command_id"]],
        "status": "passed" if passed else "failed",
        "exit_code": 0 if passed else 1,
        "log": "" if passed else "AssertionError: expected ok\nwrong",
        "stdout_digest": hashlib.sha256(stdout.encode()).hexdigest(),
        "ended_at": "2026-11-21T00:00:00+00:00",
    }
    return {
        "adapter": testing.ADAPTER,
        "execution_id": uuid4().hex,
        "candidate_sha": candidate["candidate_sha"],
        "tree_hash": candidate["tree_hash"],
        "spec_version_id": suite["spec_version_id"],
        "suite_digest": suite["suite_digest"],
        "env_digest": suite["env_digest"],
        "image": suite["image"],
        "started_at": "2026-11-21T00:00:00+00:00",
        "ended_at": "2026-11-21T00:00:01+00:00",
        "elapsed_ms": 12,
        "status": "passed" if passed else "failed",
        "cases": [row],
        "counts": {
            "passed": 1 if passed else 0,
            "failed": 0 if passed else 1,
            "missing": 0,
            "incomplete": 0,
            "unreported": 0,
        },
    }


class P0AgentCapabilitiesTest(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        (self.repo / "app.py").write_text("print('base')\n", encoding="utf-8")
        _git(self.repo, "init")
        _git(self.repo, "add", ".")
        _git(self.repo, "commit", "-m", "initial")
        self.base_sha = subprocess.check_output(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], text=True
        ).strip()
        self.image = TEST_IMAGE or ("sha256:" + "1" * 64)
        self.suite_cfg = {
            "image": self.image,
            "timeout_seconds": 20,
            "commands": {"check": ["python", "app.py"]},
            "cases": [{
                "id": "case-ok",
                "clause_id": "CL-01",
                "command_id": "check",
                "stdin": "",
                "stdout": "fixed\n",
                "required_files": ["app.py"],
            }],
        }
        self.config_path = self.root / "workbench.json"
        self.config_path.write_text(json.dumps({
            "data_scope": "public",
            "gateways": {"g": {"url": "http://127.0.0.1:8765"}},
            "repositories": {
                "repo": {
                    "path": str(self.repo),
                    "allowlist": ["app.py"],
                    "paths": {"code": ["app.py"], "test-design": ["app.py"]},
                    "data_scope": "public",
                    "suite_id": "app-suite",
                    "suite_ids": ["app-suite"],
                }
            },
            "suites": {"app-suite": self.suite_cfg},
            "agent_runners": {
                "claude": {
                    "kind": "claude",
                    "image": self.image,
                    "executable": "/usr/local/bin/claude",
                    "token_env": "P0_TEST_CLAUDE_KEY",
                },
                "deepseek": {
                    "kind": "deepseek",
                    "image": self.image,
                    "executable": "/usr/local/bin/dsh",
                    "token_env": "P0_TEST_DEEPSEEK_KEY",
                },
            },
        }), encoding="utf-8")
        self.app = Application(self.root / "state", self.config_path)
        for eid, identity in (("local-a", "model-a"), ("local-b", "model-b")):
            self.app.catalog.save({
                "endpoint_id": eid,
                "model_identity": identity,
                "gateway_id": "g",
                "gateway_etag": '"v1"',
                "location": "local",
                "fit": {"general": 4, "requirements": 4, "review": 4, "code": 4, "test": 4},
                "currency": "CNY",
                "context_tokens": 32000,
                "enabled": True,
                "allowed": True,
                "identity_verified": True,
                "capabilities": ["structured_output"],
                "priority": 1,
                "input_micros_per_million": 1000,
                "output_micros_per_million": 2000,
            })
        prepare_team(self.app)
        with self.app.store.transaction() as db:
            ws = self.app.platform.get(db, "workspaces", "default")
            self.app.platform.save(
                db, "workspaces",
                {"endpoint_ids": ["local-a", "local-b"], "repository_ids": ["repo"]},
                "default", ws["revision"],
            )

    def call(self, method, path, data=None, rev=None, query=None, principal=None):
        return self.app.handle(
            method, tuple(path.split("/")), query or {}, data or {}, uuid4().hex, rev,
            principal=principal,
        )

    # ---- P0-1: Tiered tool approval policy ----

    def test_auto_read_only_executes_search_automatically_and_pauses_on_write(self):
        replies = iter([
            json.dumps({"action": "tool", "tool_id": "search_requirements", "arguments": {"query": "退款对账"}}, ensure_ascii=False),
            json.dumps({
                "action": "tool",
                "tool_id": "create_requirement",
                "arguments": {"title": "退款对账", "description": "验收时核对订单编号", "priority": "medium"},
            }, ensure_ascii=False),
        ])
        created_id = []

        def fake_generate(endpoint, messages, limit):
            try:
                text = next(replies)
            except StopIteration:
                text = json.dumps({"action": "finish", "answer": f"已新建需求 {created_id[0]}。"}, ensure_ascii=False)
            return text, {"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140,
                          "cached_tokens": 60, "reasoning_tokens": 12}, uuid4().hex

        self.app.catalog.generate = fake_generate
        status, run = self.call("POST", "runs", {
            "mode": "tool",
            "project_id": "default",
            "allowed_tools": ["search_requirements", "create_requirement"],
            "requirement_query": "退款对账",
            "max_tool_steps": 2,
            "tool_approval_policy": "auto_read_only",
            "budget": {"currency": "CNY", "limit_micros": 100000},
            "task": {"text": "查重后新建退款对账需求"},
        })
        self.assertEqual(status, 201)
        rid = run["run_id"]
        self.assertEqual(run["tool_approval_policy"], "auto_read_only")

        # Work step 1: model proposes search_requirements (read_only) -> auto-approved immediately!
        self.assertTrue(self.app.runs.work_once())
        mid = self.call("GET", f"runs/{rid}")[1]
        self.assertEqual(mid["status"], "queued")
        self.assertEqual(next(n for n in mid["nodes"] if n["node_id"] == "confirm-1")["state"], "succeeded")
        self.assertEqual(next(n for n in mid["nodes"] if n["node_id"] == "decide-2")["state"], "ready")

        # Work step 2: model proposes create_requirement (mutating) -> MUST pause for human approval!
        self.assertTrue(self.app.runs.work_once())
        waiting = self.call("GET", f"runs/{rid}")[1]
        self.assertEqual(waiting["status"], "needs_input")
        self.assertEqual(waiting["tool_proposal"]["id"], "create_requirement")
        self.assertEqual(waiting["tool_proposal"]["risk"], "mutating")

        # Approve mutating write
        status, updated = self.call(
            "POST", f"runs/{rid}/tool-decisions",
            {"action": "approve", "proposal_ref": waiting["tool_proposal"]["proposal_ref"], "reason": "确认新建"},
            rev=waiting["revision"],
        )
        self.assertEqual(status, 200)
        reqs = self.call("GET", "requirements", query={"project_id": "default"})[1]["items"]
        self.assertEqual(len(reqs), 1)
        created_id.append(reqs[0]["id"])

        # Step 3: final answer
        self.assertTrue(self.app.runs.work_once())
        done = self.call("GET", f"runs/{rid}")[1]
        self.assertEqual(done["status"], "completed")

        # Goal assessment & usage token details & audit chain all pass
        _, assess = self.call("GET", f"runs/{rid}/goal-assessment",
                              query={"scenario": "created", "query": "退款对账", "title": "退款对账"})
        self.assertEqual(assess["status"], "passed")
        _, usage = self.call("GET", "usage", query={"workspace_id": "default"})
        model_usage = next(m for m in usage["models"] if m["calls"] > 0)
        self.assertEqual(model_usage["cached_tokens"], 180)
        self.assertEqual(model_usage["reasoning_tokens"], 36)
        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])

    def test_auto_read_only_validation_and_revocation_guard(self):
        with self.assertRaises(ValueError):
            self.call("POST", "runs", {
                "mode": "tool", "tool_id": "search_requirements", "tool_approval_policy": "auto_all",
                "project_id": "default", "budget": {"currency": "CNY", "limit_micros": 10000},
                "task": {"text": "test"},
            })
        with self.assertRaises(ValueError):
            self.call("POST", "runs", {
                "mode": "single", "tool_approval_policy": "auto_read_only",
                "project_id": "default", "task": {"text": "test"},
            })
        # Create operator, start auto_read_only tool run, revoke operator before auto_approve
        _, member = self.call("POST", "members", {
            "username": "op1", "display_name": "Operator", "role": "operator",
            "password": "fixture-password-123", "workspace_ids": ["default"], "project_ids": ["default"],
        })
        op_principal = self.app.platform.login("op1", "fixture-password-123")["user"]
        self.app.catalog.generate = lambda ep, msgs, lim: (
            json.dumps({"query": "x"}), {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10}, uuid4().hex
        )
        _, run = self.call("POST", "runs", {
            "mode": "tool", "tool_id": "search_requirements", "tool_approval_policy": "auto_read_only",
            "project_id": "default", "budget": {"currency": "CNY", "limit_micros": 10000},
            "task": {"text": "查需求"},
        }, principal=op_principal)
        # Deactivate operator right after dispatch begins
        orig_gen = self.app.catalog.generate

        def gen_and_revoke(ep, msgs, lim):
            self.call("PATCH", f"members/{member['id']}", {"active": False}, rev=member["revision"])
            return orig_gen(ep, msgs, lim)

        self.app.catalog.generate = gen_and_revoke
        self.assertTrue(self.app.runs.work_once())
        after = self.call("GET", f"runs/{run['run_id']}")[1]
        self.assertEqual(after["status"], "needs_input")
        self.assertEqual(next(n for n in after["nodes"] if n["node_id"] == "confirm")["state"], "needs_input")

    # ---- P0-2: Workbench SSE Streams ----

    def test_workbench_sse_run_and_agent_job_streams(self):
        self.app.catalog.generate = lambda ep, msgs, lim: (
            "streamed ok", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, uuid4().hex
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = self.app
        token = uuid4().hex
        server.authorization = ("Bearer " + token).encode()
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)

        _, run = self.call("POST", "runs", {"mode": "single", "project_id": "default", "task": {"text": "SSE test"}})
        self.assertTrue(self.app.runs.work_once())

        # Unauthenticated SSE rejected with 401
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            conn.request("GET", f"/api/v1/runs/{run['run_id']}/stream?timeout=1")
            self.assertEqual(conn.getresponse().status, 401)
        finally:
            conn.close()

        # Authorized SSE returns real text/event-stream with events & terminal status
        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            conn.request("GET", f"/api/v1/runs/{run['run_id']}/stream?after=0&timeout=2",
                         headers={"Authorization": "Bearer " + token})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            self.assertTrue(resp.getheader("Content-Type", "").startswith("text/event-stream"))
            body = resp.read().decode("utf-8")
        finally:
            conn.close()
        self.assertIn("event: run_created", body)
        self.assertIn("event: attempt_finished", body)
        self.assertIn("event: status", body)
        self.assertIn('"completed"', body)

        # Agent job SSE stream
        def fake_agent(runner, workspace, prompt, *, on_event, cancelled):
            on_event({"type": "tool_use", "tool": "Edit"})
            (workspace / "app.py").write_text("print('fixed')\n", encoding="utf-8")
            return {"text": "done", "usage": {"input_tokens": 5}, "session_id": "s1"}

        with patch("clauseweft.agent_runtime.run_agent", fake_agent):
            _, job = self.call("POST", "agent-jobs", {
                "project_id": "default", "runner_id": "claude", "repo_id": "repo", "prompt": "fix app",
            })
            self.assertTrue(self.app.agent_jobs.work_once())

        conn = HTTPConnection("127.0.0.1", server.server_port, timeout=5)
        try:
            conn.request("GET", f"/api/v1/agent-jobs/{job['id']}/stream?after=0&timeout=2",
                         headers={"Authorization": "Bearer " + token})
            resp = conn.getresponse()
            self.assertEqual(resp.status, 200)
            body = resp.read().decode("utf-8")
        finally:
            conn.close()
        self.assertIn("event: tool_use", body)
        self.assertIn("event: status", body)
        self.assertIn('"succeeded"', body)

    # ---- P0-3: External Agent Testing, Review Context & Follow-up Edit (continue_from) ----

    def test_external_agent_candidate_testing_review_context_and_continue_from(self):
        seen_prompts = []

        def fake_agent(runner, workspace, prompt, *, on_event, cancelled):
            seen_prompts.append((runner.kind, prompt, (workspace / "app.py").read_text(encoding="utf-8")))
            if runner.kind == "claude":
                current = (workspace / "app.py").read_text(encoding="utf-8")
                if "step1" not in current:
                    (workspace / "app.py").write_text("print('step1')\n", encoding="utf-8")
                    return {"text": "step 1 done", "usage": {"input_tokens": 10}, "session_id": "s1"}
                (workspace / "app.py").write_text("print('fixed')\n", encoding="utf-8")
                return {"text": "step 2 fixed", "usage": {"input_tokens": 12}, "session_id": "s2"}
            return {"text": "review complete", "usage": {"input_tokens": 8}, "session_id": "r1"}

        with patch("clauseweft.agent_runtime.run_agent", fake_agent), \
                patch("clauseweft.testing.start", _fake_suite_report):
            # Options expose configured suite for repo
            _, opts = self.call("GET", "agent-jobs/options", query={"project_id": "default"})
            self.assertEqual(opts["repositories"], [{"id": "repo", "suites": ["app-suite"]}])

            # Job 1: initial edit (fails test)
            status, job1 = self.call("POST", "agent-jobs", {
                "project_id": "default", "runner_id": "claude", "repo_id": "repo", "prompt": "step 1 edit",
            })
            self.assertEqual(status, 201)
            self.assertTrue(self.app.agent_jobs.work_once())

            # Run on-demand test suite on Job 1's frozen candidate
            status, test1 = self.call("POST", f"agent-jobs/{job1['id']}/tests", {"suite_id": "app-suite"})
            self.assertEqual(status, 201)
            self.assertFalse(test1["verified"])
            self.assertEqual(test1["status"], "failed")

            # DeepSeek review of Job 1 receives latest test summary in prompt
            _, rev = self.call("POST", "agent-jobs", {
                "project_id": "default", "runner_id": "deepseek", "repo_id": "repo",
                "review_of": job1["id"], "prompt": "review job1",
            })
            self.assertTrue(self.app.agent_jobs.work_once())
            review_prompt = next(p for kind, p, _ in seen_prompts if kind == "deepseek")
            self.assertIn("app-suite", review_prompt)
            self.assertIn("status=failed", review_prompt)

            # Follow-up edit (continue_from=job1) starts from Job 1's candidate ('step1')
            status, job2 = self.call("POST", "agent-jobs", {
                "project_id": "default", "runner_id": "claude", "repo_id": "repo",
                "continue_from": job1["id"], "prompt": "finish fixing app",
            })
            self.assertEqual(status, 201)
            self.assertEqual(job2["continue_from"], job1["id"])
            self.assertTrue(self.app.agent_jobs.work_once())

            # Verify Claude saw step1's frozen file content
            self.assertEqual(seen_prompts[-1][2], "print('step1')\n")

            # Verify Job 2's patch is cumulative against original base_sha ('base' -> 'fixed')
            detail2 = self.call("GET", f"agent-jobs/{job2['id']}")[1]
            self.assertIn("-print('base')", detail2["patch"])
            self.assertIn("+print('fixed')", detail2["patch"])
            patch_file = self.root / "cum.patch"
            patch_file.write_text(detail2["patch"], encoding="utf-8")
            _git(self.repo, "apply", "--check", str(patch_file))

            # On-demand test on Job 2 passes!
            status, test2 = self.call("POST", f"agent-jobs/{job2['id']}/tests", {})
            self.assertEqual(status, 201)
            self.assertTrue(test2["verified"])
            self.assertEqual(test2["status"], "passed")
            detail2 = self.call("GET", f"agent-jobs/{job2['id']}")[1]
            self.assertEqual(len(detail2["test_reports"]), 1)
            self.assertTrue(detail2["test_reports"][0]["verified"])

            # Invalid combinations rejected
            with self.assertRaises(ValueError):
                self.call("POST", "agent-jobs", {
                    "project_id": "default", "runner_id": "claude", "repo_id": "repo",
                    "review_of": job1["id"], "continue_from": job1["id"], "prompt": "both",
                })
            with self.assertRaises(ValueError):
                self.call("POST", "agent-jobs", {
                    "project_id": "default", "runner_id": "claude", "repo_id": "repo",
                    "max_fix_rounds": 1, "prompt": "no suite",
                })
            with self.app.store.transaction() as db:
                self.assertTrue(audit.verify(db)["valid"])

    # ---- P0-4: Bounded Auto Test-Fix Reflection Loop (External Agent + Workflow) ----

    def test_external_agent_bounded_auto_fix_loop_and_round_cap(self):
        calls = []

        def fix_on_round_2(runner, workspace, prompt, *, on_event, cancelled):
            calls.append(prompt)
            if len(calls) == 1:
                (workspace / "app.py").write_text("print('buggy')\n", encoding="utf-8")
                return {"text": "first try", "usage": {"input_tokens": 10}, "session_id": "s1"}
            (workspace / "app.py").write_text("print('fixed')\n", encoding="utf-8")
            return {"text": "fixed after test feedback", "usage": {"input_tokens": 15}, "session_id": "s2"}

        with patch("clauseweft.agent_runtime.run_agent", fix_on_round_2), \
                patch("clauseweft.testing.start", _fake_suite_report):
            status, job = self.call("POST", "agent-jobs", {
                "project_id": "default", "runner_id": "claude", "repo_id": "repo",
                "suite_id": "app-suite", "max_fix_rounds": 2, "prompt": "make app print fixed",
            })
            self.assertEqual(status, 201)
            self.assertTrue(self.app.agent_jobs.work_once())
            detail = self.call("GET", f"agent-jobs/{job['id']}")[1]
            self.assertEqual(detail["status"], "succeeded")
            self.assertEqual(detail["fix_rounds_used"], 1)
            self.assertTrue(detail["test_verified"])
            self.assertEqual(detail["usage"]["input_tokens"], 25)
            self.assertEqual(len(detail["test_reports"]), 2)
            self.assertIn("case-ok", calls[1])
            self.assertIn("auto_fix", [e["type"] for e in detail["events"]])

        # Verify strict upper bound when agent keeps failing
        fail_calls = []

        def always_buggy(runner, workspace, prompt, *, on_event, cancelled):
            fail_calls.append(prompt)
            (workspace / "app.py").write_text("print('still_buggy')\n", encoding="utf-8")
            return {"text": "still buggy", "usage": {"input_tokens": 5}, "session_id": "sf"}

        with patch("clauseweft.agent_runtime.run_agent", always_buggy), \
                patch("clauseweft.testing.start", _fake_suite_report):
            _, job_cap = self.call("POST", "agent-jobs", {
                "project_id": "default", "runner_id": "claude", "repo_id": "repo",
                "suite_id": "app-suite", "max_fix_rounds": 1, "prompt": "try fixing",
            })
            self.assertTrue(self.app.agent_jobs.work_once())
            detail_cap = self.call("GET", f"agent-jobs/{job_cap['id']}")[1]
            self.assertEqual(len(fail_calls), 2)  # initial + 1 auto-fix round
            self.assertEqual(detail_cap["fix_rounds_used"], 1)
            self.assertFalse(detail_cap["test_verified"])
            self.assertEqual(len(detail_cap["test_reports"]), 2)

    @unittest.skipUnless(TEST_IMAGE, "set CLAUSEWEFT_TEST_IMAGE for real Docker candidate test")
    def test_external_agent_real_docker_suite_acceptance_and_auto_fix(self):
        calls = []

        def fix_in_second_round(runner, workspace, prompt, *, on_event, cancelled):
            calls.append(prompt)
            if len(calls) == 1:
                (workspace / "app.py").write_text("print('wrong')\n", encoding="utf-8")
            else:
                (workspace / "app.py").write_text("print('fixed')\n", encoding="utf-8")
            return {"text": f"round {len(calls)}", "usage": {"input_tokens": 10}, "session_id": f"d{len(calls)}"}

        with patch("clauseweft.agent_runtime.run_agent", fix_in_second_round):
            _, job = self.call("POST", "agent-jobs", {
                "project_id": "default", "runner_id": "claude", "repo_id": "repo",
                "suite_id": "app-suite", "max_fix_rounds": 1, "prompt": "print fixed",
            })
            self.assertTrue(self.app.agent_jobs.work_once())
            detail = self.call("GET", f"agent-jobs/{job['id']}")[1]
            self.assertEqual(detail["status"], "succeeded")
            self.assertEqual(detail["fix_rounds_used"], 1)
            self.assertTrue(detail["test_verified"])
            self.assertEqual(len(detail["test_reports"]), 2)
            self.assertTrue(detail["test_reports"][0]["verified"])
            self.assertFalse(detail["test_reports"][1]["verified"])

    def test_workflow_delivery_bounded_auto_rework_on_test_failure(self):
        code_calls = []

        def workflow_generate(endpoint, messages, limit):
            joined = "\n".join(m["content"] for m in messages)
            if "起草" in joined or "clauses" in joined and "findings" not in joined and "patch" not in joined:
                reply = json.dumps({"clauses": [{"id": "CL-01", "text": "输出 fixed", "acceptance": "运行输出 fixed"}]})
            elif "findings" in joined:
                reply = json.dumps({"findings": []})
            else:
                code_calls.append(joined)
                body = (
                    "diff --git a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n@@ -1 +1 @@\n-print('base')\n+print('buggy')\n"
                )
                reply = json.dumps({"patch": body})
            return reply, {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}, uuid4().hex

        self.app.catalog.generate = workflow_generate
        status, run = self.call("POST", "runs", {
            "mode": "workflow",
            "project_id": "default",
            "repo_id": "repo",
            "base_sha": self.base_sha,
            "suite_id": "app-suite",
            "max_fix_rounds": 1,
            "budget": {"currency": "CNY", "limit_micros": 100000},
            "task": {"text": "修改 app.py 输出 fixed"},
        })
        self.assertEqual(status, 201)
        rid = run["run_id"]
        self.assertEqual(run["max_fix_rounds"], 1)

        # Draft + 2 spec reviews
        for _ in range(3):
            self.assertTrue(self.app.runs.work_once())
            self.app.delivery.work_once()

        # Approve baseline
        detail = self.call("GET", f"runs/{rid}")[1]
        spec_ver = detail["spec_version_id"]
        status, _ = self.call(
            "POST", f"runs/{rid}/approvals",
            {"kind": "baseline", "version": spec_ver, "reason": "批准基线"},
            rev=detail["revision"],
        )
        self.assertEqual(status, 200)

        # Code + test-design
        for _ in range(2):
            self.assertTrue(self.app.runs.work_once())

        with patch("clauseweft.testing.start", _fake_suite_report):
            # Round 1: integrate & run failing test -> MUST auto-rework `code` node!
            detail = self.call("GET", f"runs/{rid}")[1]
            status, _ = self.call(
                "POST", f"runs/{rid}/integrations",
                {"selected": ["code"], "reason": "整合候选"},
                rev=detail["revision"],
            )
            self.assertEqual(status, 202)
            self.assertTrue(self.app.delivery.work_once())

            detail = self.call("GET", f"runs/{rid}")[1]
            cand = detail["candidate_sha"]
            status, _ = self.call(
                "POST", f"runs/{rid}/tests",
                {"candidate_sha": cand, "suite_id": "app-suite"},
                rev=detail["revision"],
            )
            self.assertEqual(status, 202)
            self.assertTrue(self.app.delivery.work_once())

            after_fail1 = self.call("GET", f"runs/{rid}")[1]
            code_node = next(n for n in after_fail1["nodes"] if n["node_id"] == "code")
            self.assertEqual(code_node["state"], "ready")
            self.assertEqual(code_node["generation"], 2)

            # Re-run code node -> receives concrete test failure feedback in prompt!
            self.assertTrue(self.app.runs.work_once())
            self.assertTrue(any("case-ok" in prompt and "CL-01" in prompt for prompt in code_calls))

            # Round 2: integrate & fail test again -> exceeds max_fix_rounds=1, MUST NOT auto-rework again!
            detail = self.call("GET", f"runs/{rid}")[1]
            status, _ = self.call(
                "POST", f"runs/{rid}/integrations",
                {"selected": ["code"], "reason": "整合二轮候选"},
                rev=detail["revision"],
            )
            self.assertEqual(status, 202)
            self.assertTrue(self.app.delivery.work_once())

            detail = self.call("GET", f"runs/{rid}")[1]
            status, _ = self.call(
                "POST", f"runs/{rid}/tests",
                {"candidate_sha": detail["candidate_sha"], "suite_id": "app-suite"},
                rev=detail["revision"],
            )
            self.assertEqual(status, 202)
            self.assertTrue(self.app.delivery.work_once())

            after_fail2 = self.call("GET", f"runs/{rid}")[1]
            code_node_2 = next(n for n in after_fail2["nodes"] if n["node_id"] == "code")
            self.assertEqual(code_node_2["state"], "succeeded")
            self.assertEqual(code_node_2["generation"], 2)  # NOT bumped to 3!

        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])


if __name__ == "__main__":
    unittest.main()
