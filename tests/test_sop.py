"""Published SOP snapshots run through the normal scheduler and human gates."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from clauseweft.catalog import Catalog
from clauseweft.runs import Runs
from clauseweft.storage import Store
from clauseweft.workflow import compile as compile_workflow, compile_sop


class PublishedSOPTests(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = Store(Path(temporary.name))
        catalog = Catalog(self.store, {"local": {"url": "http://127.0.0.1:8765"}})
        catalog.save(
            dict(
                endpoint_id="reviewer",
                model_identity="reviewer-v1",
                location="local",
                gateway_id="local",
                gateway_etag='"demo"',
                enabled=True,
                allowed=True,
                identity_verified=True,
                context_tokens=32000,
                capabilities=[],
                priority=1,
                fit={"review": 5},
                input_micros_per_million=0,
                output_micros_per_million=0,
            )
        )
        self.runs = Runs(self.store, catalog)

    def start(self, nodes):
        return self.runs.start(
            dict(
                mode="sop",
                task={"text": "评审企业研发规范"},
                project_id="platform",
                requirement_id="REQ-1",
                template_id="review",
                template_snapshot={"name": "独立评审", "revision": 3, "nodes": nodes},
            )
        )

    def states(self, rid):
        return {n["node_id"]: n for n in self.runs.get(rid)["nodes"]}

    def finish(self, attempt, content="评审报告"):
        self.runs.finish(
            attempt,
            text=content,
            receipt=attempt,
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    def test_unsorted_graph_is_frozen_and_rework_invalidates_all_descendants(self):
        nodes = [
            dict(node_id="accept", kind="gate", dependencies=["review"]),
            dict(node_id="review", prompt="检查验收条件"),
            dict(node_id="summary", dependencies=["accept"], prompt="归纳审批后的报告"),
        ]
        run = self.start(nodes)
        rid = run["run_id"]
        nodes[1]["prompt"] = "已修改的角色提示"
        first = self.runs.claim()
        permit = self.runs.dispatch(first)
        self.assertEqual(permit[1][0]["content"], "检查验收条件")
        self.finish(first)
        self.assertEqual(self.states(rid)["accept"]["state"], "needs_input")
        self.assertIsNone(self.runs.claim())
        self.runs.gate(rid, "accept", {"actor": "reviewer", "reason": "验收条款已确认"})
        second = self.runs.claim()
        messages = self.runs.dispatch(second)[1]
        self.assertIn("验收条款已确认", messages[-1]["content"])
        self.finish(second)
        self.assertEqual(self.runs.get(rid)["status"], "completed")
        self.runs.invalidate(rid, "review", "补充权限边界")
        states = self.states(rid)
        self.assertTrue(
            all(n["generation"] == 2 and n["artifact"] is None for n in states.values())
        )
        self.assertEqual(states["review"]["state"], "ready")
        self.assertEqual(states["summary"]["state"], "waiting")
        self.assertEqual(
            self.store.read_blob(self.store.stage_blob("评审报告")).decode(), "评审报告"
        )

    def test_gate_only_template_needs_no_model_and_root_gate_is_actionable(self):
        run = self.start([dict(node_id="approve", kind="gate")])
        rid = run["run_id"]
        self.assertEqual(run["status"], "needs_input")
        self.assertEqual(
            (run["project_id"], run["requirement_id"], run["template_revision"]),
            ("platform", "REQ-1", 3),
        )
        self.assertIsNone(self.runs.claim())
        self.runs.gate(rid, "approve", {"actor": "owner", "reason": "审批通过"})
        self.assertEqual(self.runs.get(rid)["status"], "completed")

    def test_pause_fences_claimed_work_and_survives_late_result(self):
        run = self.start(
            [dict(node_id="review"), dict(node_id="summary", dependencies=["review"])]
        )
        rid = run["run_id"]
        attempt = self.runs.claim()
        self.runs.pause(rid)
        self.assertIsNone(self.runs.dispatch(attempt))
        self.assertIsNone(self.runs.claim())
        self.runs.resume(rid)
        attempt = self.runs.claim()
        self.assertIsNotNone(self.runs.dispatch(attempt))
        self.runs.pause(rid)
        self.finish(attempt)
        self.assertEqual(self.runs.get(rid)["status"], "paused")
        self.assertEqual(self.states(rid)["review"]["state"], "succeeded")
        self.assertIsNone(self.runs.claim())
        self.runs.resume(rid)
        self.assertEqual(self.states(rid)["summary"]["state"], "ready")
        with self.store.transaction() as db:
            body = json.loads(
                db.execute("SELECT body FROM runs WHERE run_id=?", (rid,)).fetchone()[0]
            )
        self.assertEqual(body["template_snapshot"]["revision"], 3)

    def test_invalid_graphs_and_incompatible_independent_groups_are_rejected(self):
        invalid = [
            [],
            [{"node_id": "a", "dependencies": ["missing"]}],
            [
                {"node_id": "a", "dependencies": ["b"]},
                {"node_id": "b", "dependencies": ["a"]},
            ],
            [{"node_id": "a", "kind": "shell"}],
            [
                {"node_id": "a", "group": "independent"},
                {"node_id": "b", "group": "independent", "dependencies": ["a"]},
            ],
        ]
        for nodes in invalid:
            with self.subTest(nodes=nodes), self.assertRaises(ValueError):
                compile_sop(nodes)

    def test_delivery_template_keeps_required_gates_and_model_output_contracts(self):
        plan = {"mode": "workflow", "task": {"text": "实施变更"}}
        nodes = compile_workflow(plan)
        nodes[0]["prompt"] = "请使用项目术语撰写需求"
        configured = compile_workflow(dict(plan, template_snapshot={"nodes": nodes}))
        self.assertIn('"clauses"', configured[0]["prompt"])
        self.assertIn("请使用项目术语", configured[0]["prompt"])
        nodes[-1]["dependencies"] = ["integrate"]
        with self.assertRaises(ValueError):
            compile_workflow(dict(plan, template_snapshot={"nodes": nodes}))


if __name__ == "__main__":
    unittest.main()
