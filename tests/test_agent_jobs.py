import http.client
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest.mock import patch

from clauseweft.api.application import Application
from clauseweft.api.http_contracts import Problem
from platform_fixture import prepare_team


def git(repo, *args, data=None):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test", "-c", "user.email=test@localhost",
                           "-c", "core.hooksPath=/dev/null", *args], input=data, capture_output=True,
                          check=True).stdout


class AgentJobsTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / "source"
        self.repo.mkdir()
        git(self.repo, "init", "--template=")
        for name, content in {"hello.txt": "old\n", "drop.txt": "remove\n", "ignored.md": "private",
                              ".env": "private", "key.txt": "-----BEGIN PRIVATE KEY-----",
                              "binary": "x\0"}.items():
            (self.repo / name).write_text(content)
        (self.repo / "link").symlink_to("ignored.md")
        git(self.repo, "add", ".")
        git(self.repo, "commit", "-m", "base")
        self.sha = git(self.repo, "rev-parse", "HEAD").decode().strip()
        self.cfg = {"data_scope": "public", "repositories": {"repo": {
            "path": str(self.repo), "data_scope": "public",
            "allowlist": ["hello.txt", "drop.txt", "new.txt", "binary", "link", "key.txt"]}},
            "agent_runners": {key: {"kind": kind, "image": "sha256:" + "9" * 64,
                "executable": path, "token_env": env, "timeout_seconds": 180} for key, kind, path, env in (
                    ("editor", "claude", "/usr/bin/claude", "MY_CLAUDE_KEY"),
                    ("reader", "deepseek", "/usr/bin/dsh", "MY_DEEPSEEK_KEY"))}}
        self.config = self.root / "config.json"
        self.reopen()
        prepare_team(self.app)
        self.body = dict(project_id="default", repo_id="repo", runner_id="editor", prompt="update greeting")

    def reopen(self):
        self.config.write_text(json.dumps(self.cfg))
        self.app = Application(self.root / "state", self.config)

    def create(self, body=None, key="first"):
        return self.app.handle("POST", ("agent-jobs",), {}, body or self.body, key)[1]

    def detail(self, item):
        return self.app.handle("GET", ("agent-jobs", item["id"]), {}, {})[1]

    def test_external_sources_and_prompt_do_not_capture_service_credentials(self):
        # Deliberately use fixture strings without any provider-specific prefix.
        with patch.dict(os.environ, {"MY_DEEPSEEK_KEY": "test-foreign-sentinel",
                                     "CLAUSEWEFT_CONSOLE_TOKEN": "test-personal-sentinel"}):
            with self.assertRaisesRegex(ValueError, "凭据"):
                self.create(dict(self.body, prompt="note test-foreign-sentinel"), key="bad-prompt")
            (self.repo / "hello.txt").write_text("test-personal-sentinel")
            git(self.repo, "add", "hello.txt")
            git(self.repo, "commit", "-m", "test material")
            with self.assertRaisesRegex(ValueError, "凭据"):
                self.create(key="bad-input")
            items = self.app.handle("GET", ("agent-jobs",), {"project_id": "default"}, {})[1]["items"]
            self.assertEqual(items, [])

    def test_rotated_secret_in_removed_lines_blocks_review_dispatch(self):
        marker = "prior-demo-credential"
        (self.repo / "hello.txt").write_text(marker + "\n")
        git(self.repo, "add", "hello.txt")
        git(self.repo, "commit", "-m", "set up removal")
        edit_job = self.create()

        def edit(runner, path, prompt, **kwargs):
            (path / "hello.txt").write_text("replaced\n")
            return {"text": "edited"}

        with patch("clauseweft.agent_runtime.run_agent", edit):
            self.app.agent_jobs.work_once()
        self.assertEqual(self.detail(edit_job)["status"], "succeeded")
        review = self.create(dict(self.body, runner_id="reader", review_of=edit_job["id"]), "review")
        with patch.dict(os.environ, {"MY_CLAUDE_KEY": marker}), patch("clauseweft.agent_runtime.run_agent") as run:
            self.app.agent_jobs.work_once()
            run.assert_not_called()
        self.assertEqual(self.detail(review)["status"], "failed")

    def test_reserved_path_is_rejected_before_opening_it(self):
        from clauseweft.api.agent_workspace import materialize

        for index, name in enumerate(("CON.txt", "nested/LPT1", "data/AUX.log")):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "路径"):
                materialize(self.root / f"copy{index}", {name: ""})

    def test_pinned_snapshot_patch_followup_read_only_and_audit(self):
        job = self.create()
        self.assertEqual(self.create(), job)
        self.assertEqual(job["base_sha"], self.sha)
        (self.repo / "hello.txt").write_text("uncommitted secret content")
        def edit(runner, path, prompt, *, on_event, cancelled):
            self.assertEqual({p.name for p in path.iterdir()}, {"hello.txt", "drop.txt"})
            self.assertEqual((path / "hello.txt").read_text(), "old\n")
            self.assertFalse(cancelled())
            on_event({"type": "tool", "tool": "Edit", "secret": "ignored"})
            (path / "hello.txt").write_text("new\n")
            (path / "new.txt").write_text("brand new")
            (path / "drop.txt").unlink()
            return {"text": "changed", "session_id": "session42", "usage": {"output_tokens": 8}}
        with patch("clauseweft.agent_runtime.run_agent", edit):
            self.assertTrue(self.app.agent_jobs.work_once())
        result = self.detail(job)
        self.assertEqual(result["status"], "succeeded", result)
        self.assertEqual(set(result["paths"]), {"hello.txt", "drop.txt", "new.txt"})
        self.assertEqual((self.repo / "hello.txt").read_text(), "uncommitted secret content")
        git(self.repo, "apply", "--check", "--cached", "-", data=result["patch"].encode())
        self.assertEqual(result["events"][2]["tool"], "Edit")
        self.assertNotIn("secret", result["events"][2])
        with self.app.store.transaction() as db:
            other = self.app.platform.save(db, "projects", {"name": "Other", "workspace_id": "default"})
        with self.assertRaises(Problem) as foreign:
            self.create(dict(self.body, runner_id="reader", review_of=job["id"], project_id=other["id"]), "foreign")
        self.assertEqual(foreign.exception.code, "INVALID_REVIEW")
        review = self.create(dict(self.body, runner_id="reader", review_of=job["id"]), "review")
        def read(runner, path, prompt, **_):
            self.assertIn(self.sha, prompt)
            self.assertEqual((path / ".clauseweft-review.patch").read_text(), result["patch"])
            self.assertEqual((path / "hello.txt").read_text(), "new\n")
            self.assertEqual((path / "new.txt").read_text(), "brand new")
            self.assertFalse((path / "drop.txt").exists())
            return {"text": "looks good"}
        with patch("clauseweft.agent_runtime.run_agent", read):
            self.app.agent_jobs.work_once()
        self.assertEqual(self.detail(review)["text"], "looks good")
        self.assertEqual(self.detail(review)["patch"], "")
        with self.app.store.transaction() as db:
            from clauseweft import audit
            self.assertTrue(audit.verify(db)["valid"])

    def test_scope_refresh_cancel_and_interruption(self):
        self.app.scope = "local_only"
        with self.assertRaises(Problem) as scope:
            self.create()
        self.assertEqual(scope.exception.status, 403)
        self.app.scope = "public"
        job = self.create()
        self.cfg["repositories"]["repo"]["data_scope"] = "local_only"
        self.reopen()
        with patch("clauseweft.agent_runtime.run_agent") as runner:
            self.app.agent_jobs.work_once()
            runner.assert_not_called()
        self.assertEqual(self.detail(job)["status"], "failed")
        self.cfg["repositories"]["repo"]["data_scope"] = "public"
        self.reopen()
        job = self.create(key="second")
        with self.assertRaises(Problem) as precondition:
            self.app.handle("POST", ("agent-jobs", job["id"], "cancel"), {}, {}, "cancel")
        self.assertEqual(precondition.exception.status, 428)
        self.app.handle("POST", ("agent-jobs", job["id"], "cancel"), {}, {}, "cancel", job["revision"])
        self.assertFalse(self.app.agent_jobs.work_once())
        self.assertEqual(self.detail(job)["status"], "cancelled")
        job = self.create(key="third")
        with self.app.store.transaction() as db:
            self.app.agent_jobs.transition(db, self.app.agent_jobs.row(db, job["id"]), "running")
        self.reopen()
        self.assertEqual(self.detail(job)["status"], "interrupted")
        self.assertFalse(self.app.agent_jobs.work_once())

    def test_one_worker_claim_and_cancel_late_result(self):
        job = self.create()
        entered, release = threading.Event(), threading.Event()
        def delayed(runner, path, prompt, *, on_event, cancelled):
            entered.set()
            release.wait(5)
            self.assertTrue(cancelled())
            return {"text": "late"}
        with patch("clauseweft.agent_runtime.run_agent", delayed):
            thread = threading.Thread(target=self.app.agent_jobs.work_once)
            thread.start()
            try:
                self.assertTrue(entered.wait(4))
                self.assertFalse(self.app.agent_jobs.work_once())
                current = self.detail(job)
                self.app.handle("POST", ("agent-jobs", job["id"], "cancel"), {}, {}, "cancel", current["revision"])
            finally:
                release.set()
                thread.join(7)
        result = self.detail(job)
        self.assertEqual(result["status"], "cancelled")
        self.assertNotIn("text", result)

    def test_refuses_unsafe_outputs_and_read_only_writes(self):
        for i, name in enumerate(("ignored.md", "link", ".git", "binary", "key.txt", "review")):
            job = self.create(dict(self.body, runner_id="reader" if name == "review" else "editor"), str(i))
            def unsafe(runner, path, prompt, **_):
                if name == "link":
                    (path / name).symlink_to(self.repo / "ignored.md")
                else:
                    (path / ("hello.txt" if name == "review" else name)).write_text("replacement")
                return {"text": "ignore me"}
            with patch("clauseweft.agent_runtime.run_agent", unsafe):
                self.app.agent_jobs.work_once()
            result = self.detail(job)
            self.assertEqual(result["status"], "failed", name)
            self.assertNotIn("patch", result)

    def test_http_list_auth_idempotency_and_cancel_preconditions(self):
        from http.server import ThreadingHTTPServer
        from clauseweft.api.server import Handler
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.app = self.app
        server.authorization = b"Bearer local-test-only"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        def call(method, suffix, data=None, auth=True, **extra):
            headers = {"Content-Type": "application/json", **extra}
            if auth:
                headers["Authorization"] = "Bearer local-test-only"
            with_connection = http.client.HTTPConnection(*server.server_address, timeout=3)
            try:
                with_connection.request(method, "/api/v1/agent-jobs" + suffix,
                                        body=json.dumps(data) if data is not None else None, headers=headers)
                response = with_connection.getresponse()
                return response.status, json.load(response)
            finally:
                with_connection.close()
        self.assertEqual(call("GET", "/options?project_id=default", auth=False)[0], 401)
        status, config = call("GET", "/options?project_id=default")
        self.assertEqual((status, config["repositories"]), (200, [{"id": "repo"}]))
        self.assertIn({"id": "editor", "kind": "claude"}, config["runners"])
        self.assertEqual(call("POST", "", self.body)[0], 422)
        header = {"Idempotency-Key": "action"}
        status, job = call("POST", "", self.body, **header)
        self.assertEqual(status, 201)
        self.assertEqual(call("POST", "", self.body, **header)[1]["id"], job["id"])
        self.assertEqual(call("GET", "?project_id=default")[1]["items"][0]["id"], job["id"])
        self.assertEqual(call("GET", "/" + job["id"])[1]["title"], self.body["prompt"])
        cancel = "/" + job["id"] + "/cancel"
        self.assertEqual(call("POST", cancel, {}, **header)[0], 428)
        self.assertEqual(call("POST", cancel, {}, **header, **{"If-Match": '"2"'})[0], 412)
        self.assertEqual(call("POST", cancel, {}, **header, **{"If-Match": '"1"'})[1]["status"], "cancelled")

    def test_project_binding_rechecked_for_queued_and_running_jobs(self):
        self.cfg["repositories"]["second"] = dict(self.cfg["repositories"]["repo"])
        self.reopen()
        prepare_team(self.app)
        def bind(repo_id):
            with self.app.store.transaction() as db:
                project = self.app.platform.get(db, "projects", "default")
                self.app.platform.save(db, "projects", {"repository_id": repo_id}, "default", project["revision"])
        first = self.create()
        bind("second")
        options = self.app.handle("GET", ("agent-jobs", "options"), {"project_id": "default"}, {})[1]
        self.assertEqual(options["repositories"], [{"id": "second"}])
        with self.assertRaises(Problem):
            self.create(key="wrong-project")
        with patch("clauseweft.agent_runtime.run_agent") as worker:
            self.app.agent_jobs.work_once()
            worker.assert_not_called()
        self.assertEqual(self.detail(first)["status"], "failed")
        second = self.create(dict(self.body, repo_id="second"), "permitted")
        def revoke(runner, path, prompt, *, cancelled, **_):
            self.assertFalse(cancelled())
            bind("repo")
            self.assertTrue(cancelled())
            return {"text": "late provider output"}
        with patch("clauseweft.agent_runtime.run_agent", revoke):
            self.app.agent_jobs.work_once()
        detail = self.detail(second)
        self.assertEqual(detail["status"], "failed")
        self.assertNotIn("text", detail)
        self.assertNotIn("patch", detail)

    def test_owner_and_project_checks_apply_before_replay(self):
        job = self.create()
        with self.app.store.transaction() as db:
            owner = self.app.platform.add_member(db, {
                "username": "otheradmin", "display_name": "other", "password": "long test passphrase", "role": "admin"})
            guest = self.app.platform.add_member(db, {
                "username": "otheruser", "display_name": "guest", "password": "long test passphrase", "role": "operator"})
        own_summary = self.app.handle("GET", ("overview",), {}, {})[1]["agent_jobs"]
        self.assertEqual(own_summary["active"], 1)
        self.assertEqual([r["id"] for r in own_summary["recent"]], [job["id"]])
        self.assertFalse(self.app.handle("GET", ("overview",), {}, {}, principal=owner)[1]["agent_jobs"])
        self.assertFalse(self.app.handle("GET", ("overview",), {"workspace_id": "example"}, {})[1]["agent_jobs"])
        for actor, code in ((owner, 404), (guest, 403)):
            with self.assertRaises(Problem) as forbidden:
                self.app.handle("GET", ("agent-jobs", job["id"]), {}, {}, principal=actor)
            self.assertEqual(forbidden.exception.status, code)
        with self.assertRaises(ValueError):
            self.create(dict(self.body, base_sha="refs/heads/main"))
        self.app.scope = "local_only"
        with self.assertRaises(Problem):
            self.create()
