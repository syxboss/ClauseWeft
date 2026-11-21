import json
import unittest
from pathlib import Path

from clauseweft.routing.preview import preview


class PreviewTests(unittest.TestCase):
    def setUp(self):
        fixture = Path(__file__).resolve().parents[1] / "examples" / "route-demo.json"
        self.snapshot = json.loads(fixture.read_text(encoding="utf-8"))
        self.task = self.snapshot["task"]

    def test_privacy_overrides_price_and_pin(self):
        decision = preview(self.snapshot)
        self.assertEqual(decision["selected_endpoint"], "local-review")
        self.assertEqual(decision["data_scope"], "local_only")
        self.assertEqual(
            {c["endpoint_id"]: c["location"] for c in decision["candidates"]},
            {"local-review": "local", "cloud-general": "cloud"},
        )
        self.task["pinned_endpoint"] = "cloud-general"
        self.assertEqual(preview(self.snapshot)["status"], "blocked")
        self.task["data_scope"] = "public"
        decision = preview(self.snapshot)
        self.assertEqual(decision["data_scope"], "public")
        self.assertEqual(decision["selected_endpoint"], "cloud-general")
        self.assertEqual(decision["candidates"][0]["location"], "cloud")

    def test_budget_rounds_up_and_unknown_price_blocks(self):
        local = self.snapshot["models"][0]
        local.update(input_micros_per_million=1, output_micros_per_million=0)
        self.task.update(input_tokens=1, budget_micros=0)
        decision = preview(self.snapshot)
        self.assertEqual(decision["status"], "blocked")
        self.assertEqual(decision["candidates"][1]["estimated_micros"], 1)
        self.task["budget_micros"] = 1
        self.assertEqual(preview(self.snapshot)["status"], "selected")
        del local["input_micros_per_million"]
        self.assertEqual(preview(self.snapshot)["status"], "blocked")

    def test_missing_quality_uses_manual_priority(self):
        self.task.update(data_scope="public", preference="quality")
        self.snapshot["models"][1]["quality_bp"] = 9900
        decision = preview(self.snapshot)
        self.assertEqual(decision["rank_metric"], "priority")
        self.assertEqual(decision["selected_endpoint"], "local-review")

    def test_context_reserves_output_and_ambiguous_intent_waits(self):
        self.task.update(input_tokens=7900, max_output_tokens=600)
        self.assertEqual(preview(self.snapshot)["status"], "blocked")
        self.task.update(text="先评审再实现", input_tokens=2400)
        self.assertEqual(preview(self.snapshot)["status"], "needs_input")

    def test_invalid_data_scope_is_rejected(self):
        self.task["data_scope"] = "locl_only"
        with self.assertRaises(ValueError):
            preview(self.snapshot)

    def test_general_task_uses_configured_models_for_arbitrary_content(self):
        self.task.update(text="帮我整理这段内容", stage="general")
        decision = preview(self.snapshot)
        self.assertEqual(decision["status"], "selected")
        self.assertEqual(decision["intent"], "general")
        self.assertEqual(decision["selected_endpoint"], "local-review")
        self.task["pinned_endpoint"] = "cloud-general"
        self.assertEqual(preview(self.snapshot)["status"], "blocked")


if __name__ == "__main__":
    unittest.main()
