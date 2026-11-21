"""Audit evidence survives restarts and rejects or detects history modification."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest

from clauseweft import audit
from clauseweft.catalog import Catalog
from clauseweft.api.application import Application
from clauseweft.runs import Runs
from clauseweft.storage import SCHEMA, Store


ACTOR = {"id": "member-1", "display_name": "需求负责人"}


class AuditTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name)
        self.store = Store(self.path)

    def append(self, resource="projects/demo", **kwargs):
        with self.store.transaction() as db:
            return audit.record(db, ACTOR, "resource.changed", resource, **kwargs)

    def test_transaction_rollback_keeps_business_history_and_chain_together(self):
        with self.assertRaises(RuntimeError), self.store.transaction() as db:
            audit.save_revision(
                db, "roles", "reviewer", 1, {"prompt": "审核权限"}, ACTOR
            )
            raise RuntimeError("business transaction failed")
        with self.store.transaction() as db:
            self.assertEqual(audit.history(db, "roles", "reviewer"), [])
            self.assertEqual(audit.verify(db)["anchor"]["seq"], 0)
        self.append(after={"description": "已提交的修改"})
        reopened = Store(self.path)
        with reopened.transaction() as db:
            result = audit.verify(db)
            self.assertTrue(result["valid"])
            self.assertEqual(result["checked_entries"], 1)

    def test_concurrent_transactions_keep_one_ordered_global_chain(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            entries = list(
                pool.map(lambda index: self.append(details={"index": index}), range(24))
            )
        self.assertEqual(sorted(entry["seq"] for entry in entries), list(range(1, 25)))
        with self.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])
            self.assertEqual(audit.verify(db)["anchor"]["seq"], 24)

    def test_normal_sql_cannot_change_records_and_offline_proof_detects_changes(self):
        self.append(after={"status": "ready"})
        self.append(after={"status": "done"})
        with self.store.transaction() as db:
            for statement in (
                "UPDATE audit_entries SET action='changed' WHERE seq=1",
                "DELETE FROM audit_entries WHERE seq=2",
            ):
                with self.assertRaises(sqlite3.IntegrityError):
                    db.execute(statement)
            bundle = audit.export_bundle(db)
        bundle["entries"][0]["after"]["status"] = "forged"
        self.assertFalse(
            audit.verify_entries(bundle["entries"], bundle["anchor"])["valid"]
        )
        with self.store.transaction() as db:
            db.execute("DROP TRIGGER audit_entries_update_blocked")
            db.execute("UPDATE audit_entries SET action='changed' WHERE seq=1")
            self.assertFalse(audit.verify(db)["valid"])

    def test_tail_deletion_is_detected_against_the_separate_head(self):
        self.append()
        self.append()
        with self.store.transaction() as db:
            anchor = audit.verify(db)["anchor"]
            db.execute("DROP TRIGGER audit_entries_delete_blocked")
            db.execute("DELETE FROM audit_entries WHERE seq=2")
            self.assertFalse(audit.verify(db)["valid"])
            with self.assertRaises(RuntimeError):
                audit.record(db, ACTOR, "change", "projects/demo")
            self.assertFalse(
                audit.verify_entries(list(reversed(audit.list_entries(db))), anchor)[
                    "valid"
                ]
            )

    def test_export_filters_keep_a_complete_proof_and_credentials_are_redacted(self):
        self.append(
            project_id="a",
            details={"password": "private-password", "max_output_tokens": 80},
            before={"api_key": "before-key"},
            after={"prompt": "Authorization: Bearer sensitive-example"},
        )
        self.append(
            project_id="b", details={"url": "https://user:private-password@example.com"}
        )
        with self.store.transaction() as db:
            bundle = audit.export_bundle(db, {"project_id": "a"})
        rendered = json.dumps(bundle)
        for secret in ("private-password", "before-key", "sensitive-example"):
            self.assertNotIn(secret, rendered)
        self.assertEqual(bundle["selected_entry_ids"], [1])
        self.assertEqual(len(bundle["entries"]), 2)
        self.assertEqual(bundle["entries"][0]["details"]["max_output_tokens"], 80)
        self.assertTrue(bundle["entries"][0]["redacted"])
        self.assertTrue(bundle["verification"]["valid"])

    def test_resource_versions_and_worker_events_have_linked_evidence(self):
        catalog = Catalog(self.store, {"local": {"url": "http://127.0.0.1:8765"}})
        catalog.save(
            dict(
                endpoint_id="local",
                model_identity="local-model",
                location="local",
                enabled=True,
                allowed=True,
                context_tokens=4096,
                capabilities=[],
                fit={"review": 5},
                priority=1,
                gateway_id="local",
                gateway_etag='"version"',
                input_micros_per_million=0,
                output_micros_per_million=0,
            )
        )
        runs = Runs(self.store, catalog)
        self.store.local.audit_correlation_id = "request-1"
        run = runs.start(
            {
                "task": {"text": "评审需求"},
                "project_id": "demo",
                "created_by": "member-1",
            }
        )
        with self.store.transaction() as db:
            history = audit.history(db, "endpoints", "local")
            self.assertEqual(history[0]["revision"], 1)
            events = audit.list_entries(db, {"run_id": run["run_id"]})
            self.assertEqual(events[0]["actor_id"], "system-worker")
            self.assertEqual(events[0]["correlation_id"], "request-1")
            self.assertEqual(events[0]["details"]["initiated_by"], "member-1")
            self.assertTrue(audit.verify(db)["valid"])
            endpoint = json.loads(
                db.execute(
                    "SELECT data FROM endpoint_revisions WHERE endpoint_id='local'"
                ).fetchone()[0]
            )
            endpoint["priority"] = 99
            db.execute(
                "UPDATE endpoint_revisions SET data=? WHERE endpoint_id='local'",
                (json.dumps(endpoint),),
            )
            self.assertFalse(audit.verify(db)["valid"])

    def test_live_resource_changes_are_detected_against_audited_versions(self):
        app = Application(self.path / "platform")
        project = app.handle(
            "POST", ("projects",), {}, {"name": "交易服务"}, "project"
        )[1]
        member = app.handle(
            "POST",
            ("members",),
            {},
            {
                "username": "reviewer",
                "role": "viewer",
                "password": "example-password-123",
            },
            "member",
        )[1]
        template = app.handle("POST", ("templates",), {}, {"name": "测试发布快照", "nodes": [{"node_id": "review", "stage": "general", "prompt": "检查来源"}]}, "audit-template")[1]
        app.handle("POST", ("templates", template["id"], "publish"), {}, {}, "audit-publish", expected=template["revision"])
        with app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])
            db.execute("SAVEPOINT live_tamper")
            changed = dict(project)
            changed["name"] = "未经记录的修改"
            payload = json.loads(
                db.execute(
                    "SELECT payload FROM platform_resources WHERE kind='projects' AND id=?",
                    (project["id"],),
                ).fetchone()[0]
            )
            payload["name"] = changed["name"]
            db.execute(
                "UPDATE platform_resources SET payload=? WHERE kind='projects' AND id=?",
                (json.dumps(payload), project["id"]),
            )
            self.assertFalse(audit.verify(db)["valid"])
            db.execute("ROLLBACK TO live_tamper")
            db.execute(
                "UPDATE platform_members SET role='admin' WHERE id=?", (member["id"],)
            )
            self.assertFalse(audit.verify(db)["valid"])
            db.execute("ROLLBACK TO live_tamper")
            db.execute("RELEASE live_tamper")
            published = db.execute(
                "SELECT snapshot FROM platform_releases WHERE template_id=? AND revision=1", (template["id"],)
            ).fetchone()[0]
            release = json.loads(published)
            release["nodes"][0]["prompt"] = "未经发布的指令"
            db.execute("SAVEPOINT published_tamper")
            db.execute(
                "UPDATE platform_releases SET snapshot=? WHERE template_id=? AND revision=1",
                (json.dumps(release), template["id"]),
            )
            self.assertFalse(audit.verify(db)["valid"])
            db.execute("ROLLBACK TO published_tamper")
            db.execute("RELEASE published_tamper")
            self.assertTrue(audit.verify(db)["valid"])


class LegacyAuditTest(unittest.TestCase):
    def test_available_legacy_evidence_is_marked_and_imported_once(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)
            db = sqlite3.connect(path / "workspace.sqlite3")
            for statement in SCHEMA:
                db.execute(statement)
            db.execute("PRAGMA user_version=1")
            db.execute(
                "CREATE TABLE platform_resources(kind TEXT,id TEXT,revision INTEGER,payload TEXT,created REAL,updated REAL)"
            )
            db.execute(
                "INSERT INTO platform_resources VALUES('roles','reviewer',4,?,1,2)",
                (json.dumps({"name": "历史角色", "prompt": "审核权限"}),),
            )
            db.execute(
                "CREATE TABLE platform_audit(id INTEGER,at REAL,actor_id TEXT,actor TEXT,action TEXT,resource TEXT,details TEXT)"
            )
            db.execute(
                "INSERT INTO platform_audit VALUES(1,1,'known-member','旧版负责人','post.roles','roles','{}')"
            )
            db.execute(
                "INSERT INTO endpoint_revisions VALUES('endpoint',2,?)",
                (json.dumps({"api_key": "legacy-credential"}),),
            )
            db.commit()
            db.close()
            store = Store(path)
            with store.transaction() as db:
                entries = audit.list_entries(db)
                self.assertTrue(audit.verify(db)["valid"])
                self.assertTrue(
                    all(entry["outcome"] == "imported" for entry in entries)
                )
                history = audit.history(db, "roles", "reviewer")
                self.assertEqual([record["revision"] for record in history], [4])
                self.assertEqual(history[0]["source"], "legacy")
                self.assertIn("known-member", [entry["actor_id"] for entry in entries])
                self.assertIn(
                    "legacy-unknown", [entry["actor_id"] for entry in entries]
                )
                self.assertNotIn(
                    "legacy-credential", json.dumps(audit.export_bundle(db))
                )
                count = len(entries)
            store = Store(path)
            with store.transaction() as db:
                self.assertEqual(len(audit.list_entries(db)), count)


if __name__ == "__main__":
    unittest.main()
