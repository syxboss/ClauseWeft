import json
import hashlib
from unittest.mock import patch
from uuid import uuid4

from clauseweft import audit, requirements, reviews, testing
from clauseweft.api.application import Application
from clauseweft.api.delivery import export_record
from clauseweft.api.server import Problem
from test_delivery import RepositoryFixture, change, config
from platform_fixture import prepare_team


class DeliveryActionTest(RepositoryFixture):
    def setUp(self):
        super().setUp()
        cfg = self.root / "console.json"
        cfg.write_text(json.dumps({"repositories":{"r":{"path":str(self.repo),"allowlist":["."],
            "suite_id":"normal","suite_ids":["normal","extra"]}},"suites":{"normal":config(),"extra":config(seconds=5)}}))
        self.app = Application(self.root / "state", cfg)
        prepare_team(self.app)
        plan = {"mode":"workflow","repo_id":"r","base_sha":self.sha,"task":{"text":"需求","stage":"requirements"}}
        _, run = self.app.handle("POST",("runs",),{},plan,"create")
        self.rid = run["run_id"]
        self.artifacts = {}
        # Seed completed model branches. All Git and decision operations below use the application services.
        with self.app.store.transaction() as db:
            spec = requirements.revise(db,self.rid,None,[dict(id="CSV-01",text="目标",acceptance="验证")],
                                       "草稿","author",["review-product","review-test"])
            self.version = spec["version"]
            for reviewer in ("review-product","review-test"):
                reviews.submit(db,self.rid,self.version,reviewer,reviewer,reviewer,[])
            requirements.freeze(db,self.rid,self.version,"owner","批准")
            for key, value in (("draft",{"clauses":spec["clauses"]}),("review-product",{"findings":[]}),
                               ("review-test",{"findings":[]}),("approve-spec",{"spec_version_id":self.version}),
                               ("code",{"patch":change("print('base')","print('denied')")}),
                               ("test-design",{"patch":change("old","new","other.txt")})):
                sha = self.app.store.stage_blob(json.dumps(value))
                self.artifacts[key] = sha
                db.execute("UPDATE nodes SET state='succeeded',artifact=? WHERE run_id=? AND node_id=?",
                           (sha,self.rid,key))
                if key in ("draft","review-product","review-test"):
                    db.execute("INSERT INTO api_imports VALUES (?,?,?,?,NULL)",(self.rid,key,1,sha))
            self.app.runs._refresh(db,self.rid)
        self.action("integrations",{"selected":["test-design","code"],"reason":"采用两个补丁"})
        self.assertTrue(self.app.delivery.work_once())

    def action(self, name, data):
        revision = self.app.run(self.rid)["revision"]
        return self.app.handle("POST",("runs",self.rid,name),{},data,uuid4().hex,revision)[1]

    def test_order_rework_and_stale_suite(self):
        run = self.app.run(self.rid)
        candidate = self.app.delivery.gate_artifact(run,"integrate")
        self.assertEqual([w["node_id"] for w in candidate["work_packages"]],["test-design","code"])
        self.assertEqual(candidate["work_packages"][0]["paths"],["other.txt"])
        with self.assertRaises(ValueError):
            self.action("tests",{"candidate_sha":candidate["candidate_sha"],"suite_id":"unknown"})
        job = self.action("tests",{"candidate_sha":candidate["candidate_sha"],"suite_id":"extra"})
        with self.app.store.transaction() as db:
            ctx = self.app.context(db,self.rid)
            self.assertEqual(ctx["suite_id"],"extra")
            row = db.execute("SELECT input FROM api_jobs WHERE id=?",(job["id"],)).fetchone()
            self.assertEqual(json.loads(row[0])["suite"],testing.freeze_suite(self.version,config(seconds=5)))
        result = self.action("rework",{"node_id":"code","reason":"检查失败，请重新实现"})
        self.assertEqual((result["generation"],result["next_gate"]),(2,"integrate"))
        run = self.app.run(self.rid)
        by_id = {n["node_id"]:n for n in run["nodes"]}
        self.assertIsNone(run["candidate_sha"])
        self.assertEqual(by_id["test-design"]["artifact"],self.artifacts["test-design"])
        self.assertEqual(by_id["code"]["state"],"ready")
        self.assertIsNone(by_id["code"]["artifact"])
        self.assertTrue(self.app.delivery.work_once())  # stale queued test never starts Docker
        with self.app.store.transaction() as db:
            row = db.execute("SELECT state FROM api_jobs WHERE id=?",(job["id"],)).fetchone()
            self.assertEqual(row[0],"stale")
            self.assertIsNone(reviews.candidate(db,self.rid))
        with self.assertRaises(Problem):
            self.action("approvals",{"kind":"delivery","version":self.version,
                        "candidate_sha":candidate["candidate_sha"],"reason":"迟到的审批"})
        with self.assertRaises(Problem):
            self.action("exports",{})

    def test_delivery_jobs_preserve_complete_audit_results_and_actor(self):
        with self.app.store.transaction() as db:
            entry = next(item for item in audit.list_entries(db, {"run_id": self.rid})
                         if item["action"] == "delivery_job_finished")
            proof = entry["after"]
            self.assertEqual(entry["actor_id"], "system-worker")
            self.assertEqual(entry["details"]["requested_by"]["id"], "bootstrap-admin")
            self.assertEqual(proof["generation"], 1)
            self.assertEqual(proof["spec_version_id"], self.version)
            self.assertEqual(proof["context"]["base_sha"], self.sha)
            self.assertIn("print('denied')", proof["result"]["patch"])
            self.assertEqual(proof["result_sha256"], hashlib.sha256(
                json.dumps(proof["result"], sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()
            ).hexdigest())
            self.assertTrue(audit.verify(db)["valid"])

    def test_changed_job_result_cannot_keep_a_green_audit_verification(self):
        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])
            job = db.execute("SELECT id FROM api_jobs WHERE run_id=? AND state='succeeded'", (self.rid,)).fetchone()[0]
            db.execute("UPDATE api_jobs SET result=? WHERE id=?", (json.dumps({"status": "forged"}), job))
            proof = audit.verify(db)
            self.assertFalse(proof["valid"])
            self.assertIn("job_result_mismatch", [error["code"] for error in proof["errors"]])

    def test_failed_job_and_stale_job_keep_auditable_inputs(self):
        candidate = self.app.delivery.gate_artifact(self.app.run(self.rid), "integrate")
        job = self.action("tests", {"candidate_sha": candidate["candidate_sha"]})
        with patch.object(self.app.delivery, "workspace", side_effect=RuntimeError(
            "Authorization: Bearer synthetic-private-value"
        )):
            self.assertTrue(self.app.delivery.work_once())
        with self.app.store.transaction() as db:
            failed = next(item for item in audit.list_entries(db, {"run_id": self.rid})
                          if item["action"] == "delivery_job_failed")
            self.assertEqual(failed["outcome"], "failed")
            self.assertEqual(failed["after"]["input"]["candidate_sha"], candidate["candidate_sha"])
            self.assertEqual(failed["after"]["input"]["suite"]["spec_version_id"], self.version)
            self.assertNotIn("synthetic-private-value", json.dumps(failed))
            result = db.execute("SELECT result FROM api_jobs WHERE id=?", (job["id"],)).fetchone()[0]
            self.assertEqual(json.loads(result)["error_type"], "RuntimeError")
            self.assertNotIn("synthetic-private-value", result)
        stale_job = self.action("tests", {"candidate_sha": candidate["candidate_sha"]})
        self.action("rework", {"node_id": "code", "reason": "修改权限路径"})
        self.assertTrue(self.app.delivery.work_once())
        with self.app.store.transaction() as db:
            stale = next(item for item in audit.list_entries(db, {"run_id": self.rid})
                         if item["action"] == "delivery_job_stale")
            self.assertEqual(stale["after"]["id"], stale_job["id"])
            self.assertEqual(stale["outcome"], "stale")
            self.assertEqual(stale["after"]["result"]["status"], "stale")

    def test_requirement_revision_and_model_import_keep_version_bound_content(self):
        clauses = [{"id": "CSV-01", "text": "仅管理员导出", "acceptance": "普通成员被拒绝"}]
        actor = self.app.handle("POST", ("members",), {}, {"username": "operator-1", "display_name": "研发负责人", "role": "operator", "password": "operator-fixture-password", "workspace_ids": ["default"], "project_ids": ["default"]}, uuid4().hex)[1]
        revision = self.app.run(self.rid)["revision"]
        self.app.handle("POST", ("runs", self.rid, "specs"), {},
                        {"parent": self.version, "clauses": clauses, "reason": "细化角色来源"},
                        uuid4().hex, revision, actor)
        with self.app.store.transaction() as db:
            decision = next(item for item in audit.list_entries(db, {"run_id": self.rid})
                            if item["action"] == "delivery.specs")
            self.assertEqual(decision["actor_id"], actor["id"])
            self.assertEqual(decision["before"]["spec"]["version"], self.version)
            self.assertEqual(decision["after"]["spec"]["clauses"], clauses)
            current = self.app.run(self.rid)
            node = next(item for item in current["nodes"] if item["node_id"] == "review-product")
            reply = self.app.store.stage_blob(json.dumps({"findings": []}))
            attempt_id = uuid4().hex
            db.execute("""INSERT INTO attempts(attempt_id,run_id,node_id,generation,state,lease_until,started,endpoint_id,model_identity)
                VALUES(?,?,'review-product',?,'succeeded',0,0,'model-endpoint','independent-model')""",
                (attempt_id, self.rid, node["generation"]))
            db.execute("UPDATE nodes SET state='succeeded',artifact=?,attempt_id=? WHERE run_id=? AND node_id='review-product'",
                       (reply, attempt_id, self.rid))
            self.app.delivery.scan_results(db, self.app.run(self.rid))
            imported = next(item for item in audit.list_entries(db, {"run_id": self.rid})
                            if item["action"] == "delivery.artifact_checked" and item["details"]["node_id"] == "review-product")
            self.assertEqual(imported["actor_id"], "model:independent-model")
            self.assertEqual(imported["outcome"], "succeeded")
            self.assertEqual(imported["after"]["target"], decision["after"]["spec"]["version"])
            self.assertEqual(imported["details"]["artifact"], reply)
            self.assertEqual(imported["details"]["generation"], node["generation"])
            self.assertEqual(imported["details"]["parsed"], {"findings": []})
            self.assertTrue(audit.verify(db)["valid"])

    def test_restart_records_interrupted_job_without_inventing_a_test_verdict(self):
        candidate = self.app.delivery.gate_artifact(self.app.run(self.rid), "integrate")
        job = self.action("tests", {"candidate_sha": candidate["candidate_sha"]})
        with self.app.store.transaction() as db:
            db.execute("UPDATE api_jobs SET state='running' WHERE id=?", (job["id"],))
        reopened = Application(self.root / "state", self.root / "console.json")
        with reopened.store.transaction() as db:
            saved = db.execute("SELECT state,result FROM api_jobs WHERE id=?", (job["id"],)).fetchone()
            self.assertEqual(saved["state"], "interrupted")
            self.assertEqual(json.loads(saved["result"])["status"], "interrupted")
            entry = next(item for item in audit.list_entries(db, {"run_id": self.rid})
                         if item["action"] == "delivery_job_interrupted")
            self.assertEqual(entry["outcome"], "uncertain")
            self.assertEqual(entry["after"]["input"]["candidate_sha"], candidate["candidate_sha"])
            self.assertEqual(entry["after"]["generation"], 1)
            self.assertTrue(audit.verify(db)["valid"])

    def test_restart_with_changed_environment_revokes_gate(self):
        # A fixture checkpoint is sufficient to exercise startup invalidation.
        frozen = testing.freeze_suite(self.version,config())
        self.app.runs.gate(self.rid,"execute-tests",{k:frozen[k] for k in ("suite_digest","env_digest","spec_version_id")})
        cfg = self.root / "console.json"
        settings = json.loads(cfg.read_text())
        settings["suites"]["normal"]["timeout_seconds"] += 1
        cfg.write_text(json.dumps(settings))
        reopened = Application(self.root / "state",cfg)
        run = reopened.run(self.rid)
        by_id = {n["node_id"]:n for n in run["nodes"]}
        self.assertEqual(by_id["execute-tests"]["state"],"needs_input")
        self.assertEqual(by_id["execute-tests"]["generation"],2)
        self.assertEqual(by_id["integrate"]["generation"],1)
        self.assertTrue(run["candidate_sha"])

    def test_upgrade_pre_platform_completed_delivery_restores_project_links_first(self):
        # Restore an old accepted checkpoint without depending on a Docker run.
        # Its report and decision are immutable fixtures for startup migration.
        run = self.app.run(self.rid)
        candidate = self.app.delivery.gate_artifact(run, "integrate")
        frozen = testing.freeze_suite(self.version, config())
        report = dict(status="passed", candidate_sha=candidate["candidate_sha"], execution_id="old-test",
                      cases=[dict(id="regular", status="passed", exit_code=0)],
                      **{key: frozen[key] for key in ("suite_digest", "env_digest", "spec_version_id")})
        self.app.runs.gate(self.rid, "execute-tests", report)
        with self.app.store.transaction() as db:
            target = reviews.candidate(db, self.rid)
            reviews.submit(db, self.rid, target["target"], "code-review", "reviewer", "independent-reviewer", [])
            review_artifact = self.app.store.stage_blob(json.dumps({"findings": []}))
            db.execute("UPDATE nodes SET state='succeeded',artifact=? WHERE run_id=? AND node_id='code-review'",
                       (review_artifact, self.rid))
            self.app.runs._refresh(db, self.rid)
            decision = reviews.accept(db, self.rid, self.version, candidate["candidate_sha"], "old-owner",
                                      "历史交付已确认", report=report, manifest={"candidate": candidate})
            self.app.runs.gate(self.rid, "accept", decision)
            for table in ("platform_sessions", "platform_login_failures", "platform_audit", "platform_run_links",
                          "platform_releases", "platform_resources", "platform_members", "platform_initialization"):
                db.execute("DROP TABLE " + table)
            # A pre-platform installation also predates the content revision ledger.
            # Keeping new audit proofs while dropping their resources is corruption,
            # rather than the legacy upgrade this regression exercises.
            for table in ("audit_resource_revisions", "audit_entries", "audit_head", "audit_migrations", "deployment_snapshot"):
                db.execute("DROP TABLE " + table)
        reopened = Application(self.root / "state", self.root / "console.json")
        restored = reopened.run(self.rid)
        self.assertEqual(restored["status"], "completed")
        self.assertEqual(restored["project_id"], "default")
        self.assertEqual(restored["candidate_sha"], candidate["candidate_sha"])
        self.assertEqual(reopened.delivery.gate_artifact(restored, "execute-tests"), report)
        with reopened.store.transaction() as db:
            self.assertEqual(reviews.delivery(db, self.rid)["decision_id"], decision["decision_id"])

    def test_changed_repository_scope_blocks_existing_run(self):
        cfg = self.root / "console.json"
        settings = json.loads(cfg.read_text())
        settings["repositories"]["r"]["allowlist"] = ["other.txt"]
        cfg.write_text(json.dumps(settings))
        self.app = Application(self.root / "state",cfg)
        with self.assertRaises(Problem) as caught:
            self.action("tests",{"candidate_sha":self.app.run(self.rid)["candidate_sha"]})
        self.assertIn("文件范围",str(caught.exception))

    def test_export_redacts_values_and_preserves_patch(self):
        data = {"reason":"Authorization: Bearer synthetic-sensitive-fixture",
                "password":"synthetic-sensitive-fixture","log":"api_key=synthetic-sensitive-fixture",
                "patch":change("old","new","other.txt")}
        safe = export_record(data)
        self.assertNotIn("synthetic-sensitive-fixture",json.dumps(safe))
        self.assertEqual(safe["patch"],data["patch"])
        with self.assertRaises(RuntimeError):
            export_record({"patch":change("old","Authorization: Bearer synthetic-sensitive-fixture","other.txt")})
