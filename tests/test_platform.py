"""Team account permissions, SOP release snapshots and project-owned tasks."""

import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from uuid import uuid4

from clauseweft.api.application import Application
from clauseweft.api.server import Handler, Problem
from platform_fixture import prepare_team


class PlatformTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = Application(self.root / "state")
        prepare_team(self.app)

    def call(
        self,
        method,
        path,
        data=None,
        *,
        revision=None,
        key=None,
        actor=None,
        query=None,
    ):
        return self.app.handle(
            method,
            tuple(path.split("/")),
            query or {},
            data or {},
            key or uuid4().hex,
            revision,
            actor,
        )[1]

    def post(self, path, data, **kwargs):
        return self.call("POST", path, data, **kwargs)

    def test_first_delivery_template_uses_engine_graph_without_duplicating_prompts(self):
        from clauseweft import workflow

        template = self.post("templates", {"name": "交付", "execution_mode": "workflow"})
        expected = workflow.compile({"mode": "workflow"})
        self.assertEqual(template["nodes"], [dict(node, prompt="") for node in expected])
        self.post("templates/" + template["id"] + "/publish", {}, revision=1)
        versions = self.call("GET", "templates/" + template["id"] + "/versions")
        frozen = versions["items"][0]
        compiled = workflow.compile({"mode": "workflow", "template_snapshot": frozen})
        self.assertEqual([n["prompt"].strip() for n in compiled], [n["prompt"].strip() for n in expected])
        with self.assertRaises(ValueError):
            self.post("templates", {"name": "显式空节点", "execution_mode": "workflow", "nodes": []})
        with self.assertRaises(ValueError):
            self.post("templates", {"name": "空自定义", "execution_mode": "sop"})

    def test_project_requirement_idempotency_and_stale_revision(self):
        data = {"name": "交易平台", "description": "内部研发项目"}
        project = self.post("projects", data, key="create-project")
        self.assertEqual(project, self.post("projects", data, key="create-project"))
        requirement = self.post(
            "requirements", {"title": "导出账单", "project_id": project["id"]}
        )
        self.assertEqual(requirement["status"], "backlog")
        changed = self.call(
            "PATCH",
            "requirements/" + requirement["id"],
            {"status": "ready"},
            revision=1,
        )
        self.assertEqual(changed["revision"], 2)
        with self.assertRaises(Problem) as stale:
            self.call(
                "PATCH",
                "requirements/" + requirement["id"],
                {"status": "done"},
                revision=1,
            )
        self.assertEqual(stale.exception.status, 412)
        other = self.post("projects", {"name": "另一项目"})
        with self.assertRaises(ValueError):
            self.post(
                "runs",
                {
                    "project_id": other["id"],
                    "requirement_id": requirement["id"],
                    "task": {"text": "评审"},
                },
            )
        self.assertEqual(self.call("GET", "runs")["items"], [])
        raw = self.call("GET", "audit")["items"]
        business_actions = {"post.projects", "post.requirements", "patch.requirements"}
        self.assertEqual(sum(row["action"] in business_actions for row in raw), 4)
        self.assertTrue(any(row["outcome"] == "failed" for row in raw))
        with self.assertRaises(Problem) as ownership:
            self.call("PATCH", "requirements/" + requirement["id"], {"project_id": other["id"]}, revision=2)
        self.assertEqual(ownership.exception.status, 409)
        moved = self.post("requirements", {"title": "另一个项目的独立需求", "project_id": other["id"]})
        run = self.post(
            "runs",
            {
                "project_id": other["id"],
                "requirement_id": moved["id"],
                "task": {"text": "评审"},
            },
        )
        with self.assertRaises(Problem) as linked:
            self.call(
                "PATCH",
                "requirements/" + moved["id"],
                {"project_id": project["id"]},
                revision=moved["revision"] + 1,
            )
        self.assertEqual(linked.exception.status, 409)
        current = self.call("GET", "requirements/" + moved["id"])
        self.assertEqual(current["project_id"], run["project_id"])

    def test_published_role_snapshot_and_sop_approval_are_real(self):
        # Use a configured local gateway, then replace only its generation transport.
        config = self.root / "config.json"
        config.write_text(
            json.dumps({"gateways": {"g": {"url": "http://127.0.0.1:8765"}}})
        )
        self.app = Application(self.root / "state", config)
        self.app.catalog.save(
            dict(
                endpoint_id="local",
                model_identity="local",
                identity_verified=True,
                gateway_id="g",
                gateway_etag='"fixture"',
                enabled=True,
                allowed=True,
                location="local",
                context_tokens=10000,
                capabilities=[],
                fit={"review": 5},
                priority=1,
                input_micros_per_million=0,
                output_micros_per_million=0,
            )
        )
        prepare_team(self.app)
        prompts = []

        def generate(endpoint, messages, limit):
            prompts.append(messages[0]["content"])
            return (
                "评审已完成",
                {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                uuid4().hex,
            )

        self.app.catalog.generate = generate
        project = self.post("projects", {"name": "支付服务"})
        requirement = self.post(
            "requirements", {"title": "评审权限", "project_id": project["id"]}
        )
        role = self.post(
            "roles", {"name": "权限专家", "stage": "review", "prompt": "检查权限来源"}
        )
        template = self.post(
            "templates",
            {
                "name": "权限审批",
                "execution_mode": "sop",
                "nodes": [
                    {
                        "node_id": "review",
                        "kind": "model",
                        "stage": "review",
                        "role_id": role["id"],
                        "dependencies": [],
                    },
                    {
                        "node_id": "approve",
                        "kind": "gate",
                        "stage": "review",
                        "dependencies": ["review"],
                    },
                ],
            },
        )
        tid = template["id"]
        self.post("templates/" + tid + "/publish", {}, revision=1)
        self.call(
            "PATCH", "roles/" + role["id"], {"prompt": "新的角色指令"}, revision=1
        )
        with self.assertRaises(Problem) as changed_role:
            self.post("templates/" + tid + "/publish", {}, revision=1)
        self.assertEqual(changed_role.exception.code, "SAVE_TEMPLATE_VERSION")
        run = self.post(
            "runs",
            {
                "title": "审核导出权限",
                "project_id": project["id"],
                "requirement_id": requirement["id"],
                "template_id": tid,
                "task": {"text": "评审管理员权限", "max_output_tokens": 40},
            },
        )
        self.assertTrue(self.app.runs.work_once())
        self.assertEqual(prompts, ["检查权限来源"])
        run = self.call("GET", "runs/" + run["run_id"])
        self.assertEqual(run["template_revision"], 1)
        self.assertEqual(run["nodes"][1]["state"], "needs_input")
        overview = self.call("GET", "overview", query={"project_id": project["id"]})
        self.assertEqual(overview["pending_approvals"], 1)
        approved = self.post(
            "runs/" + run["run_id"] + "/gates/approve",
            {"action": "approve", "reason": "已核对"},
            revision=run["revision"],
        )
        self.assertEqual(approved["status"], "completed")
        links = self.call("GET", "runs", query={"requirement_id": requirement["id"]})[
            "items"
        ]
        self.assertEqual([r["run_id"] for r in links], [run["run_id"]])
        self.assertEqual(
            self.call("GET", "requirements/" + requirement["id"])["run_status"],
            "completed",
        )
        self.call("PATCH", "templates/" + tid, {}, revision=1)
        published = self.post("templates/" + tid + "/publish", {}, revision=2)
        self.assertEqual(published["published_revision"], 2)
        versions = self.call("GET", "templates/" + tid + "/versions")["items"]
        self.assertEqual([version["revision"] for version in versions], [2, 1])
        self.assertEqual(versions[0]["nodes"][0]["prompt"], "新的角色指令")
        self.assertEqual(versions[1]["nodes"][0]["prompt"], "检查权限来源")
        self.assertEqual(versions[1]["node_count"], 2)
        self.assertEqual(versions[1]["actor_id"], "bootstrap-admin")

    def test_password_sessions_rbac_and_member_revocation(self):
        member = self.post(
            "members",
            {
                "username": "auditor",
                "display_name": "审计员",
                "role": "viewer",
                "password": "safe-demo-password",
                "workspace_ids": ["default"],
                "project_ids": ["default"],
            },
        )
        login = self.app.platform.login("auditor", "safe-demo-password")
        viewer = self.app.platform.authenticate(login["token"])
        self.assertEqual(self.call("GET", "auth/me", actor=viewer)["id"], member["id"])
        self.assertEqual(self.call("GET", "overview", actor=viewer)["projects"], 1)
        with self.assertRaises(Problem) as blocked:
            self.post("projects", {"name": "越权写入"}, actor=viewer)
        self.assertEqual(blocked.exception.status, 403)
        self.call(
            "PATCH", "members/" + member["id"], {"role": "operator"}, revision=1
        )
        operator = self.app.platform.authenticate(login["token"])
        self.assertEqual(operator["role"], "operator")
        self.post("projects", {"name": "研发项目"}, actor=operator)
        updated = next(row for row in self.call("GET", "members")["items"] if row["id"] == member["id"])
        with self.assertRaises(Problem):
            self.post(
                "members",
                {"username": "other", "password": "safe-demo-password"},
                actor=operator,
            )
        self.call(
            "PATCH",
            "members/" + member["id"],
            {"active": False},
            revision=updated["revision"],
        )
        with self.assertRaises(Problem) as revoked:
            self.app.platform.authenticate(login["token"])
        self.assertEqual(revoked.exception.status, 401)
        dump = json.dumps(self.call("GET", "members")) + json.dumps(
            self.call("GET", "audit")
        )
        self.assertNotIn("safe-demo-password", dump)
        self.assertNotIn("password_hash", dump)

    def test_existing_run_migrates_to_default_project_on_reopen(self):
        run = self.app.runs.start({"task": {"text": "评审已有需求"}})
        self.app = Application(self.root / "state")
        listed = self.call("GET", "runs")["items"]
        self.assertEqual(listed[0]["run_id"], run["run_id"])
        self.assertEqual(listed[0]["project_id"], "default")

    def test_concurrent_publish_cannot_replace_prepared_execution_plan(self):
        template = self.post(
            "templates",
            {
                "name": "发布竞争",
                "execution_mode": "sop",
                "nodes": [
                    {
                        "node_id": "approve",
                        "kind": "gate",
                        "stage": "review",
                        "dependencies": [],
                    }
                ],
            },
        )
        self.post("templates/" + template["id"] + "/publish", {}, revision=1)
        original_prepare = self.app.delivery.prepare_context
        from clauseweft import workflow
        from clauseweft.api.platform import BOOTSTRAP

        def concurrent_publish(plan):
            with self.app.store.transaction() as db:
                self.app.platform.save(
                    db,
                    "templates",
                    {
                        "execution_mode": "workflow",
                        "nodes": workflow.compile({"mode": "workflow"}),
                    },
                    template["id"],
                    1,
                )
                self.app.platform.publish(db, template["id"], 2, BOOTSTRAP)
            return original_prepare(plan)

        self.app.delivery.prepare_context = concurrent_publish
        run = self.post(
            "runs", {"template_id": template["id"], "task": {"text": "检查发布版本"}}
        )
        self.assertEqual(run["mode"], "sop")
        self.assertEqual(run["template_revision"], 1)
        self.assertEqual([node["node_id"] for node in run["nodes"]], ["approve"])
        self.assertEqual(run["nodes"][0]["state"], "needs_input")


class PlatformHTTPTest(unittest.TestCase):
    def test_password_login_logout_and_http_read_only_permission(self):
        with TemporaryDirectory() as root:
            app = Application(root)
            app.handle(
                "POST",
                ("members",),
                {},
                {
                    "username": "viewer",
                    "role": "viewer",
                    "password": "demo-password-123",
                },
                "member",
            )
            with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
                server.app = app
                server.authorization = b"Bearer bootstrap-fixture"
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                self.addCleanup(thread.join, 2)
                self.addCleanup(server.shutdown)

                def request(path, data=None, token=None):
                    conn = http.client.HTTPConnection("127.0.0.1", server.server_port)
                    headers = {
                        "Content-Type": "application/json",
                        "Idempotency-Key": uuid4().hex,
                    }
                    if token:
                        headers["Authorization"] = "Bearer " + token
                    conn.request(
                        "POST" if data is not None else "GET",
                        "/api/v1/" + path,
                        json.dumps(data) if data is not None else None,
                        headers,
                    )
                    response = conn.getresponse()
                    body = json.loads(response.read())
                    conn.close()
                    return response.status, body

                status, result = request(
                    "auth/login",
                    {"username": "viewer", "password": "demo-password-123"},
                )
                self.assertEqual(status, 200)
                token = result["token"]
                self.assertEqual(request("auth/me", token=token)[1]["role"], "viewer")
                self.assertEqual(
                    request("runs", {"task": {"text": "评审"}}, token)[0], 403
                )
                self.assertEqual(request("auth/logout", {}, token)[0], 200)
                self.assertEqual(request("auth/me", token=token)[0], 401)


if __name__ == "__main__":
    unittest.main()
