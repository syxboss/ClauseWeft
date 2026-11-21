"""Frozen skill inputs, request evidence, checkpoint authority and audited access."""

from contextlib import redirect_stdout
import hashlib
import io
import http.client
from http.server import ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import uuid4

from clauseweft import audit
from clauseweft.audit.__main__ import verify_file
from clauseweft.api.application import Application
from clauseweft.api.server import Handler, Problem
from clauseweft.storage import canonical
from platform_fixture import prepare_team


class HarnessTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "data_scope": "public",
                    "gateways": {"g": {"url": "http://127.0.0.1:8765"}},
                }
            )
        )
        self.app = Application(self.root / "state", config)
        prepare_team(self.app)
        self.calls = []

        def generate(endpoint, messages, limit):
            self.calls.append((endpoint["endpoint_id"], messages, limit))
            return (
                "已核对",
                {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
                uuid4().hex,
            )

        self.app.catalog.generate = generate
        for name, location, price in (
            ("local", "local", 2_000_000),
            ("cloud", "cloud", 1_000_000),
        ):
            self.app.catalog.save(
                dict(
                    endpoint_id=name,
                    model_identity=name,
                    identity_verified=True,
                    gateway_id="g",
                    gateway_etag='"fixture"',
                    enabled=True,
                    allowed=True,
                    location=location,
                    context_tokens=32000,
                    capabilities=[],
                    fit={"review": 5},
                    priority=1,
                    input_micros_per_million=price,
                    output_micros_per_million=price,
                )
            )

        prepare_team(self.app)

    def call(self, method, path, body=None, revision=None, actor=None):
        return self.app.handle(
            method, tuple(path.split("/")), {}, body or {}, uuid4().hex, revision, actor
        )[1]

    def post(self, path, body, revision=None):
        return self.call("POST", path, body, revision)

    def template(self, skill):
        role = self.post(
            "roles",
            {
                "name": "核验员",
                "prompt": "检查访问权限",
                "stage": "review",
                "skill_ids": [skill["id"]],
            },
        )
        template = self.post(
            "templates",
            {
                "name": "技能核验",
                "nodes": [
                    {"node_id": "review", "role_id": role["id"], "stage": "review"},
                    {"node_id": "approve", "kind": "gate", "dependencies": ["review"]},
                    {
                        "node_id": "summary",
                        "kind": "model",
                        "stage": "review",
                        "dependencies": ["approve"],
                    },
                ],
            },
        )
        self.post("templates/" + template["id"] + "/publish", {}, template["revision"])
        return template

    def start(self, template=None, **fields):
        body = {
            "task": {"text": "评审导出权限", "stage": "review", "data_scope": "public"},
            "budget": {"currency": "USD", "limit_micros": 10000},
            **fields,
        }
        if template:
            body["template_id"] = template["id"]
        return self.post("runs", body)

    def test_frozen_skill_and_exact_messages_survive_edits_and_rework(self):
        skill = self.post(
            "skills", {"name": "权限清单", "instructions": "技能第一版：检查服务端角色"}
        )
        template = self.template(skill)
        self.call(
            "PATCH",
            "skills/" + skill["id"],
            {"instructions": "技能第二版：检查筛选"},
            skill["revision"],
        )
        run = self.start(template)
        self.assertTrue(self.app.runs.work_once())
        self.assertEqual(self.calls[0][0], "local")
        self.assertIn("技能第一版", canonical(self.calls[0][1]))
        self.assertNotIn("技能第二版", canonical(self.calls[0][1]))
        context = self.call("GET", "runs/" + run["run_id"] + "/context")["items"][0]
        self.assertEqual(context["messages"], self.calls[0][1])
        self.assertEqual(context["skills"][0]["revision"], 1)
        raw = self.app.store.read_blob(context["context_sha256"])
        self.assertEqual(hashlib.sha256(raw).hexdigest(), context["context_sha256"])
        self.app.runs.invalidate(run["run_id"], "review", "检查回收权限")
        self.app.runs.work_once()
        contexts = self.call("GET", "runs/" + run["run_id"] + "/context")["items"]
        self.assertEqual(len(contexts), 2)
        self.assertEqual(contexts[0]["context_sha256"], context["context_sha256"])
        self.assertNotIn("检查回收权限", canonical(contexts[0]["messages"]))
        self.assertIn("检查回收权限", canonical(contexts[1]["messages"]))
        history = self.call("GET", "skills/" + skill["id"] + "/history")["items"]
        self.assertEqual([item["revision"] for item in history], [2, 1])

    def test_skill_revocation_after_claim_stops_dispatch(self):
        skill = self.post(
            "skills",
            {
                "name": "内部约束",
                "instructions": "检查内部接口",
                "data_scope": "public",
            },
        )
        run = self.start(skill_ids=[skill["id"]])
        attempt = self.app.runs.claim()
        self.call(
            "PATCH", "skills/" + skill["id"], {"enabled": False}, skill["revision"]
        )
        self.assertIsNone(self.app.runs.dispatch(attempt))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.app.run(run["run_id"])["budget"]["held_micros"], 0)

    def test_direct_role_freezes_server_prompt_and_combines_skills_once(self):
        shared = self.post("skills", {"name": "评审清单", "instructions": "检查导出权限"})
        extra = self.post("skills", {"name": "格式要求", "instructions": "列出验证步骤"})
        role = self.post("roles", {
            "name": "审阅者", "stage": "review", "prompt": "逐项给出依据",
            "endpoint_id": "local", "skill_ids": [shared["id"]],
        })
        request = {
            "task": {"text": "核对附件", "data_scope": "public"},
            "role_id": role["id"], "skill_ids": [shared["id"], extra["id"]],
        }
        preview = self.post("routing/previews", request)
        self.assertEqual((preview["intent"], preview["selected_endpoint"]), ("review", "local"))
        run = self.post("runs", request)
        self.call("PATCH", "roles/" + role["id"], {"name": "新版", "prompt": "新指令"}, role["revision"])
        self.call("PATCH", "skills/" + shared["id"], {"instructions": "新清单"}, shared["revision"])
        self.assertTrue(self.app.runs.work_once())
        path = "runs/" + run["run_id"]
        result = self.call("GET", path)
        self.assertEqual(result["status"], "completed")
        self.assertEqual((result["role_id"], result["role_name"], result["role_revision"]),
                         (role["id"], "审阅者", 1))
        self.assertEqual(result["task"]["text"], "核对附件")
        context, = self.call("GET", path + "/context")["items"]
        self.assertEqual(context["messages"][0], {"role": "system", "content": "逐项给出依据"})
        self.assertEqual(context["messages"][-1], {"role": "user", "content": "核对附件"})
        self.assertEqual([s["id"] for s in context["skills"]], [shared["id"], extra["id"]])
        self.assertIn("检查导出权限", context["messages"][1]["content"])
        self.assertEqual(context["data_scope"], "local_only")

    def test_role_handoff_checks_workspace_and_endpoint_binding(self):
        other = self.post("roles", {"name": "示例角色", "workspace_id": "example"})
        with self.assertRaises(Problem) as denied:
            self.start(role_id=other["id"])
        self.assertEqual(denied.exception.code, "WORKSPACE_FORBIDDEN")
        role = self.post("roles", {"name": "本地评审", "endpoint_id": "local"})
        for conflict in (
            {"task": {"text": "核对附件", "pinned_endpoint": "cloud"}},
            {"pinned_endpoints": {"task": "cloud"}},
            {"mode": "complex"},
            {"role_snapshot": {"id": role["id"], "revision": 9}},
        ):
            with self.subTest(conflict=conflict), self.assertRaises(ValueError):
                self.start(role_id=role["id"], **conflict)

    def test_checkpoint_fork_reuses_model_evidence_and_reauthorises_gates(self):
        skill = self.post("skills", {"name": "评审", "instructions": "检查角色和边界"})
        run = self.start(self.template(skill))
        rid = run["run_id"]
        self.app.runs.work_once()
        current = self.app.run(rid)
        self.post(
            "runs/" + rid + "/gates/approve",
            {"action": "approve", "reason": "原运行已确认"},
            current["revision"],
        )
        self.app.runs.work_once()
        current = self.app.run(rid)
        self.assertEqual(current["status"], "completed")
        checkpoint = self.post(
            "runs/" + rid + "/checkpoints",
            {"label": "已完成核验", "reason": "保留复用起点"},
            current["revision"],
        )
        self.assertEqual(len(checkpoint["node_states"]), 3)
        current = self.app.run(rid)
        fork = self.post(
            "runs/" + rid + "/forks",
            {
                "checkpoint_id": checkpoint["id"],
                "title": "新审批分支",
                "reason": "独立复核",
                "budget": {"currency": "USD", "limit_micros": 2000},
            },
            current["revision"],
        )
        self.assertNotEqual(fork["run_id"], rid)
        states = {node["node_id"]: node for node in fork["nodes"]}
        self.assertEqual(states["review"]["state"], "succeeded")
        self.assertEqual(states["review"]["inherited_from"]["run_id"], rid)
        self.assertEqual(states["approve"]["state"], "needs_input")
        self.assertEqual(states["summary"]["state"], "waiting")
        self.assertEqual(fork["budget"]["settled_micros"], 0)
        self.assertEqual(fork["budget"]["limit_micros"], 2000)
        inherited = self.call("GET", "runs/" + fork["run_id"] + "/context")["items"]
        self.assertEqual(inherited[0]["inherited_from"]["run_id"], rid)
        self.post(
            "runs/" + fork["run_id"] + "/gates/approve",
            {"action": "approve", "reason": "新运行重新批准"},
            fork["revision"],
        )
        self.app.runs.work_once()
        self.assertEqual(self.app.run(fork["run_id"])["status"], "completed")
        self.assertEqual(len(self.calls), 3)
        self.assertGreater(self.app.run(fork["run_id"])["budget"]["settled_micros"], 0)
        self.assertEqual(self.app.run(rid)["budget"], current["budget"])

    def test_checkpoint_refuses_inflight_and_context_views_redact_credentials(self):
        run = self.start()
        attempt = self.app.runs.claim()
        with self.assertRaises(Problem) as busy:
            self.post(
                "runs/" + run["run_id"] + "/checkpoints",
                {"label": "不稳定", "reason": "禁止保存在途状态"},
                self.app.run(run["run_id"])["revision"],
            )
        self.assertEqual(busy.exception.code, "CHECKPOINT_BUSY")
        self.app.runs.dispatch(attempt)
        self.app.runs.finish(
            attempt,
            text="已完成",
            receipt="first",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )
        original = self.app.run(run["run_id"])
        self.app.runs.invalidate(
            run["run_id"], "task", "Authorization: Bearer synthetic-fixture-value"
        )
        self.app.runs.work_once()
        view = self.call("GET", "runs/" + run["run_id"] + "/context")["items"][-1]
        self.assertTrue(view["redacted"])
        self.assertNotIn("synthetic-fixture-value", canonical(view))
        self.assertNotEqual(view["view_sha256"], view["context_sha256"])
        self.assertEqual(original["status"], "completed")

    def test_full_audit_records_denials_reads_deployment_and_exports(self):
        member = self.post("members", {"username": "readonly", "display_name": "审阅人", "role": "viewer", "password": "readonly-test-password", "workspace_ids": ["default"], "project_ids": ["default"]})
        viewer = {"id": member["id"], "display_name": "审阅人", "role": "viewer"}
        with self.assertRaises(Problem):
            self.call(
                "POST",
                "skills",
                {"name": "禁止写入", "instructions": "test"},
                actor=viewer,
            )
        self.call("GET", "projects", actor=viewer)
        rows = self.call("GET", "audit")["items"]
        self.assertTrue(
            any(
                row["outcome"] == "denied" and row["actor_id"] == member["id"]
                for row in rows
            )
        )
        self.assertTrue(
            any(
                row["outcome"] == "read" and row["actor_id"] == member["id"]
                for row in rows
            )
        )
        deployment = next(row for row in rows if row["action"] == "deployment.started")
        manifest = deployment["after"]["manifest"]
        self.assertTrue(any(name.startswith("clauseweft/") and name.endswith(".py") for name in manifest))
        for asset in ("page.html", "scripts/events.js", "styles/shell.css"):
            content = files("clauseweft.api.web").joinpath(asset).read_text(encoding="utf-8")
            self.assertEqual(manifest["clauseweft/api/web/" + asset], hashlib.sha256(content.encode()).hexdigest())
        bundle = self.call("GET", "audit/exports")
        self.assertTrue(
            audit.verify_entries(bundle["entries"], bundle["anchor"])["valid"]
        )
        self.assertEqual(bundle["evidence_status"], "complete")
        self.assertTrue(
            any(
                item.get("view", {}).get("schema") == "clauseweft-source-snapshot-v1"
                for item in bundle["evidence"]
                if isinstance(item.get("view"), dict)
            )
        )

    def test_offline_export_verifies_sidecars_and_detects_modification(self):
        bundle = self.call("GET", "audit/exports")
        export_path = self.root / "audit.json"
        export_path.write_text(json.dumps(bundle))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(verify_file(export_path), 0)
        bundle["business_records"]["mw_spec"].append({"id": "forged"})
        export_path.write_text(json.dumps(bundle))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(verify_file(export_path), 1)

    def test_inherited_context_requires_the_persisted_checkpoint_source(self):
        original = self.post("runs", {"task": {"text": "本项目证据", "stage": "review"}, "budget": {"currency": "USD", "limit_micros": 100000}})
        self.assertTrue(self.app.runs.work_once())
        original = self.call("GET", "runs/" + original["run_id"])
        self.assertEqual(original["status"], "completed")
        checkpoint = self.post("runs/" + original["run_id"] + "/checkpoints", {"label": "可靠来源", "reason": "保留来源关系"}, original["revision"])
        original = self.call("GET", "runs/" + original["run_id"])
        fork = self.post("runs/" + original["run_id"] + "/forks", {"checkpoint_id": checkpoint["id"], "title": "继承证据", "reason": "核对项目边界", "budget": {"currency": "USD", "limit_micros": 100000}}, original["revision"])
        self.assertEqual(len(self.call("GET", "runs/" + fork["run_id"] + "/context")["items"]), 1)
        other_project = self.post("projects", {"name": "另一个项目"})
        other = self.post("runs", {"project_id": other_project["id"], "task": {"text": "其他项目的输入", "stage": "review"}, "budget": {"currency": "USD", "limit_micros": 100000}})
        self.assertTrue(self.app.runs.work_once())
        other_attempt = self.call("GET", "runs/" + other["run_id"] + "/attempts")["items"][0]["attempt_id"]
        # A legacy record with an invalid source must not be treated as a valid fork.
        with self.app.store.transaction() as db:
            row = db.execute("SELECT definition FROM nodes WHERE run_id=?", (fork["run_id"],)).fetchone()
            definition = json.loads(row["definition"])
            definition["inherited_from"]["attempt_id"] = other_attempt
            db.execute("UPDATE nodes SET definition=? WHERE run_id=?", (canonical(definition), fork["run_id"]))
        with self.assertRaises(Problem) as rejected:
            self.call("GET", "runs/" + fork["run_id"] + "/context")
        self.assertEqual(rejected.exception.code, "INVALID_CONTEXT_PROVENANCE")


class AuditedHTTPTest(unittest.TestCase):
    def test_invalid_auth_and_malformed_requests_are_audited(self):
        with TemporaryDirectory() as directory:
            app = Application(directory)
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                server.app = app
                server.authorization = b"Bearer fixture"
                worker = threading.Thread(target=server.serve_forever, daemon=True)
                worker.start()
                try:
                    for body, headers, expected in (
                        (None, {"Authorization": "Bearer wrong"}, 401),
                        (
                            "{",
                            {
                                "Authorization": "Bearer fixture",
                                "Content-Type": "application/json",
                            },
                            422,
                        ),
                    ):
                        connection = http.client.HTTPConnection(
                            "127.0.0.1", server.server_port
                        )
                        connection.request("POST", "/api/v1/projects", body, headers)
                        response = connection.getresponse()
                        self.assertEqual(response.status, expected)
                        self.assertEqual(len(response.getheader("X-Request-ID")), 32)
                        response.read()
                        connection.close()
                    with app.store.transaction() as db:
                        rows = audit.list_entries(db)
                        self.assertTrue(
                            any(
                                row["outcome"] == "denied"
                                and row["details"].get("status") == 401
                                for row in rows
                            )
                        )
                        self.assertTrue(
                            any(
                                row["outcome"] == "failed"
                                and row["details"].get("status") == 422
                                for row in rows
                            )
                        )
                        self.assertTrue(audit.verify(db)["valid"])
                finally:
                    server.shutdown()
                    worker.join(2)


if __name__ == "__main__":
    unittest.main()
