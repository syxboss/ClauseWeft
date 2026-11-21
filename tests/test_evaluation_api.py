"""Run paired evaluations through the application and real worker accounting, with stub inference."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from clauseweft.api.application import Application
from clauseweft.api.server import Problem
from platform_fixture import prepare_team


class PairedEvaluationAPI(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        config = root / "config.json"
        config.write_text(json.dumps({"gateways":{"local":{"url":"http://127.0.0.1:8765"}}}))
        self.app = Application(root / "state", config)
        prepare_team(self.app)
        self.used = []

        def generate(endpoint, messages, limit):
            self.used.append(endpoint["endpoint_id"])
            return "做出决定的依据", dict(prompt_tokens=1, completion_tokens=1, total_tokens=2), uuid4().hex
        self.app.catalog.generate = generate
        for name, price in (("expensive", 3_000_000), ("cheap", 1_000_000), ("excluded", 0)):
            self.post(("endpoints",), dict(endpoint_id=name, model_identity=name, location="local",
                      enabled=True, allowed=True, context_tokens=4096, capabilities=[], fit={"review":5},
                      priority=1, input_micros_per_million=price, output_micros_per_million=price,
                      gateway_id="local", gateway_etag='"test"'))
        prepare_team(self.app)

    def post(self, parts, body, revision=None):
        return self.app.handle("POST", parts, {}, body, key=uuid4().hex, expected=revision)[1]

    def test_malformed_empty_and_missing_samples(self):
        for sample in (None, {"items":None}, {"version":"v","items":[None]}):
            with self.assertRaises(ValueError):
                self.post(("evaluations",), {"mode":"paired", "dataset":sample, "fixed_endpoint":"cheap"})
        params = {"mode":"paired", "fixed_endpoint":"cheap",
                  "dataset":{"version":"v", "items":[{"id":"a","snapshot":None}, {"id":"b"}]}}
        eid = self.post(("evaluations",), params)["evaluation_id"]
        report = self.app.handle("GET", ("evaluations",eid), {}, {})[1]
        self.assertEqual(report["total"], 2)
        self.assertEqual(report["automatic"]["unexecuted"], 2)
        self.assertFalse(self.app.runs.work_once())
        with self.assertRaises(ValueError):
            self.post(("evaluations",eid,"grades"), {"unknown":True}, 1)

    def test_client_cannot_widen_workspace_scope_or_set_execution_prices(self):
        task = {"text":"评审", "budget_micros":0, "data_scope":"public"}
        sample = {"id":"a","snapshot":{"task":task, "models":[{"fake":"config"}]},
                  "allowed_endpoints":["cheap"], "acceptance":"明确权限"}
        params = {"mode":"paired", "fixed_endpoint":"cheap", "dataset":{"version":"v", "items":[sample]}}
        with self.assertRaises(Problem) as raised:
            self.post(("evaluations",), params)
        self.assertEqual(raised.exception.status, 403)
        task.pop("data_scope")
        saved = json.dumps(params)
        _, created = self.app.handle("POST", ("evaluations",), {}, params, key="stable")
        self.assertEqual(self.app.handle("POST", ("evaluations",), {}, params, key="stable")[1], created)
        self.assertEqual(json.dumps(params), saved)
        self.assertFalse(self.app.runs.work_once())
        report = self.app.handle("GET", ("evaluations", created["evaluation_id"]), {}, {})[1]
        self.assertEqual(report["automatic"]["unexecuted"], 1)

    def test_partial_question_keeps_valid_pair_and_denominator(self):
        dataset = {"version": "v", "items": [
            {"id": key, "acceptance": "指出依据", "allowed_endpoints": ["cheap"],
             "snapshot": {"task": task}}
            for key, task in (("complete", {"text": "评审", "budget_micros": 10000}),
                              ("missing-budget", {"text": "评审"}))
        ]}
        eid = self.post(("evaluations",), {"mode": "paired", "fixed_endpoint": "cheap",
                                           "dataset": dataset})["evaluation_id"]
        self.assertTrue(self.app.runs.work_once())
        self.assertTrue(self.app.runs.work_once())
        self.assertFalse(self.app.runs.work_once())
        report = self.app.handle("GET", ("evaluations", eid), {}, {})[1]
        self.assertEqual(report["total"], 2)
        for arm in ("fixed", "automatic"):
            self.assertEqual(report[arm]["unexecuted"], 1)
            self.assertEqual(report[arm]["awaiting_grade"], 1)

    def test_rejected_fixed_call_is_failed_while_bill_is_pending(self):
        dataset = {"version": "v", "items": [
            {"id": "review", "acceptance": "指出依据", "allowed_endpoints": ["cheap"],
             "snapshot": {"task": {"text": "评审", "budget_micros": 10000}}}
        ]}
        eid = self.post(("evaluations",), {"mode": "paired", "fixed_endpoint": "cheap",
                                           "dataset": dataset})["evaluation_id"]
        attempt = self.app.runs.claim()
        self.assertIsNotNone(self.app.runs.dispatch(attempt))
        path = ("evaluations", eid)
        fixed = self.app.handle("GET", path, {}, {})[1]["fixed"]
        self.assertEqual((fixed["pending_execution"], fixed["awaiting_grade"]), (1, 0))
        self.app.runs.finish(attempt, error="upstream_rate_limited")
        fixed = self.app.handle("GET", path, {}, {})[1]["fixed"]
        self.assertEqual((fixed["failed"], fixed["pending_execution"], fixed["awaiting_grade"]), (1, 0, 0))
        self.assertEqual(fixed["pending_cost_items"], 1)
        self.assertIsNone(fixed["cost_per_success_micros"])

    def test_blind_grade_after_both_arms_and_frozen_pool_changes(self):
        dataset = {"version":"1","items":[{"id":"q","acceptance":"指出必要依据",
                   "allowed_endpoints":["expensive","cheap"], "snapshot":{"task":{
                   "text":"评审权限", "stage":"review", "max_output_tokens":20, "budget_micros":10000,
                   "required_capabilities":[], "preference":"cost", "data_scope":"local_only"}}}]}
        def create():
            return self.post(("evaluations",), {"mode":"paired","dataset":dataset,
                                                "fixed_endpoint":"expensive"})["evaluation_id"]
        a = create()
        for _ in range(2):
            self.assertTrue(self.app.runs.work_once())
        self.assertCountEqual(self.used, ["expensive","cheap"])
        blind = self.app.handle("GET",("evaluations",a,"blind"),{}, {})[1]
        self.assertEqual(len(blind["items"]), 2)
        self.assertTrue(all(set(i) == {"blind_id","item_id","acceptance","artifact"} for i in blind["items"]))
        grades = {i["blind_id"]: True for i in blind["items"]}
        report = self.post(("evaluations",a,"grades"), grades, blind["revision"])
        self.assertEqual(report["paired_coverage"], 1)
        self.assertEqual(report["fixed"]["cost_per_success_micros"], 6)
        self.assertEqual(report["automatic"]["cost_per_success_micros"], 2)

        b = create()
        for eid in ("cheap","expensive"):
            self.app.handle("PATCH",("endpoints",eid),{},{"enabled":False},uuid4().hex,1)
        for _ in range(2):
            self.assertTrue(self.app.runs.work_once())
        report = self.app.handle("GET",("evaluations",b),{}, {})[1]
        self.assertEqual(report["fixed"]["unexecuted"], 1)
        self.assertEqual(report["automatic"]["unexecuted"], 1)
        self.assertCountEqual(self.used, ["expensive","cheap"])
