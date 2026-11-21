"""Default goal-to-result path with self-configured models and inferred currency."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from clauseweft import audit
from clauseweft.api.application import Application
from platform_fixture import prepare_team


class SimpleTasksTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        config = self.root / "config.json"
        config.write_text(json.dumps({"gateways": {"local": {"url": "http://127.0.0.1:8765"}}}))
        self.app = Application(self.root / "state", config)
        self.calls = []
        self.configure("my-model", "CNY")
        prepare_team(self.app)

        def generate(endpoint, messages, limit):
            self.calls.append((endpoint["endpoint_id"], messages))
            return "整理后的内容与实施建议", {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, uuid4().hex
        self.app.catalog.generate = generate

    def configure(self, endpoint_id, currency):
        return self.app.catalog.save({
            "endpoint_id": endpoint_id, "model_identity": endpoint_id,
            "gateway_id": "local", "gateway_etag": '"configured"',
            "enabled": True, "allowed": True, "location": "local",
            "context_tokens": 32000, "capabilities": [], "fit": {}, "priority": 1,
            "currency": currency, "input_micros_per_million": 4000000,
            "output_micros_per_million": 8000000,
        })

    def call(self, method, path, body=None, key=None):
        return self.app.handle(method, tuple(path.split("/")), {}, body or {}, key or uuid4().hex)[1]

    def test_goal_runs_without_requirement_template_gate_or_manual_budget(self):
        body = {"project_id": "default", "task": {"text": "帮我整理一份简明的工作说明"}}
        preview = self.call("POST", "routing/previews", body)
        self.assertEqual(preview["status"], "selected")
        self.assertEqual(preview["intent"], "general")
        self.assertEqual(preview["budget"]["currency"], "CNY")
        self.assertEqual(preview["budget"]["source"], "model_estimate")
        self.assertGreater(preview["budget"]["limit_micros"], 0)
        created = self.call("POST", "runs", body, "simple-task")
        self.assertEqual(created["mode"], "single")
        self.assertEqual(len(created["nodes"]), 1)
        self.assertEqual(created["nodes"][0]["kind"], "model")
        self.assertTrue(self.app.runs.work_once())
        result = self.call("GET", "runs/" + created["run_id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["task"]["text"], body["task"]["text"])
        self.assertEqual(result["budget"]["currency"], "CNY")
        self.assertEqual(result["budget"]["settled_micros"], 20)
        self.assertEqual(result["budget_source"], "model_estimate")
        report = self.call("GET", "usage")
        model = next(row for row in report["models"] if row["endpoint_id"] == "my-model")
        self.assertEqual((model["calls"], model["input_tokens"], model["output_tokens"]), (1, 1, 2))
        self.assertEqual(model["settled_micros"], 20)
        self.assertEqual(self.call("POST", "runs", body, "simple-task"), created)
        self.assertEqual(len(self.calls), 1)
        with self.app.store.transaction() as db:
            self.assertEqual(self.app.platform.list(db, "requirements", {}), [])
            self.assertEqual(self.app.platform.list(db, "templates", {}), [])
            self.assertTrue(audit.verify(db)["valid"])

    def test_text_materials_keep_goal_separate_and_count_towards_request(self):
        materials = [{"name": "接口记录.md", "text": "  字段含义与示例\n<amount>  "}]
        body = {"project_id": "default", "task": {"text": "总结变化"}}
        baseline = self.call("POST", "routing/previews", body)["budget"]["limit_micros"]
        body["task"]["materials"] = materials
        preview = self.call("POST", "routing/previews", body)
        self.assertGreater(preview["budget"]["limit_micros"], baseline)
        run = self.call("POST", "runs", body)
        self.assertTrue(self.app.runs.work_once())
        path = "runs/" + run["run_id"]
        detail = self.call("GET", path)
        self.assertEqual(detail["task"]["text"], "总结变化")
        self.assertEqual(detail["task"]["materials"], materials)
        actual = self.calls[0][1]
        self.assertIn({"role": "user", "content": "参考材料：接口记录.md\n  字段含义与示例\n<amount>  "}, actual)
        self.assertEqual(self.call("GET", path + "/context")["items"][0]["messages"], actual)

    def test_rejects_malformed_or_oversized_materials_before_dispatch(self):
        valid = {"name": "sample.txt", "text": "hello"}
        for items in ({"name": "sample"}, [valid] * 5, [dict(valid, name="../server.log")],
                      [dict(valid, role="system")], [dict(valid, text="x\x00y")],
                      [dict(valid, text="文" * 22000)]):
            with self.subTest(example=repr(items)[:55]), self.assertRaisesRegex(ValueError, "材料"):
                self.call("POST", "runs", {"project_id": "default", "task": {"text": "复核", "materials": items}})
        self.assertFalse(self.calls)

    def test_manual_model_resolves_multiple_currency_pool_without_exchange_rates(self):
        self.configure("another-model", "USD")
        prepare_team(self.app)
        body = {"project_id": "default", "task": {"text": "整理内容"}}
        with self.assertRaisesRegex(ValueError, "不同币种"):
            self.call("POST", "routing/previews", body)
        body["task"]["pinned_endpoint"] = "my-model"
        preview = self.call("POST", "routing/previews", body)
        self.assertEqual(preview["selected_endpoint"], "my-model")
        self.assertEqual(preview["budget"]["currency"], "CNY")

    def test_explicit_zero_budget_keeps_the_spend_limit(self):
        created = self.call("POST", "runs", {"project_id": "default", "task": {"text": "整理内容", "pinned_endpoint": "my-model"}, "budget": {"currency": "CNY", "limit_micros": 0}})
        self.assertTrue(self.app.runs.work_once())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.call("GET", "runs/" + created["run_id"])["budget"]["settled_micros"], 0)

    def test_complex_task_is_previewed_locally_then_delivers_one_summary(self):
        body = {"mode": "complex", "project_id": "default", "task": {"text": "归纳两组反馈"}}
        with self.assertRaisesRegex(ValueError, "总预算"):
            self.call("POST", "runs", body)
        body["budget"] = {"currency": "CNY", "limit_micros": 100000}
        body["task"]["pinned_endpoint"] = "my-model"
        preview = self.call("POST", "routing/previews", body)
        self.assertEqual(preview["preview_node"], "plan")
        self.assertEqual(preview["selected_endpoint"], "my-model")
        run = self.call("POST", "runs", body, "complex")
        self.assertEqual(self.calls, [])
        replies = iter([json.dumps({"subtasks": ["整理买家反馈", "整理卖家反馈"]}),
                        "买家关注速度", "卖家关注售后", "结论与建议"])

        def generate(endpoint, messages, limit):
            self.calls.append((endpoint["endpoint_id"], messages))
            return next(replies), {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}, uuid4().hex

        self.app.catalog.generate = generate
        for _ in range(4):
            self.assertTrue(self.app.runs.work_once())
        self.assertFalse(self.app.runs.work_once())
        self.assertEqual(self.call("POST", "runs", body, "complex"), run)
        path = "runs/" + run["run_id"]
        result = self.call("GET", path)
        self.assertEqual(result["status"], "completed")
        summary = next(node for node in result["nodes"] if node["node_id"] == "summary")
        self.assertEqual(self.call("GET", path + "/artifacts/" + summary["artifact"])["content"], "结论与建议")
        contexts = self.call("GET", path + "/context")["items"]
        self.assertEqual(len(contexts), 4)
        self.assertTrue(all(c["endpoint_id"] == "my-model" and c["context_sha256"] for c in contexts))
        self.assertEqual(result["budget"]["settled_micros"], 80)
        self.assertEqual(self.call("GET", "usage")["models"][0]["calls"], 4)


if __name__ == "__main__":
    unittest.main()
