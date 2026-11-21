"""Durable fan-out, isolation and fan-in of a planner's model-generated briefs."""

from concurrent.futures import ThreadPoolExecutor
import json
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from clauseweft import audit, workflow
from clauseweft.catalog import Catalog
from clauseweft.runs import Runs
from clauseweft.storage import Store


class ComplexWorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = Store(temp.name)
        self.catalog = Catalog(self.store, {"local": {"url": "http://localhost:8765"}})
        self.catalog.save(dict(
            endpoint_id="worker", model_identity="local-model", gateway_id="local",
            gateway_etag='"revision"', enabled=True, allowed=True, location="local",
            context_tokens=65536, capabilities=[], fit={}, priority=1,
            input_micros_per_million=0, output_micros_per_million=1000000,
        ))
        self.runs = Runs(self.store, self.catalog)
        self.body = dict(
            mode="complex", task=dict(text="交付研究报告", stage="general", max_output_tokens=100, data_scope="public"),
            budget={"currency": "USD", "limit_micros": 1000}, max_concurrency=2,
            skill_snapshots=[dict(id="evidence", revision=1, name="核实依据", instructions="列出依据。",
                                  data_scope="local_only")],
        )
        self.plan = '{"subtasks":["检查物流","核对库存"]}'
        self.usage = {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}

    def dispatch(self):
        attempt = self.runs.claim()
        self.assertIsNotNone(attempt)
        permit = self.runs.dispatch(attempt)
        self.assertIsNotNone(permit)
        return attempt, permit[1]

    def test_truncated_parseable_plan_cannot_start_children(self):
        rid = self.runs.start(self.body)["run_id"]
        attempt, _ = self.dispatch()
        self.runs.finish(attempt, self.plan, self.usage, "partial-receipt", "length", 100)
        run = self.runs.get(rid)
        self.assertEqual([n["node_id"] for n in run["nodes"]], ["plan"])
        self.assertEqual(run["nodes"][0]["reason"], "output_length_limit")
        self.assertEqual(run["budget"]["settled_micros"], 2)
        self.assertFalse(self.runs.work_once())

    def test_preview_and_start_validate_same_concurrency_range(self):
        for limit in (0, 257, True):
            for operation in (self.runs.preview, self.runs.start):
                with self.subTest(limit=limit, operation=operation.__name__), self.assertRaisesRegex(ValueError, "并发数"):
                    operation(dict(self.body, max_concurrency=limit))
        for limit in (1, 256):
            request = dict(self.body, max_concurrency=limit)
            self.assertEqual(self.runs.preview(request)["status"], "selected")
            self.assertEqual(self.runs.start(request)["status"], "queued")

    def test_engine_complex_mode_requires_budget_and_defaults_to_general(self):
        request = {"mode": "complex", "task": {"text": "整理调查结果"}}
        for budget_fields in ({}, {"budget": {}}, {"budget": {"limit_micros": 100}}):
            for operation in (self.runs.preview, self.runs.start):
                with self.subTest(fields=budget_fields, operation=operation.__name__), self.assertRaisesRegex(ValueError, "总预算"):
                    operation(dict(request, **budget_fields))
        request["budget"] = self.body["budget"]
        self.assertEqual(self.runs.preview(request)["intent"], "general")
        result = self.runs.start(request)
        self.assertEqual(result["nodes"][0]["stage"], "general")
        attempt, _ = self.dispatch()
        self.finish(attempt, self.plan)
        self.assertEqual({n["stage"] for n in self.runs.get(result["run_id"])["nodes"]}, {"general"})

    def finish(self, attempt, text):
        self.runs.finish(attempt, text=text, receipt=attempt, usage=self.usage)

    def test_parallel_results_are_isolated_then_assembled_in_plan_order(self):
        rid = self.runs.start(self.body)["run_id"]
        plan, messages = self.dispatch()
        self.assertIn('"subtasks"', messages[0]["content"])
        self.finish(plan, self.plan)
        self.finish(plan, self.plan)
        reopened = Store(self.store.root)
        self.catalog = Catalog(reopened, self.catalog.gateways)
        self.runs = Runs(reopened, self.catalog)
        self.assertEqual([n["node_id"] for n in self.runs.get(rid)["nodes"]],
                         ["plan", "branch-1", "branch-2", "summary"])
        barrier = threading.Barrier(2)

        def generate(endpoint, messages, tokens):
            brief = messages[-1]["content"]
            other = "核对库存" if "检查物流" in brief else "检查物流"
            self.assertNotIn(other, str(messages))
            self.assertNotIn("subtasks", str(messages))
            self.assertIn("交付研究报告", str(messages))
            barrier.wait(timeout=5)
            return "已" + brief.splitlines()[-1], self.usage, brief

        with patch.object(self.catalog, "generate", side_effect=generate), ThreadPoolExecutor(2) as pool:
            calls = [pool.submit(self.runs.work_once) for _ in range(2)]
            self.assertEqual([call.result() for call in calls], [True, True])
        summary, messages = self.dispatch()
        self.assertEqual([msg["content"] for msg in messages[-2:]], [
            "branch-1\n分工：检查物流\n结果：已检查物流", "branch-2\n分工：核对库存\n结果：已核对库存",
        ])
        self.finish(summary, "研究报告")
        result = self.runs.get(rid)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["budget"]["settled_micros"], 8)
        with self.store.transaction() as db:
            for row in db.execute("SELECT context_ref FROM attempts WHERE run_id=?", (rid,)):
                context = json.loads(self.store.read_blob(row[0]))
                self.assertEqual(context["skills"], self.body["skill_snapshots"])
                self.assertEqual(context["data_scope"], "local_only")
                self.assertIn("列出依据。", str(context["messages"]))
        with self.assertRaisesRegex(RuntimeError, "重新规划"):
            self.runs.invalidate(rid, "plan")

    def test_invalid_plan_keeps_paid_response_then_resumes_without_partial_graph(self):
        rid = self.runs.start(self.body)["run_id"]
        attempt, _ = self.dispatch()
        malformed = '{"subtasks":["有效"," "]}'
        self.finish(attempt, malformed)
        result = self.runs.get(rid)
        self.assertEqual((result["status"], result["budget"]["settled_micros"]), ("needs_input", 2))
        self.assertEqual(len(result["nodes"]), 1)
        self.assertIsNone(result["nodes"][0]["artifact"])
        self.assertEqual(result["nodes"][0]["reason"], "invalid_complex_plan")
        with self.store.transaction() as db:
            artifact = db.execute("SELECT artifact FROM attempts WHERE attempt_id=?", (attempt,)).fetchone()[0]
            self.assertTrue(audit.verify(db)["valid"])
        events = self.store.events_after(rid)
        finished = next(e["data"] for e in events if e["kind"] == "attempt_finished")
        self.assertEqual(finished["response_sha256"], artifact)
        self.assertFalse(finished["published"])
        self.assertEqual(self.store.read_blob(artifact).decode(), malformed)
        self.runs.resume(rid)
        retry, _ = self.dispatch()
        self.finish(retry, self.plan)
        self.assertEqual(len(self.runs.get(rid)["nodes"]), 4)

    def test_planner_receipt_after_supersession_or_cancel_only_updates_history(self):
        rid = self.runs.start(self.body)["run_id"]
        old, _ = self.dispatch()
        self.runs.invalidate(rid, "plan")
        self.finish(old, self.plan)
        self.assertEqual(len(self.runs.get(rid)["nodes"]), 1)
        new, _ = self.dispatch()
        self.runs.cancel(rid)
        self.finish(new, self.plan)
        run = self.runs.get(rid)
        self.assertEqual(len(run["nodes"]), 1)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual(run["budget"]["settled_micros"], 4)

    def test_expired_plan_cannot_expand_after_reconcile_and_retry(self):
        rid = self.runs.start(self.body)["run_id"]
        old, _ = self.dispatch()
        with self.store.transaction() as db:
            db.execute("UPDATE attempts SET lease_until=0 WHERE attempt_id=?", (old,))
        self.assertIsNone(self.runs.claim())
        self.runs.resume(rid)
        self.assertIsNone(self.runs.claim())
        self.runs.reconcile(rid, old, 5, "owner", "本地出账记录")
        self.runs.resume(rid)
        new, _ = self.dispatch()
        self.finish(old, '{"subtasks":["旧计划 A","旧计划 B","旧计划 C"]}')
        self.assertEqual(len(self.runs.get(rid)["nodes"]), 1)
        self.finish(new, self.plan)
        self.finish(old, self.plan)
        run = self.runs.get(rid)
        self.assertEqual([n["brief"] for n in run["nodes"] if "brief" in n], ["检查物流", "核对库存"])
        self.assertEqual(run["budget"]["settled_micros"], 7)
        expanded = [e for e in self.store.events_after(rid) if e["kind"] == "complex_plan_expanded"]
        self.assertEqual([e["data"]["attempt_id"] for e in expanded], [new])

    def test_failed_branch_blocks_summary_and_retry_reuses_sibling(self):
        rid = self.runs.start(self.body)["run_id"]
        plan, _ = self.dispatch()
        self.finish(plan, self.plan)
        a, _ = self.dispatch()
        b, _ = self.dispatch()
        self.finish(a, "有效的物流结果")
        self.runs.finish(b, error="invalid_request")
        self.assertIsNone(self.runs.claim())
        self.assertEqual(self.runs.get(rid)["nodes"][-1]["state"], "waiting")
        self.runs.resume(rid)
        retry, msgs = self.dispatch()
        self.assertIn("核对库存", msgs[-1]["content"])
        self.finish(retry, "库存结果")
        _, msgs = self.dispatch()
        self.assertIn("有效的物流结果", msgs[-2]["content"])
        self.assertIn("库存结果", msgs[-1]["content"])

    def test_branch_verifies_parent_plan_artifact_before_dispatch(self):
        rid = self.runs.start(self.body)["run_id"]
        attempt, _ = self.dispatch()
        self.finish(attempt, self.plan)
        artifact = self.runs.get(rid)["nodes"][0]["artifact"]
        (self.store.objects / artifact).write_text("damaged", encoding="utf-8")
        branch = self.runs.claim()
        self.assertIsNone(self.runs.dispatch(branch))
        node = self.runs.get(rid)["nodes"][1]
        self.assertEqual((node["state"], node["reason"]), ("needs_input", "invalid_input_artifact"))

    def test_expansion_rejects_unbounded_or_nontext_plans(self):
        definition = workflow.compile(self.body)[0]
        for subtasks in ([], ["少"], ["多"] * 9, [1, "a"], ["a", "x" * 4001], ["a", chr(0xd800)]):
            with self.subTest(subtasks=str(subtasks)[:40]), self.assertRaises(ValueError):
                workflow.expand_complex(json.dumps({"subtasks": subtasks}), definition)
        self.assertEqual(len(workflow.expand_complex(json.dumps({"subtasks": ["x" * 4000] * 8}), definition)), 9)
