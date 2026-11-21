"""Exercise access boundaries before preparation, replay, nested evidence and dispatch."""

import http.client
from http.server import ThreadingHTTPServer
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
from uuid import uuid4

from clauseweft import audit
from clauseweft.api.application import Application
from clauseweft.api.server import Handler, Problem


class AuthorizationBoundaryTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.root = Path(scratch.name)
        config = self.root / "config.json"
        config.write_text(
            json.dumps({"gateways": {"g": {"url": "http://127.0.0.1:8765"}}})
        )
        self.app = Application(self.root / "state", config)
        with self.app.store.transaction() as db:
            self.own = self.app.platform.save(
                db, "projects", {"name": "授权项目"}, resource_id="default"
            )
            self.other = self.app.platform.save(
                db, "projects", {"name": "同空间私有项目"}
            )
            self.space = self.app.platform.save(db, "workspaces", {"name": "其他团队"})
            self.remote = self.app.platform.save(
                db,
                "projects",
                {"name": "跨空间私有项目", "workspace_id": self.space["id"]},
            )
            self.operator = self.app.platform.add_member(
                db,
                {
                    "username": "operator",
                    "password": "fixture-password-123",
                    "role": "operator",
                    "workspace_ids": ["default"],
                    "project_ids": [self.own["id"]],
                },
            )
            self.viewer = self.app.platform.add_member(
                db,
                {
                    "username": "viewer",
                    "password": "fixture-password-123",
                    "role": "viewer",
                    "workspace_ids": ["default"],
                    "project_ids": [self.own["id"]],
                },
            )
        self.own_run = self.gate_run(self.own)
        self.other_run = self.gate_run(self.other)
        self.remote_run = self.gate_run(self.remote)

    def call(
        self,
        method,
        path,
        body=None,
        *,
        actor=None,
        revision=None,
        key=None,
        query=None,
    ):
        return self.app.handle(
            method,
            tuple(path.split("/")),
            query or {},
            body or {},
            key or uuid4().hex,
            revision,
            actor,
        )[1]

    def gate_run(self, project):
        template = self.call(
            "POST",
            "templates",
            {
                "name": "私有人工确认",
                "workspace_id": project["workspace_id"],
                "nodes": [{"node_id": "approve", "kind": "gate", "dependencies": []}],
            },
        )
        self.call(
            "POST",
            "templates/" + template["id"] + "/publish",
            revision=template["revision"],
        )
        return self.call(
            "POST",
            "runs",
            {
                "project_id": project["id"],
                "workspace_id": project["workspace_id"],
                "template_id": template["id"],
                "task": {"text": "PRIVATE " + project["id"]},
            },
        )

    def deny(self, method, path, body=None, *, actor=None, query=None):
        with self.assertRaises(Problem, msg=method + " " + path) as denied:
            self.call(
                method,
                path,
                body,
                actor=actor or self.operator,
                query=query,
                revision=1,
            )
        self.assertIn(denied.exception.status, (401, 403, 404))
        return denied.exception

    def revoke(self, member=None, **data):
        member = member or self.operator
        with self.app.store.transaction() as db:
            current = next(
                m for m in self.app.platform.members(db) if m["id"] == member["id"]
            )
            return self.app.platform.update_member(
                db, member["id"], data or {"project_ids": []}, current["revision"]
            )

    def test_all_nested_run_evidence_and_actions_check_parent_project(self):
        read_suffixes = (
            "",
            "/events",
            "/attempts",
            "/context",
            "/checkpoints",
            "/specs",
            "/findings",
            "/artifacts/private-reference",
        )
        write_suffixes = (
            "/specs",
            "/findings/private/resolutions",
            "/approvals",
            "/integrations",
            "/tests",
            "/rework",
            "/cancel",
            "/pause",
            "/resume",
            "/reconciliations",
            "/exports",
            "/checkpoints",
            "/forks",
            "/gates/approve",
        )
        with patch.object(
            self.app.store,
            "read_blob",
            side_effect=AssertionError("unauthorized blob read"),
        ):
            for run in (self.other_run, self.remote_run):
                for actor in (self.operator, self.viewer):
                    for suffix in read_suffixes:
                        with self.subTest(
                            run=run["run_id"], actor=actor["role"], path=suffix
                        ):
                            self.deny(
                                "GET", "runs/" + run["run_id"] + suffix, actor=actor
                            )
                    for suffix in write_suffixes:
                        with self.subTest(
                            run=run["run_id"], actor=actor["role"], path=suffix
                        ):
                            self.deny(
                                "POST",
                                "runs/" + run["run_id"] + suffix,
                                {"reason": "attempt private action"},
                                actor=actor,
                            )
        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])
        self.assertEqual(
            self.call("GET", "runs/" + self.own_run["run_id"], actor=self.viewer)[
                "project_id"
            ],
            self.own["id"],
        )

    def test_nonadministrators_cannot_read_or_mutate_any_admin_root(self):
        paths = (
            "members",
            "members/other",
            "audit",
            "audit/verify",
            "audit/export",
            "audit/exports",
            "settings",
            "endpoints",
            "endpoints/private",
            "endpoints/private/probes",
            "evaluations",
            "evaluations/private",
            "evaluations/private/blind",
            "evaluations/private/grades",
        )
        for actor in (self.operator, self.viewer):
            for method in ("GET", "POST", "PATCH"):
                for path in paths:
                    with self.subTest(actor=actor["role"], method=method, path=path):
                        self.assertEqual(
                            self.deny(method, path, actor=actor).status, 403
                        )
        for kind in ("roles", "skills", "templates"):
            self.assertEqual(self.deny("GET", kind, actor=self.viewer).status, 403)
        self.deny("POST", "workspaces", {"name": "escalation"})
        self.deny("PATCH", "workspaces/default", {"name": "escalation"})
        self.deny("GET", "workspaces/default/history")

    def test_collections_overview_search_filters_and_history_do_not_leak(self):
        own_requirement = self.call(
            "POST", "requirements", {"title": "授权需求", "project_id": self.own["id"]}
        )
        private = self.call(
            "POST",
            "requirements",
            {"title": "PRIVATE-REQUIREMENT", "project_id": self.other["id"]},
        )
        for actor in (self.operator, self.viewer):
            self.assertEqual(
                [p["id"] for p in self.call("GET", "projects", actor=actor)["items"]],
                [self.own["id"]],
            )
            self.assertEqual(
                [
                    r["id"]
                    for r in self.call("GET", "requirements", actor=actor)["items"]
                ],
                [own_requirement["id"]],
            )
            listed = self.call("GET", "runs", actor=actor)
            self.assertEqual(
                [run["run_id"] for run in listed["items"]], [self.own_run["run_id"]]
            )
            overview = self.call("GET", "overview", actor=actor)
            self.assertEqual(overview["projects"], 1)
            self.assertEqual(overview["runs"], 1)
            self.assertNotIn("PRIVATE-REQUIREMENT", json.dumps(overview))
            self.deny("GET", "requirements/" + private["id"] + "/history", actor=actor)
            self.deny("GET", "projects/" + self.other["id"], actor=actor)
            self.deny(
                "GET", "runs", actor=actor, query={"project_id": self.other["id"]}
            )
            self.deny(
                "GET", "runs", actor=actor, query={"requirement_id": private["id"]}
            )
            self.deny(
                "GET", "overview", actor=actor, query={"workspace_id": self.space["id"]}
            )
        viewer_workspace = self.call("GET", "workspace", actor=self.viewer)
        self.assertEqual(viewer_workspace["repositories"], [])
        self.assertEqual(viewer_workspace["model_options"], [])
        self.assertEqual(
            self.call("GET", "overview", actor=self.viewer)["approvals"], []
        )

    def test_idempotent_cached_writes_recheck_revoked_grants(self):
        cases = [
            (
                "requirements",
                {"title": "cached private requirement", "project_id": self.own["id"]},
            ),
            (
                "runs",
                {"project_id": self.own["id"], "task": {"text": "cached private run"}},
            ),
        ]
        for i, (path, body) in enumerate(cases):
            with self.subTest(path=path):
                # Each case begins with an explicitly regranted current account.
                self.revoke(project_ids=[self.own["id"]])
                key = "retry-" + str(i)
                first = self.call("POST", path, body, actor=self.operator, key=key)
                self.assertEqual(
                    first, self.call("POST", path, body, actor=self.operator, key=key)
                )
                self.revoke(project_ids=[])
                with self.assertRaises(Problem) as denied:
                    self.call("POST", path, body, actor=self.operator, key=key)
                self.assertEqual(denied.exception.status, 404)

    def test_cached_project_creation_cannot_reveal_revoked_project(self):
        body = {"name": "created once", "workspace_id": "default"}
        created = self.call(
            "POST", "projects", body, actor=self.operator, key="project-cache"
        )
        self.assertEqual(
            created,
            self.call(
                "POST", "projects", body, actor=self.operator, key="project-cache"
            ),
        )
        self.revoke(project_ids=[self.own["id"]])
        with self.assertRaises(Problem) as denied:
            self.call(
                "POST", "projects", body, actor=self.operator, key="project-cache"
            )
        self.assertEqual(denied.exception.status, 404)

    def test_public_templates_reject_forged_server_owned_evidence(self):
        owned_fields = {
            "inherited_from": {
                "run_id": self.other_run["run_id"],
                "attempt_id": "private-attempt",
            },
            "skill_snapshots": [{"id": "private-skill", "instructions": "forged"}],
            "role_name": "forged role",
            "role_revision": 9,
            "artifact": "private-hash",
            "generation": 9,
            "input_digest": "forged",
            "feedback": "forged rework",
        }
        for field, value in owned_fields.items():
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.call(
                        "POST",
                        "templates",
                        {
                            "name": "forged source",
                            "nodes": [
                                {"node_id": "inspect", "stage": "review", field: value}
                            ],
                        },
                        actor=self.operator,
                    )
        template = self.call(
            "POST",
            "templates",
            {
                "name": "clean template",
                "nodes": [{"node_id": "inspect", "stage": "review"}],
            },
            actor=self.operator,
        )
        with self.assertRaises(ValueError):
            self.call(
                "PATCH",
                "templates/" + template["id"],
                {
                    "nodes": [
                        {
                            "node_id": "inspect",
                            "inherited_from": owned_fields["inherited_from"],
                        }
                    ]
                },
                actor=self.operator,
                revision=1,
            )

    def test_cached_run_does_not_bypass_role_downgrade(self):
        body = {"project_id": self.own["id"], "task": {"text": "cached run"}}
        self.call("POST", "runs", body, actor=self.operator, key="role-cache")
        self.revoke(role="viewer")
        with self.assertRaises(Problem) as denied:
            self.call("POST", "runs", body, actor=self.operator, key="role-cache")
        self.assertEqual(denied.exception.status, 403)

    def test_revocation_during_preparation_prevents_commit(self):
        with self.app.store.transaction() as db:
            before = db.execute("SELECT count(*) FROM runs").fetchone()[0]
        original = self.app.delivery.prepare_context

        def prepare(plan):
            result = original(plan)
            self.revoke(project_ids=[])
            return result

        with patch.object(self.app.delivery, "prepare_context", side_effect=prepare):
            self.deny(
                "POST",
                "runs",
                {"project_id": self.own["id"], "task": {"text": "must not commit"}},
            )
        with self.app.store.transaction() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM runs").fetchone()[0], before
            )
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM api_requests WHERE response LIKE '%must not commit%'"
                ).fetchone()[0],
                0,
            )

    def model(self):
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
                output_micros_per_million=1_000_000,
            )
        )
        with self.app.store.transaction() as db:
            workspace = self.app.platform.get(db, "workspaces", "default")
            self.app.platform.save(
                db,
                "workspaces",
                {"endpoint_ids": ["local"]},
                "default",
                workspace["revision"],
            )

    def test_claimed_dispatch_rechecks_account_and_workspace_endpoint_access(self):
        self.model()
        for remove_endpoint in (False, True):
            with self.subTest(remove_endpoint=remove_endpoint):
                self.revoke(project_ids=[self.own["id"]])
                run = self.call(
                    "POST",
                    "runs",
                    {
                        "project_id": self.own["id"],
                        "task": {
                            "text": "verify dispatch grant",
                            "stage": "review",
                            "max_output_tokens": 20,
                        },
                        "budget": {"currency": "USD", "limit_micros": 10000},
                    },
                    actor=self.operator,
                )
                attempt = self.app.runs.claim()
                self.assertIsNotNone(attempt)
                if remove_endpoint:
                    with self.app.store.transaction() as db:
                        workspace = self.app.platform.get(db, "workspaces", "default")
                        self.app.platform.save(
                            db,
                            "workspaces",
                            {"endpoint_ids": []},
                            "default",
                            workspace["revision"],
                        )
                else:
                    self.revoke(project_ids=[])
                with patch.object(
                    self.app.catalog,
                    "generate",
                    side_effect=AssertionError("revoked dispatch reached provider"),
                ):
                    self.assertIsNone(self.app.runs.dispatch(attempt))
                    self.assertFalse(self.app.runs.work_once())
                current = self.app.runs.get(run["run_id"])
                self.assertEqual(current["budget"]["held_micros"], 0)
                self.assertEqual(current["nodes"][0]["state"], "needs_input")

    def test_requirement_cannot_move_history_to_another_project(self):
        requirement = self.call(
            "POST",
            "requirements",
            {"title": "Private revision", "project_id": self.other["id"]},
        )
        with self.assertRaises(Problem) as conflict:
            self.call(
                "PATCH",
                "requirements/" + requirement["id"],
                {"project_id": self.own["id"]},
                revision=1,
            )
        self.assertEqual(conflict.exception.status, 409)
        self.deny(
            "GET", "requirements/" + requirement["id"] + "/history", actor=self.viewer
        )

    def test_http_session_cannot_access_other_projects_or_admin_exports(self):
        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            server.app = self.app
            server.authorization = b"Bearer bootstrap-fixture"
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(thread.join, 2)
            self.addCleanup(server.shutdown)

            def request(path, data=None, token=None):
                connection = http.client.HTTPConnection(
                    "127.0.0.1", server.server_port, timeout=3
                )
                headers = {
                    "Content-Type": "application/json",
                    "Idempotency-Key": uuid4().hex,
                }
                if token:
                    headers["Authorization"] = "Bearer " + token
                connection.request(
                    "POST" if data is not None else "GET",
                    "/api/v1/" + path,
                    json.dumps(data) if data is not None else None,
                    headers,
                )
                response = connection.getresponse()
                value = json.loads(response.read())
                status = response.status
                connection.close()
                return status, value

            status, login = request(
                "auth/login",
                {"username": "operator", "password": "fixture-password-123"},
            )
            self.assertEqual(status, 200)
            token = login["token"]
            self.assertEqual(
                request("runs/" + self.own_run["run_id"], token=token)[0], 200
            )
            self.assertEqual(
                request("runs/" + self.other_run["run_id"] + "/context", token=token)[
                    0
                ],
                404,
            )
            self.assertEqual(request("audit/exports", token=token)[0], 403)
            self.revoke(project_ids=[])
            self.assertEqual(
                request("runs/" + self.own_run["run_id"], token=token)[0], 404
            )
            self.assertEqual(request("auth/me", token=token)[1]["project_ids"], [])


if __name__ == "__main__":
    unittest.main()
