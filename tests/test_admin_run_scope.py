"""Administrative runs on a new installation with real project identifiers."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from clauseweft.api.application import Application
from clauseweft.api.server import Problem


class AdminRunScopeTest(unittest.TestCase):
    def setUp(self):
        scratch = TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        root = Path(scratch.name)
        config = root / "config.json"
        config.write_text(json.dumps({"gateways": {"g": {"url": "http://127.0.0.1:8765"}}}))
        self.app = Application(root / "state", config)
        self.post("endpoints", {
            "endpoint_id": "local", "model_identity": "local", "gateway_id": "g", "gateway_etag": '"v1"',
            "location": "local", "enabled": True, "allowed": True, "capabilities": [], "fit": {},
            "currency": "CNY", "priority": 1, "context_tokens": 8192,
            "input_micros_per_million": 1, "output_micros_per_million": 1,
        })
        self.workspace = self.post("workspaces", {"name": "研发", "endpoint_ids": ["local"]})["id"]
        self.project = self.post("projects", {"name": "结算", "workspace_id": self.workspace})["id"]
        self.scope = {"project_id": self.project, "workspace_id": self.workspace}
        self.probe = {"kind": "structured_output", "budget": {"currency": "CNY", "limit_micros": 5000}}
        self.eval = {"mode": "paired", "fixed_endpoint": "local", "currency": "CNY",
                     "dataset": {"version": "v1", "items": [{
                         "id": "q", "acceptance": "列出依据", "allowed_endpoints": ["local"],
                         "snapshot": {"task": {"text": "评审", "budget_micros": 5000}},
                     }]}}
        self.app.catalog.generate = lambda *args: (
            '{"check":"ok"}', {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, uuid4().hex)

    def post(self, path, body, revision=None):
        return self.app.handle("POST", tuple(path.split("/")), {}, body, uuid4().hex, revision)[1]

    def test_nondefault_project_owns_probe_and_both_evaluation_arms(self):
        with self.assertRaises(Problem):
            self.app.handle("GET", ("projects", "default"), {}, {})
        probe = self.post("endpoints/local/probes", {**self.probe, **self.scope}, 1)
        evaluation = self.post("evaluations", {**self.eval, **self.scope})
        completed = 0
        while self.app.runs.work_once():
            completed += 1
        self.assertEqual(completed, 3)
        runs = self.app.handle("GET", ("runs",), self.scope, {})[1]["items"]
        self.assertEqual(len(runs), 3)
        self.assertTrue(all(run["status"] == "completed" and run["workspace_id"] == self.workspace
                            and run["project_id"] == self.project for run in runs))
        history = self.app.handle("GET", ("endpoints", "local", "probes"), {}, {})[1]["items"]
        self.assertEqual((history[0]["run_id"], history[0]["verified"]), (probe["run_id"], True))
        report = self.app.handle("GET", ("evaluations", evaluation["evaluation_id"]), {}, {})[1]
        self.assertEqual((report["workspace_id"], report["project_id"]), (self.workspace, self.project))
        self.assertEqual((report["fixed"]["awaiting_grade"], report["automatic"]["awaiting_grade"]), (1, 1))

    def test_empty_ambiguous_archived_mismatched_or_unauthorized_scopes(self):
        with self.assertRaisesRegex(ValueError, "project_id"):
            self.post("endpoints/local/probes", self.probe, 1)
        self.post("evaluations", {**self.eval, "workspace_id": self.workspace})
        self.post("projects", {"name": "二期", "workspace_id": self.workspace})
        with self.assertRaisesRegex(ValueError, "project_id"):
            self.post("evaluations", {**self.eval, "workspace_id": self.workspace})
        for path, data, rev in (("endpoints/local/probes", self.probe, 1), ("evaluations", self.eval, None)):
            with self.assertRaises(Problem) as error:
                self.post(path, {**data, "project_id": self.project, "workspace_id": "default"}, rev)
            self.assertEqual(error.exception.code, "WORKSPACE_FORBIDDEN")
        other = self.post("projects", {"name": "无模型的项目"})["id"]
        with self.assertRaises(Problem) as error:
            self.post("endpoints/local/probes", {**self.probe, "project_id": other}, 1)
        self.assertEqual(error.exception.code, "MODEL_FORBIDDEN")
        self.app.handle("PATCH", ("projects", self.project), {}, {"status": "archived"}, uuid4().hex, 1)
        with self.assertRaises(Problem) as error:
            self.post("evaluations", {**self.eval, **self.scope})
        self.assertEqual(error.exception.code, "PROJECT_ARCHIVED")
