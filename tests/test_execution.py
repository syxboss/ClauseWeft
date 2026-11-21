"""Crash, concurrency, isolation and accounting tests for the workspace scheduler."""

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from clauseweft.catalog import Catalog, GatewayFailure
from clauseweft.runs import Runs
from clauseweft.storage import Store
from clauseweft.workflow import allocate, validate


class SchedulerTests(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name)
        self.store = Store(self.path)
        self.cat = Catalog(self.store, {"main": {"url": "http://localhost:8765"}})
        self.runs = Runs(self.store, self.cat)
        self.a = self.model("A", "alpha")
        self.b = self.model("B", "beta")
        self.calls = []

    def model(self, key, identity, **fields):
        config = dict(
            endpoint_id=key,
            model_identity=identity,
            identity_verified=True,
            location="local",
            enabled=True,
            allowed=True,
            context_tokens=10000,
            capabilities=[],
            priority=1,
            fit={k: 5 for k in ("code", "test", "review", "requirements")},
            gateway_id="main",
            gateway_etag='"revision"',
            input_micros_per_million=0,
            output_micros_per_million=1_000_000,
        )
        return self.cat.save(dict(config, **fields))

    def start(self, mode="single", limit=1000, **kwargs):
        return self.runs.start(
            dict(
                mode=mode,
                task=dict(text="评审规格", max_output_tokens=70),
                budget={"limit_micros": limit},
                **kwargs,
            )
        )["run_id"]

    def finish(self, attempt, text="ok", usage=2):
        self.runs.finish(
            attempt,
            text=text,
            receipt="request-" + attempt,
            usage=None
            if usage is None
            else {"prompt_tokens": 10, "completion_tokens": usage, "total_tokens": 10 + usage},
        )

    def nodes(self, run):
        return {n["node_id"]: n for n in self.runs.get(run)["nodes"]}

    def reviews(self, limit=1000):
        rid = self.start("workflow", limit)
        draft = self.runs.claim()
        self.runs.dispatch(draft)
        self.finish(draft, "approved?", usage=0)
        return rid

    def test_distinct_parallel_workers_read_same_draft_and_wait_for_gate(self):
        rid = self.reviews()
        both_started = threading.Barrier(2)

        def complete(endpoint, messages, max_tokens):
            self.calls.append((endpoint["model_identity"], messages[-1]["content"]))
            both_started.wait(timeout=5)
            return endpoint["endpoint_id"], None, endpoint["endpoint_id"]

        with patch.object(self.cat, "generate", side_effect=complete), ThreadPoolExecutor(2) as pool:
            self.assertEqual(
                [f.result() for f in [pool.submit(self.runs.work_once) for _ in range(2)]], [True, True]
            )
        self.assertCountEqual(self.calls, [("alpha", "draft\napproved?"), ("beta", "draft\napproved?")])
        with self.store.transaction() as db:
            decisions = [json.loads(row[0]) for row in db.execute(
                "SELECT decision FROM attempts WHERE run_id=? AND node_id LIKE 'review-%'", (rid,)
            )]
        self.assertTrue(all(d["independent_group"] == "spec-review" for d in decisions))
        assigned_b = next(d for d in decisions if d["selected_endpoint"] == "B")
        self.assertEqual(assigned_b["ranked_first_endpoint"], "A")
        self.assertFalse(self.runs.work_once())
        self.assertEqual(self.nodes(rid)["approve-spec"]["state"], "needs_input")
        self.assertEqual(self.nodes(rid)["code"]["state"], "waiting")
        self.runs.gate(rid, "approve-spec", {"spec_version_id": "v1", "clauses": ["immutable"]})
        att = self.runs.claim()
        self.assertIn("immutable", self.runs.dispatch(att)[1][-1]["content"])

    def test_aliases_and_unverified_identity_cannot_fill_second_slot(self):
        self.cat.save(dict(self.b, model_identity="alpha"))
        self.model("C", "gamma", identity_verified=False)
        rid = self.reviews()
        with patch.object(self.cat, "generate", side_effect=AssertionError("no calls permitted")):
            while self.runs.work_once():
                pass
        self.assertEqual(self.nodes(rid)["review-test"]["reason"], "distinct_models_required")

    def test_only_one_atomic_hold_then_late_cancelled_receipt(self):
        rid = self.reviews(limit=100)
        claims = [self.runs.claim(), self.runs.claim()]
        with ThreadPoolExecutor(2) as pool:
            permits = list(pool.map(self.runs.dispatch, claims))
        self.assertEqual(sum(bool(p) for p in permits), 1)
        att = claims[next(i for i, p in enumerate(permits) if p)]
        self.assertEqual(self.runs.get(rid)["budget"]["held_micros"], 70)
        self.runs.cancel(rid)
        self.finish(att, usage=3)
        self.finish(att, usage=3)
        run = self.runs.get(rid)
        self.assertEqual(run["budget"]["held_micros"], 0)
        self.assertEqual(run["budget"]["settled_micros"], 3)
        self.assertEqual(run["status"], "cancelled")
        self.assertTrue(all(not n["artifact"] for n in run["nodes"] if n["node_id"] != "draft"))
        with self.assertRaises(RuntimeError):
            self.runs.resume(rid)

    def test_expiry_before_and_after_intent(self):
        rid = self.start()

        def expire(att):
            with self.store.transaction() as db:
                db.execute("UPDATE attempts SET lease_until=0 WHERE attempt_id=?", (att,))

        first = self.runs.claim()
        expire(first)
        second = self.runs.claim()
        self.assertIsNone(self.runs.dispatch(first))
        self.assertTrue(self.runs.dispatch(second))
        expire(second)
        reopened = Store(self.path)
        self.runs = Runs(reopened, Catalog(reopened, self.cat.gateways))
        self.assertIsNone(self.runs.claim())
        self.assertEqual(self.runs.resume(rid)["budget"]["held_micros"], 70)
        self.assertIsNone(self.runs.claim())
        self.runs.reconcile(rid, second, 9, "owner", "statement #4")
        self.runs.resume(rid)
        third = self.runs.claim()
        self.runs.dispatch(third)
        self.finish(second, text="stale", usage=7)
        self.finish(third, text="new", usage=1)
        self.assertEqual(self.store.read_blob(self.nodes(rid)["task"]["artifact"]), b"new")
        self.assertEqual(self.runs.get(rid)["budget"]["settled_micros"], 10)

    def test_revision_invalidates_old_results_and_keeps_history(self):
        rid = self.reviews()
        stale = self.runs.claim()
        self.runs.dispatch(stale)
        self.runs.replace_input(rid, "draft", {"clauses": ["new"]})
        self.finish(stale, usage=None)
        node = self.nodes(rid)["review-product"]
        self.assertIsNone(node["artifact"])
        self.assertEqual(node["generation"], 2)
        self.assertIn(stale, self.runs.get(rid)["budget"]["uncertain_attempts"])
        with self.store.transaction() as db:
            row = db.execute("SELECT artifact FROM attempts WHERE attempt_id=?", (stale,)).fetchone()
            self.assertEqual(self.store.read_blob(row[0]), b"ok")

    def test_retry_fallback_is_budgeted_and_pinned_failure_waits(self):
        rid = self.start()
        first = self.runs.claim()
        self.assertEqual(self.runs.dispatch(first)[0]["endpoint_id"], "A")
        self.runs.finish(first, error="upstream_rate_limited")
        self.assertIn(first, self.runs.get(rid)["budget"]["uncertain_attempts"])
        with self.store.transaction() as db:
            db.execute("UPDATE nodes SET ready_at=0 WHERE run_id=?", (rid,))
        second = self.runs.claim()
        self.assertEqual(self.runs.dispatch(second)[0]["endpoint_id"], "B")
        with self.store.transaction() as db:
            decision = json.loads(db.execute(
                "SELECT decision FROM attempts WHERE attempt_id=?", (second,)
            ).fetchone()[0])
        self.assertEqual(decision["ranked_first_endpoint"], "A")
        self.assertEqual(decision["rate_limited_endpoints"], ["A"])
        self.finish(second)
        self.assertEqual(self.runs.get(rid)["status"], "completed")
        rid = self.start(pinned_endpoints={"task": "A"})
        with patch.object(self.cat, "generate", side_effect=GatewayFailure("upstream_rate_limited")):
            self.runs.work_once()
        self.assertEqual(self.runs.get(rid)["status"], "needs_input")
        self.assertIsNone(self.runs.claim())

    def test_frozen_catalog_and_live_revocation_checked_after_claim(self):
        for frozen in [{}, {"A": 1}]:
            with self.subTest(frozen=frozen):
                rid = self.start(endpoint_revisions=frozen)
                att = self.runs.claim()
                self.cat.save(dict(self.a, priority=2))
                self.assertIsNone(self.runs.dispatch(att))
                self.assertIn("outside_frozen_catalog", self.nodes(rid)["task"]["reason"])
        rid = self.start()
        att = self.runs.claim()
        for endpoint in self.cat.list():
            self.cat.save(dict(endpoint, enabled=False))
        self.assertIsNone(self.runs.dispatch(att))
        self.assertEqual(self.runs.get(rid)["budget"]["held_micros"], 0)

    def test_allocation_considers_constrained_branch(self):
        a, b = ({"model_identity": k} for k in "AB")
        allocation = allocate({"flex": [a, b], "fixed": [a]}, set())
        self.assertEqual(allocation, {"flex": b, "fixed": a})
        with self.assertRaises(ValueError):
            validate([{"node_id": "a", "dependencies": ["a"]}])

    def test_transaction_rollback_and_blob_corruption(self):
        with self.assertRaises(RuntimeError), self.store.transaction():
            self.start()
            raise RuntimeError()
        self.assertEqual(self.runs.list(), [])
        sha = self.store.stage_blob(b"input")
        (self.store.objects / sha).write_bytes(b"changed")
        with self.assertRaises(RuntimeError):
            self.store.read_blob(sha)

    def test_credential_failure_disables_peers_in_same_gateway(self):
        self.start()
        att = self.runs.claim()
        self.runs.dispatch(att)
        self.assertTrue(self.runs.renew(att))
        self.runs.finish(att, error="unauthorized")
        self.assertFalse(self.runs.renew(att))
        self.assertTrue(all(not m["enabled"] for m in self.cat.list()))

    def test_preview_matches_first_dispatch_and_rechecks_scope(self):
        self.cat.save(dict(self.a, location="cloud"))
        self.cat.save(dict(self.b, allowed=False))
        body = {
            "mode": "workflow",
            "task": {"text": "写需求", "data_scope": "public", "input_tokens": 1},
            "budget": {"limit_micros": 1000},
        }
        policy = {"scope": "public"}
        self.runs = Runs(self.store, self.cat, scope_for=lambda b: policy["scope"])
        preview = self.runs.preview(body)
        self.assertEqual(preview["selected_endpoint"], "A")
        rid = self.runs.start(body)["run_id"]
        att = self.runs.claim()
        self.assertTrue(self.runs.dispatch(att))
        with self.store.transaction() as db:
            self.assertEqual(
                json.loads(
                    db.execute("SELECT decision FROM attempts WHERE attempt_id=?", (att,)).fetchone()[0]
                ),
                preview,
            )
        self.runs.cancel(rid)
        rid = self.runs.start(body)["run_id"]
        att = self.runs.claim()
        policy["scope"] = "local_only"
        self.assertIsNone(self.runs.dispatch(att))
        self.assertIn("data_scope", self.nodes(rid)["draft"]["reason"])

    def test_rework_preserves_feedback_in_dispatch(self):
        rid = self.reviews()
        self.runs.invalidate(rid, "review-product", feedback="检查空结果")
        att = self.runs.claim()
        permit = self.runs.dispatch(att)
        self.assertIn("检查空结果", str(permit[1]))

    def test_gateway_rebinding_and_revoked_workspace(self):
        new_cat = Catalog(self.store, {"main": {"url": "http://localhost:9000"}})
        self.assertTrue(all(not model["enabled"] for model in new_cat.list()))
        new_cat.save(dict(self.a, enabled=True))
        self.assertFalse(self.cat.get("A")["enabled"])
        self.assertTrue(new_cat.get("A")["enabled"])
        seen = []

        def current_policy(body):
            seen.append(body.get("run_id"))
            return "blocked" if "run_id" in body else "public"

        self.runs = Runs(self.store, new_cat, current_policy)
        rid = self.start()
        att = self.runs.claim()
        self.assertIsNone(self.runs.dispatch(att))
        self.assertIn("workspace_policy_changed", self.nodes(rid)["task"]["reason"])
        self.assertIn(rid, seen)

    def test_quality_fallback_only_within_fit_and_id_breaks_ties(self):
        self.cat.save(dict(self.a, quality_bp=7000, priority=1))
        self.cat.save(dict(self.b, quality_bp=9000, priority=9))
        self.model("C", "gamma", fit={"review": 1}, priority=0)
        body = {"task": {"text": "评审", "preference": "quality"}, "budget": {"limit_micros": 1000}}
        decision = self.runs.preview(body)
        self.assertEqual(decision["selected_endpoint"], "B")
        self.assertEqual(decision["rank_metric"], "quality_bp")
        self.cat.save(dict(self.a, quality_bp=9000, priority=100))
        self.assertEqual(self.runs.preview(body)["selected_endpoint"], "A")

    def test_duplicate_gateway_receipt_cannot_double_settle(self):
        rid = self.reviews()
        first, second = self.runs.claim(), self.runs.claim()
        for att in (first, second):
            self.runs.dispatch(att)
            self.runs.finish(
                att,
                text="answer",
                usage={"prompt_tokens": 1, "completion_tokens": 3, "total_tokens": 4},
                receipt="one",
            )
        budget = self.runs.get(rid)["budget"]
        self.assertEqual(budget["settled_micros"], 3)
        self.assertEqual(budget["uncertain_attempts"], [second])
        self.assertEqual(budget["held_micros"], 70)

    def test_replacing_draft_invalidates_generation_specific_feedback(self):
        rid = self.reviews()
        self.runs.invalidate(rid, "review-product", feedback="旧版仅管理员")
        self.runs.replace_input(rid, "draft", {"clauses": ["所有用户"]})
        att = self.runs.claim()
        messages = self.runs.dispatch(att)[1]
        self.assertNotIn("旧版仅管理员", str(messages))
        self.assertIn("所有用户", str(messages))

    def test_alias_may_name_a_tagged_model_but_platform_id_is_a_url_segment(self):
        self.assertEqual(
            self.cat.save(dict(self.a, endpoint_alias="llama/review:8b"))["endpoint_alias"], "llama/review:8b"
        )
        for invalid in ("..", ".", "a/b"):
            with self.assertRaises(ValueError):
                self.cat.save(dict(self.a, endpoint_id=invalid))
