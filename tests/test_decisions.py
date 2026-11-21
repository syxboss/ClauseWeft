import json
from pathlib import Path
import sqlite3
import unittest

from clauseweft import evaluation, requirements, reviews


def clause(text):
    return [{"id": "CSV-01", "text": text, "acceptance": ["管理员成功", "普通用户被拒绝"]}]


def finding(severity):
    return dict(location="CSV-01", severity=severity, example="普通用户也可导出",
                suggestion="核对服务端角色", evidence="权限用例 02")


class DecisionsTest(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.execute("PRAGMA foreign_keys=ON")
        requirements.init_schema(self.db)
        self.rid = "run-A"
        self.v1 = self.revise(None, "管理员可导出")

    def revise(self, parent, text):
        return requirements.revise(self.db, self.rid, parent, clause(text), "确认权限", "负责人")["version"]

    def submit(self, target, branch, findings, identity=None):
        return reviews.submit(self.db, self.rid, target, branch, "author", identity or branch, findings)

    def freeze(self, version):
        return requirements.freeze(self.db, self.rid, version, "owner", "评审已完成")

    def test_revise_preserves_disagreements_and_requires_reviews_on_new_version(self):
        review = self.submit(self.v1, "product", [finding("blocker")])
        self.assertEqual(review, self.submit(self.v1, "product", [finding("blocker")]))
        with self.assertRaises(reviews.Conflict):
            self.submit(self.v1, "product", [])
        with self.assertRaises(reviews.Conflict):
            self.submit(self.v1, "engineering", [], identity="product")
        self.submit(self.v1, "engineering", [dict(finding("info"), example="普通用户无法导出")])
        with self.assertRaises(reviews.Conflict):
            self.freeze(self.v1)
        with self.assertRaises(ValueError):
            reviews.resolve(self.db, self.rid, review["findings"][0]["id"], "owner", "fix", "已核对")
        reviews.resolve(self.db, self.rid, review["findings"][0]["id"], "owner", "fix", "已核对", "权限用例通过")
        self.freeze(self.v1)
        v2 = self.revise(self.v1, "项目管理员可导出")
        self.assertFalse(requirements.current(self.db, self.rid)["approved"])
        self.assertEqual([d["id"] for d in requirements.compare(self.db, self.rid, self.v1, v2)], ["CSV-01"])
        for stale in (self.v1, v2):
            with self.assertRaises(reviews.Conflict):
                self.freeze(stale)
        self.submit(v2, "product", [])
        self.submit(v2, "engineering", [])
        self.assertTrue(self.freeze(v2)["approved"])
        findings = reviews.list_findings(self.db, self.rid)
        self.assertEqual(len(findings), 2)
        self.assertEqual(findings[0]["resolution"]["actor"], "owner")
        self.assertTrue(all(s["approved"] for s in requirements.list_specs(self.db, self.rid)))

    def test_delivery_binds_candidate_and_preserves_immutable_historical_manifest(self):
        self.submit(self.v1, "product", [])
        self.submit(self.v1, "engineering", [])
        self.freeze(self.v1)
        c = reviews.set_candidate(self.db, self.rid, self.v1, "c"*40)
        self.submit(c["target"], "code-review", [])
        report = dict(status="passed", spec_version_id=self.v1, candidate_sha="c"*40, execution_id="run-test",
                      suite_digest="suite", env_digest="env", cases=[dict(id="01", status="passed")])
        def accept(sha):
            return reviews.accept(self.db, self.rid, self.v1, sha, "owner", "核对",
                                  report=report, manifest={"patch_ref":"p"})
        signed = accept("c"*40)
        self.assertEqual(reviews.delivery(self.db, self.rid), signed)
        d = reviews.set_candidate(self.db, self.rid, self.v1, "d"*40)
        self.assertIsNone(reviews.delivery(self.db, self.rid))
        for sha in ("c"*40, "d"*40):
            with self.assertRaises(reviews.Conflict):
                accept(sha)
        self.submit(d["target"], "code-review", [])
        with self.assertRaises(reviews.Conflict):
            accept("d"*40)
        self.assertIn(signed["decision_id"], self.db.execute("SELECT id FROM mw_signed_decision WHERE kind='delivery'").fetchone())

    def test_finding_list_tracks_current_signed_and_superseded_targets(self):
        def statuses():
            return {item["target"]: item["target_status"] for item in reviews.finding_records(self.db, self.rid)}

        self.submit(self.v1, "product", [finding("minor")])
        self.submit(self.v1, "engineering", [])
        self.assertEqual(statuses(), {self.v1: "open"})
        self.freeze(self.v1)
        self.assertEqual(statuses(), {self.v1: "signed"})
        a = reviews.set_candidate(self.db, self.rid, self.v1, "a" * 40)["target"]
        note = self.submit(a, "code-review", [finding("minor")])["findings"][0]["id"]
        self.assertEqual(statuses()[a], "open")
        reviews.resolve(self.db, self.rid, note, "owner", "fix", "已核对")
        b = reviews.set_candidate(self.db, self.rid, self.v1, "b" * 40)["target"]
        self.submit(b, "code-review", [finding("info")])
        self.assertEqual(statuses(), {self.v1: "signed", a: "historical", b: "open"})
        report = dict(status="passed", spec_version_id=self.v1, candidate_sha="b" * 40,
                      execution_id="test", suite_digest="suite", env_digest="env",
                      cases=[dict(id="case", status="passed")])
        reviews.accept(self.db, self.rid, self.v1, "b" * 40, "owner", "检查实测", report=report, manifest={})
        self.assertEqual(statuses()[b], "signed")
        self.revise(self.v1, "已修订")
        self.assertEqual(set(statuses().values()), {"historical"})

    def test_merged_blocker_waits_for_original_and_cannot_form_a_cycle(self):
        blocker = self.submit(self.v1, "product", [finding("blocker")])["findings"][0]["id"]
        original = self.submit(self.v1, "engineering", [finding("major")])["findings"][0]["id"]
        reviews.resolve(self.db, self.rid, blocker, "owner", "duplicate", "同一权限问题", "对照评审", [original])
        with self.assertRaises(reviews.Conflict):
            reviews.resolve(self.db, self.rid, original, "owner", "duplicate", "重复", "对照评审", [blocker])
        with self.assertRaises(reviews.Conflict):
            self.freeze(self.v1)
        reviews.resolve(self.db, self.rid, original, "owner", "fix", "确认权限来源", "重新执行权限用例")
        self.freeze(self.v1)

    def test_foreign_run_references_and_stale_parent_rejected(self):
        with self.assertRaises(reviews.Conflict):
            self.revise(None, "不同父版本")
        with self.assertRaises(reviews.Conflict):
            reviews.submit(self.db, "run-B", self.v1, "product", "a", "b", [])
        with self.assertRaises(ValueError):
            requirements.revise(self.db, self.rid, self.v1, clause("一")*2, "重复", "负责人")

    def test_approval_and_baseline_roll_back_together(self):
        self.submit(self.v1, "product", [])
        self.submit(self.v1, "engineering", [])
        self.db.commit()
        with self.assertRaises(RuntimeError):
            with self.db:
                self.freeze(self.v1)
                raise RuntimeError("模拟写入审计事件失败")
        self.assertFalse(requirements.current(self.db, self.rid)["approved"])
        self.assertIsNone(requirements.list_specs(self.db, self.rid)[0]["approval"])


class EvaluationTest(unittest.TestCase):
    def setUp(self):
        path = Path(__file__).resolve().parents[1] / "examples" / "route-demo.json"
        self.snapshot = json.loads(path.read_text(encoding="utf-8"))
        self.dataset = dict(version="v1",items=[
            dict(id=k, snapshot=self.snapshot, allowed_endpoints=["local-review"], acceptance="通过权限用例")
            for k in ("q1", "q2", "q3")])

    def test_replay_keeps_missing_and_detects_forbidden_route(self):
        self.dataset["items"][2].pop("snapshot")
        stats = evaluation.replay(self.dataset)
        self.assertEqual((stats["missing"],stats["violations"],stats["total"]), (1,0,3))
        stats = evaluation.replay(self.dataset, lambda snapshot: {"selected_endpoint":"cloud-general"})
        self.assertEqual(stats["violations"], 2)
        self.assertNotIn("cost_per_success_micros", stats)

    def test_paired_run_blinding_costs_and_coverage(self):
        def start(item, pin):
            if pin and item["id"] == "q3":
                raise ValueError("预算不足")
            return item["id"] + ("-F" if pin else "-A")
        exp = evaluation.paired_run(self.dataset, "local-review", start)
        self.assertEqual(exp["dataset"], self.dataset)
        observations = {}
        for k, cost in (("q1-F", 7), ("q2-F", 3), ("q1-A", 8), ("q2-A", 5)):
            observations[k] = dict(executed=True, state="succeeded", elapsed_ms=50,
                                   attempts=[dict(settled_micros=cost)], artifact="匿名工件")
        observations["q2-F"]["state"] = "timed_out"
        observations["q2-F"]["attempts"] += [dict(settled_micros=2), dict(settled_micros=6)]
        observations["q2-A"]["attempts"].append(dict(settled_micros=None))
        blind = evaluation.blind_items(exp, observations)
        self.assertEqual(len(blind), 3)
        self.assertEqual(set(blind[0]), {"blind_id","item_id","acceptance","artifact"})
        grades = {r["blind_id"]:True for r in blind}
        stats = evaluation.summarize(exp, observations, grades)
        fixed = stats["fixed"]
        self.assertEqual((fixed["passed"], fixed["failed"], fixed["unexecuted"]), (1,1,1))
        self.assertEqual((fixed["retries"], fixed["cost_per_success_micros"], fixed["timeouts"]), (2,18,1))
        self.assertEqual((stats["paired_count"],stats["automatic_wins"]), (2,1))
        self.assertIsNone(stats["automatic"]["cost_per_success_micros"])
        self.assertEqual(stats["automatic"]["known_cost_micros"], 13)

    def test_zero_denominators_and_unavailable_fixed_model(self):
        dataset = dict(version="empty", items=[])
        exp = evaluation.paired_run(dataset, "cloud", lambda *_: self.fail())
        stats = evaluation.summarize(exp, {}, {})
        self.assertIsNone(stats["paired_coverage"])
        self.assertIsNone(stats["fixed"]["cost_per_success_micros"])
        calls = []
        def start(item, pin):
            calls.append(pin)
            return item["id"]
        exp = evaluation.paired_run(self.dataset, "cloud-general", start)
        self.assertEqual(calls,[None]*3)
        self.assertTrue(all(not i["arms"]["fixed"]["run_id"] for i in exp["items"]))
