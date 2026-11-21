"""Clean install, workspace ownership and explicit account access grants."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from clauseweft import audit
from clauseweft.api.application import Application
from clauseweft.api.platform import BOOTSTRAP
from clauseweft.api.server import Problem
from clauseweft.catalog import Catalog
from clauseweft.runs import Runs
from clauseweft.storage import Store, canonical


class WorkspacePermissionsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.app = Application(self.root / "state")

    def member(self, db, role="viewer", **grants):
        return self.app.platform.add_member(
            db,
            {
                "username": role + "-user",
                "password": "fixture-password-123",
                "role": role,
                **grants,
            },
        )

    def test_clean_install_has_one_isolated_example_and_empty_team(self):
        with self.app.store.transaction() as db:
            spaces = self.app.platform.visible_workspaces(db, BOOTSTRAP)
            self.assertEqual({space["id"] for space in spaces}, {"default", "example"})
            actual = self.app.platform.overview(db, {})
            self.assertEqual(
                (actual["projects"], actual["runs"], actual["requirements"]), (0, 0, 0)
            )
            example = self.app.platform.overview(db, {"workspace_id": "example"})
            self.assertEqual(
                (example["projects"], example["runs"], example["requirements"]),
                (1, 1, 0),
            )
            self.assertEqual(example["project_list"][0]["id"], "example-project")
            run = example["recent_runs"][0]
            self.assertTrue(run["is_example"])
            self.assertEqual(run["nodes"][0]["kind"], "model")
            self.assertEqual(run["nodes"][0]["reason"], "model_configuration_required")
            self.assertEqual(run["mode"], "single")
            self.assertEqual(example["pending_approvals"], 0)
            self.assertEqual(self.app.platform.list(db, "templates", {"workspace_id": "example"}), [])
            self.assertEqual(run["budget"]["settled_micros"], 0)
            self.assertEqual(
                db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0
            )
            self.assertTrue(audit.verify(db)["valid"])

    def test_reopen_does_not_reseed_or_overwrite_example(self):
        with self.app.store.transaction() as db:
            self.app.platform.save(
                db, "projects", {"name": "调整后的示例"}, "example-project", 1
            )
            old_run = db.execute("SELECT run_id FROM runs").fetchone()[0]
        self.app = Application(self.root / "state")
        with self.app.store.transaction() as db:
            self.assertEqual(
                self.app.platform.get(db, "projects", "example-project")["name"],
                "调整后的示例",
            )
            self.assertEqual(
                [row[0] for row in db.execute("SELECT run_id FROM runs")], [old_run]
            )

    def test_unassigned_members_have_no_implicit_data_access(self):
        with self.app.store.transaction() as db:
            member = self.member(db)
            self.assertEqual(self.app.platform.visible_workspaces(db, member), [])
            self.assertEqual(self.app.platform.allowed_project_ids(db, member), set())
            for workspace in ("default", "example"):
                with self.assertRaises(Problem) as denied:
                    self.app.platform.authorize_workspace(db, workspace, member)
                self.assertEqual(denied.exception.status, 404)
            self.assertEqual(
                self.app.platform.public_actor(db, member)["capabilities"], ["read"]
            )

    def test_grants_filter_workspace_project_requirement_and_overview(self):
        with self.app.store.transaction() as db:
            team = self.app.platform.save(db, "projects", {"name": "真实项目"})
            member = self.member(
                db, workspace_ids=["example"], project_ids=["example-project"]
            )
            example_requirement = self.app.platform.save(db, "requirements", {"title": "测试创建的需求", "project_id": "example-project", "workspace_id": "example"})
            self.assertEqual(
                [
                    space["id"]
                    for space in self.app.platform.visible_workspaces(db, member)
                ],
                ["example"],
            )
            self.assertEqual(
                self.app.platform.allowed_project_ids(db, member), {"example-project"}
            )
            with self.assertRaises(Problem):
                self.app.platform.authorize_project(db, team["id"], member)
            with self.assertRaises(Problem):
                self.app.platform.authorize_resource(db, "projects", team["id"], member)
            requirements = self.app.platform.list(
                db, "requirements", {"workspace_id": "example"}, member
            )
            self.assertEqual(
                [item["id"] for item in requirements], [example_requirement["id"]]
            )
            overview = self.app.platform.overview(
                db, {"workspace_id": "example"}, member
            )
            self.assertEqual(overview["runs"], 1)
            self.assertEqual(overview["members"], 0)
            self.assertEqual(overview["endpoints"], 0)
            self.assertEqual(overview["costs"], [])
            self.assertEqual(overview["activity"], [])

    def test_grants_revalidate_current_account_role_and_revocation(self):
        with self.app.store.transaction() as db:
            member = self.member(
                db, workspace_ids=["example"], project_ids=["example-project"]
            )
            stale = self.app.platform.public_actor(db, member)
            changed = self.app.platform.update_member(
                db,
                member["id"],
                {"workspace_ids": [], "project_ids": [], "role": "operator"},
                1,
            )
            current = self.app.platform.public_actor(db, stale)
            self.assertEqual(current["role"], "operator")
            self.assertEqual(current["project_ids"], [])
            self.assertIn("write", current["capabilities"])
            with self.assertRaises(Problem):
                self.app.platform.authorize_project(db, "example-project", stale)
            self.app.platform.update_member(
                db, member["id"], {"active": False}, changed["revision"]
            )
            with self.assertRaises(Problem) as revoked:
                self.app.platform.public_actor(db, stale)
            self.assertEqual(revoked.exception.status, 401)

    def test_project_grant_requires_its_workspace_and_does_not_include_future_projects(
        self,
    ):
        with self.app.store.transaction() as db:
            with self.assertRaises(ValueError):
                self.member(db, project_ids=["example-project"])
            member = self.member(
                db, workspace_ids=["example"], project_ids=["example-project"]
            )
            another = self.app.platform.save(
                db, "projects", {"name": "未授权项目", "workspace_id": "example"}
            )
            self.assertNotIn(
                another["id"], self.app.platform.allowed_project_ids(db, member)
            )
            with self.assertRaises(ValueError):
                self.app.platform.update_member(
                    db, member["id"], {"workspace_ids": []}, 1
                )

    def test_operator_created_project_grant_is_explicit_and_auditable(self):
        with self.app.store.transaction() as db:
            member = self.member(db, role="operator", workspace_ids=["default"])
            project = self.app.platform.save(db, "projects", {"name": "操作者新建项目"})
            self.app.platform.grant_created_project(db, member, project)
            current = self.app.platform.public_actor(db, member)
            self.assertEqual(current["project_ids"], [project["id"]])
            self.assertEqual(
                self.app.platform.authorize_project(db, project["id"], current)["name"],
                project["name"],
            )
            rows = audit.history(db, "members", member["id"])
            self.assertEqual(rows[0]["payload"]["project_ids"], [project["id"]])
            self.assertTrue(audit.verify(db)["valid"])

    def test_cross_workspace_references_and_ownership_changes_are_rejected(self):
        with self.app.store.transaction() as db:
            skill = self.app.platform.save(
                db, "skills", {"name": "实际规范", "instructions": "检查来源"}
            )
            with self.assertRaises(ValueError):
                self.app.platform.save(
                    db,
                    "roles",
                    {
                        "name": "越界技能",
                        "workspace_id": "example",
                        "skill_ids": [skill["id"]],
                    },
                )
            role = self.app.platform.save(
                db, "roles", {"name": "实际角色", "skill_ids": [skill["id"]]}
            )
            with self.assertRaises(ValueError):
                self.app.platform.save(
                    db,
                    "templates",
                    {
                        "name": "越界角色",
                        "workspace_id": "example",
                        "nodes": [
                            {
                                "node_id": "review",
                                "kind": "model",
                                "stage": "review",
                                "role_id": role["id"],
                                "dependencies": [],
                            }
                        ],
                    },
                )
            with self.assertRaises(ValueError):
                self.app.platform.freeze_skills(db, [skill["id"]], "example")
            with self.assertRaises(Problem) as locked:
                self.app.platform.save(
                    db, "skills", {"workspace_id": "example"}, skill["id"], 1
                )
            self.assertEqual(locked.exception.code, "WORKSPACE_OWNERSHIP_LOCKED")
            with self.assertRaises(ValueError):
                self.app.platform.save(
                    db,
                    "requirements",
                    {"title": "越界需求", "project_id": "example-project"},
                )

    def test_legacy_unlinked_run_preserved_and_no_grants_inferred(self):
        state = self.root / "legacy"
        store = Store(state)
        run = Runs(store, Catalog(store, {})).start(
            {
                "mode": "sop",
                "task": {"text": "历史审批"},
                "template_snapshot": {
                    "nodes": [
                        {
                            "node_id": "gate",
                            "kind": "gate",
                            "stage": "review",
                            "dependencies": [],
                        }
                    ]
                },
            }
        )
        migrated = Application(state)
        with migrated.store.transaction() as db:
            saved = migrated.platform.enrich_run(db, migrated.runs.get(run["run_id"]))
            self.assertEqual(saved["project_id"], "default")
            self.assertEqual(saved["workspace_id"], "default")
            self.assertEqual(saved["nodes"][0]["node_id"], "gate")
            self.assertEqual(
                migrated.platform.get(db, "projects", "default")["name"], "历史项目"
            )
            self.assertEqual(
                migrated.platform.overview(db, {"workspace_id": "example"})["runs"], 1
            )
            self.assertTrue(audit.verify(db)["valid"])

    def test_standalone_run_adoption_after_initialization_is_audited(self):
        run = self.app.runs.start(
            {
                "mode": "sop",
                "task": {"text": "初始化之后的独立运行"},
                "template_snapshot": {
                    "nodes": [
                        {
                            "node_id": "gate",
                            "kind": "gate",
                            "stage": "review",
                            "dependencies": [],
                        }
                    ]
                },
            }
        )
        self.app = Application(self.root / "state")
        with self.app.store.transaction() as db:
            saved = self.app.platform.enrich_run(db, self.app.runs.get(run["run_id"]))
            self.assertEqual(saved["workspace_id"], "default")
            self.assertEqual(self.app.platform.overview(db, {})["runs"], 1)
            self.assertEqual(
                self.app.platform.overview(db, {"workspace_id": "example"})["runs"], 1
            )
            entries = audit.list_entries(
                db, {"action": "workspace.run_adopted", "run_id": run["run_id"]}
            )
            self.assertEqual(len(entries), 1)
            self.assertTrue(audit.verify(db)["valid"])
        self.app = Application(self.root / "state")
        with self.app.store.transaction() as db:
            self.assertEqual(
                len(
                    audit.list_entries(
                        db, {"action": "workspace.run_adopted", "run_id": run["run_id"]}
                    )
                ),
                1,
            )

    def test_workspace_repository_authorization_limits_binding(self):
        config = self.root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "repositories": {
                        "trusted": {"path": str(self.root), "allowlist": ["app.py"]}
                    }
                }
            )
        )
        app = Application(self.root / "repository-state", config)
        with app.store.transaction() as db:
            project = app.platform.save(
                db, "projects", {"name": "可信项目", "repository_id": "trusted"}
            )
            self.assertEqual(project["repository_id"], "trusted")
            with self.assertRaises(ValueError):
                app.platform.save(
                    db,
                    "projects",
                    {
                        "name": "越界仓库",
                        "workspace_id": "example",
                        "repository_id": "trusted",
                    },
                )
            with self.assertRaises(ValueError):
                app.platform.save(
                    db,
                    "workspaces",
                    {"name": "虚构仓库", "repository_ids": ["unknown"]},
                )
            self.assertEqual(
                canonical(
                    app.platform.get(db, "workspaces", "example")["repository_ids"]
                ),
                "[]",
            )


if __name__ == "__main__":
    unittest.main()
