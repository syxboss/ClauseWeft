"""Exercise tool proposal, consent, execution and grounded replies through the API."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from clauseweft.api.application import Application
from clauseweft.api.server import Problem
from platform_fixture import prepare_team


class ProjectToolsTest(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.reference = self.root / "references.json"
        self.reference.write_text(json.dumps([{"id": "payments", "title": "对账", "text": "退款需要核对业务编号。"}]))
        config = self.root / "config.json"
        config.write_text(json.dumps({"gateways": {"g": {"url": "http://127.0.0.1:8765"}}, "data_scope": "public",
            "project_references": {"default": {"path": str(self.reference), "name": "项目手册"}}}))
        self.app = Application(self.root / "state", config)
        self.app.catalog.save({
            "endpoint_id": "local", "model_identity": "m", "gateway_id": "g", "gateway_etag": '"v"',
            "location": "local", "fit": {}, "currency": "CNY", "context_tokens": 32000,
            "enabled": True, "allowed": True, "capabilities": [], "priority": 1,
            "input_micros_per_million": 1, "output_micros_per_million": 1,
        })
        prepare_team(self.app)
        self.messages = []
        self.reply = "answer"
        def generate(endpoint, messages, limit):
            self.messages.append(messages)
            return self.reply, {"prompt_tokens": 15, "completion_tokens": 18, "total_tokens": 33}, uuid4().hex
        self.app.catalog.generate = generate

    def call(self, method, path, data=None, key=None, rev=None, principal=None):
        return self.app.handle(method, tuple(path.split("/")), {}, data or {}, key or uuid4().hex,
                               rev, principal=principal)[1]

    def propose(self, tool, reply):
        self.reply = json.dumps(reply, ensure_ascii=False)
        request = {"mode": "tool", "tool_id": tool, "task": {"text": "处理项目事项"},
                   "project_id": "default", "budget": {"limit_micros": 100000, "currency": "CNY"}}
        preview = self.call("POST", "routing/previews", request)
        self.assertEqual(preview["preview_node"], "propose")
        run = self.call("POST", "runs", request)
        self.assertTrue(self.app.runs.work_once())
        self.assertFalse(self.app.runs.work_once())
        run = self.call("GET", "runs/" + run["run_id"])
        self.assertEqual(run["status"], "needs_input")
        return run

    def decision(self, run, action="approve", key=None, principal=None):
        return self.call("POST", "runs/" + run["run_id"] + "/tool-decisions",
                         {"action": action, "proposal_ref": run["tool_proposal"]["proposal_ref"], "reason": "已核对"},
                         key=key, rev=run["revision"], principal=principal)

    def begin_lookup(self):
        run = self.call("POST", "runs", {
            "mode": "tool", "allowed_tools": ["search_requirements", "create_requirement"],
            "requirement_query": "退款", "project_id": "default", "task": {"text": "查找退款需求，缺少时新建"},
            "budget": {"limit_micros": 100000, "currency": "CNY"},
        })
        self.rid = run["run_id"]
        return self.advance({"action": "tool", "tool_id": "search_requirements", "arguments": {"query": "退款"}})

    def advance(self, response):
        self.reply = json.dumps(response, ensure_ascii=False)
        self.assertTrue(self.app.runs.work_once())
        return self.call("GET", "runs/" + self.rid)

    def assess(self, scenario):
        return self.app.handle("GET", ("runs", self.rid, "goal-assessment"),
                               {"scenario": scenario, "query": "退款", "title": "退款对账"}, {})[1]

    def propose_creation(self):
        self.decision(self.begin_lookup())
        return self.advance({"action": "tool", "tool_id": "create_requirement",
                             "arguments": {"title": "退款对账", "description": "按订单号核验", "priority": "high"}})

    def test_existing_goal_checks_final_answer_receipt_and_live_requirement(self):
        item = self.call("POST", "requirements", {"title": "退款对账", "project_id": "default"})
        run = self.begin_lookup()
        self.decision(run)
        self.assertEqual(self.assess("existing")["status"], "pending")
        self.advance({"action": "finish", "answer": "命中原有需求 " + item["id"]})
        report = self.assess("existing")
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual(report["evidence"]["project_requirement_ids"], [item["id"]])
        self.assertEqual(self.assess("created")["status"], "failed")
        self.call("PATCH", "requirements/" + item["id"], {"title": "另一个标题"}, rev=item["revision"])
        changed = self.assess("existing")
        self.assertEqual(changed["status"], "failed")
        self.assertFalse(changed["checks"]["hit_matches_live_requirement"])

    def test_created_goal_requires_approved_creation_and_cited_real_id(self):
        proposal = self.propose_creation()
        self.assertEqual(self.call("GET", "requirements")["items"], [])
        self.assertEqual(self.assess("created")["status"], "pending")
        self.decision(proposal)
        new_id = self.call("GET", "requirements")["items"][0]["id"]
        self.advance({"action": "finish", "answer": "已创建 " + new_id})
        report = self.assess("created")
        self.assertEqual(report["status"], "passed", report)
        self.assertEqual([r["node_id"] for r in report["evidence"]["receipts"]], ["confirm-1", "confirm-2"])
        self.assertTrue(report["evidence"]["final_answer"])
        self.assertEqual(self.assess("rejected")["status"], "failed")

    def test_rejected_goal_requires_actual_rejection_of_the_target_and_no_write(self):
        proposal = self.propose_creation()
        self.decision(proposal, "reject")
        report = self.assess("rejected")
        self.assertEqual(report["status"], "passed", report)
        self.assertFalse(report["evidence"]["project_requirement_ids"])
        self.assertEqual(report["evidence"]["rejected_proposals"], [proposal["tool_proposal"]["proposal_ref"]])
        self.assertEqual(self.assess("created")["status"], "failed")

    def test_created_goal_fails_on_concurrent_insert_and_ungrounded_answer(self):
        proposal = self.propose_creation()
        concurrent = self.call("POST", "requirements", {"title": "退款对账", "project_id": "default"})
        self.decision(proposal)
        self.advance({"action": "finish", "answer": "我已经为你新建了需求。"})
        incomplete = self.assess("created")
        self.assertEqual(incomplete["status"], "pending")
        self.assertFalse(incomplete["checks"]["answer_cites_requirement"])
        run = self.call("GET", "runs/" + self.rid)
        self.call("POST", "runs/" + self.rid + "/resume", {}, rev=run["revision"])
        self.advance({"action": "finish", "answer": "已有需求 " + concurrent["id"]})
        report = self.assess("created")
        self.assertEqual(report["status"], "failed", report)
        self.assertEqual(report["evidence"]["project_requirement_ids"], [concurrent["id"]])
        self.assertFalse(report["checks"]["approved_target_created"])
        self.assertTrue(report["checks"]["answer_cites_requirement"])

    def test_write_requires_review_and_is_atomic_with_receipt(self):
        args = {"title": "退款核对", "description": "按业务编号关联收据", "priority": "medium"}
        run = self.propose("create_requirement", args)
        self.assertEqual(run["tool_proposal"]["arguments"], args)
        self.assertFalse(self.call("GET", "requirements")["items"])
        with self.assertRaises((Problem, ValueError)):
            self.call("POST", "runs/" + run["run_id"] + "/gates/confirm", {"action": "approve", "reason": "跳过"}, rev=run["revision"])
        with self.assertRaises(Problem):
            self.call("POST", "runs/" + run["run_id"] + "/tool-decisions", {
                "action": "approve", "reason": "已确认", "proposal_ref": "wrong"}, rev=run["revision"])
        viewer = self.call("POST", "members", {"username":"reader","password":"test-12-password","role":"viewer",
                           "display_name":"读者", "project_ids":["default"],"workspace_ids":["default"]})
        with self.assertRaises(Problem):
            self.decision(run, principal=viewer)
        approved = self.decision(run, key="execute-once")
        self.assertEqual(approved, self.decision(run, key="execute-once"))
        with self.assertRaises(Problem):
            self.decision(run)
        items = self.call("GET", "requirements")["items"]
        self.assertEqual([i["title"] for i in items], ["退款核对"])
        self.reply = "已登记"
        self.assertTrue(self.app.runs.work_once())
        self.assertIn(items[0]["id"], self.messages[-1][-1]["content"])
        self.assertEqual(self.app.run(run["run_id"])["status"], "completed")
        events = self.call("GET", "runs/" + run["run_id"] + "/events")["items"]
        self.assertEqual(sum(event["kind"] == "tool_executed" for event in events), 1)
        self.assertTrue(self.call("GET", "audit/verify")["valid"])

    def test_structured_arguments_and_rejection(self):
        run = self.propose("create_requirement", {"project_id": "elsewhere", "query": "退款"})
        self.assertIn("error", run["tool_proposal"])
        with self.assertRaises(ValueError):
            self.decision(run)
        self.decision(run, "revise")
        self.reply = '{"title":"修订","description":"验收","priority":"high"}'
        self.assertTrue(self.app.runs.work_once())
        revised = self.call("GET", "runs/" + run["run_id"])
        self.assertNotEqual(run["tool_proposal"]["proposal_ref"], revised["tool_proposal"]["proposal_ref"])
        self.decision(revised, "reject")
        self.assertEqual(self.app.run(run["run_id"])["status"], "cancelled")
        self.assertFalse(self.call("GET", "requirements")["items"])

    def test_adaptive_proposal_rework_replay_and_tool_limit(self):
        plan = {"mode": "tool", "allowed_tools": ["search_requirements"], "max_tool_steps": 1,
                "task": {"text": "查退款"}, "project_id": "default", "budget": {"currency": "CNY", "limit_micros": 100000}}
        self.rid = self.call("POST", "runs", plan)["run_id"]
        path = "runs/" + self.rid
        query = {"action": "tool", "tool_id": "search_requirements", "arguments": {"query": "退款"}}
        foreign = self.advance(dict(query, tool_id="create_requirement"))
        self.assertEqual(foreign["waiting_actions"][0]["reason"], "invalid_agent_decision")
        self.assertIsNone(foreign["tool_proposal"])
        self.call("POST", path + "/resume", rev=foreign["revision"])
        first = self.advance(query)
        self.decision(first, "revise")
        revised = self.advance({"tool": "search_requirements", "arguments": {"query": "退款对账"}})
        self.assertNotEqual(revised["tool_proposal"]["proposal_ref"], first["tool_proposal"]["proposal_ref"])
        with self.assertRaises(Problem):
            self.decision(first)
        decision = self.decision(revised, key="approve-current-only")
        self.assertEqual(self.decision(revised, key="approve-current-only"), decision)
        limited = self.advance(query)
        self.assertEqual(limited["waiting_actions"][0]["reason"], "tool_step_limit")
        self.assertIsNone(limited["tool_proposal"])
        self.call("POST", path + "/resume", rev=limited["revision"])
        final = self.advance({"action": "finish", "answer": "没有命中。"})
        self.assertEqual(final["status"], "completed")
        self.assertEqual([n["node_id"] for n in final["nodes"]], ["decide-1", "confirm-1", "decide-2"])
        self.assertEqual(self.app.store.read_blob(final["nodes"][-1]["artifact"]).decode(), "没有命中。")
        self.assertIn("已到工具执行次数上限", str(self.messages[-1]))
        previous_call = next(m for m in self.messages[-1] if m["role"] == "assistant")
        self.assertEqual(json.loads(previous_call["content"]), {"action": "tool", "tool_id": "search_requirements",
                                                               "arguments": {"query": "退款对账"}})
        self.assertIn(revised["tool_proposal"]["proposal_ref"], self.messages[-1][-1]["content"])

    def test_cancelled_or_replayed_decision_cannot_expand_graph(self):
        plan = {"mode": "tool", "allowed_tools": ["search_requirements"], "task": {"text": "查找项目需求"},
                "project_id": "default", "budget": {"currency": "CNY", "limit_micros": 100000}}
        rid = self.call("POST", "runs", plan)["run_id"]
        attempt = self.app.runs.claim()
        self.assertTrue(self.app.runs.dispatch(attempt))
        self.app.runs.cancel(rid)
        text = json.dumps({"action": "tool", "tool_id": "search_requirements", "arguments": {"query": "退款"}})
        for _ in range(2):
            self.app.runs.finish(attempt, text=text, usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                                 receipt="same-final-receipt")
        run = self.call("GET", "runs/" + rid)
        self.assertEqual(run["status"], "cancelled")
        self.assertEqual([n["node_id"] for n in run["nodes"]], ["decide-1"])
        self.assertIsNone(run["tool_proposal"])

    def test_conditional_goal_requires_search_and_proof_before_completion(self):
        first = self.begin_lookup()
        self.decision(first)
        early = self.advance({"action": "finish", "answer": "没有找到，建议新建。"})
        self.assertEqual(early["waiting_actions"][0]["reason"], "invalid_agent_decision")
        self.assertEqual(self.call("GET", "requirements")["items"], [])
        self.assertFalse(self.app.runs.work_once())
        with self.app.store.transaction() as db:
            evidence = db.execute("SELECT artifact FROM attempts WHERE attempt_id=?", (early["nodes"][-1]["attempt_id"],)).fetchone()[0]
        self.assertIn("建议新建", self.app.store.read_blob(evidence).decode())

    def test_single_tool_follow_on_proposal_is_not_a_successful_answer(self):
        run = self.propose("search_requirements", {"query": "退款对账"})
        self.decision(run)
        self.reply = json.dumps({"tool": "create_requirement", "arguments": {
            "title": "退款对账", "description": "核对退款单", "priority": "medium"}})
        self.assertTrue(self.app.runs.work_once())
        run = self.call("GET", "runs/" + run["run_id"])
        self.assertEqual(run["status"], "needs_input")
        self.assertEqual(run["waiting_actions"][0]["reason"], "additional_tool_requested")
        self.assertFalse(run["nodes"][-1]["artifact"])
        self.assertEqual(self.call("GET", "requirements")["items"], [])

    def test_followup_from_write_uses_confirmed_receipt_and_never_repeats_action(self):
        run = self.propose("create_requirement", {"title": "退款", "description": "核对编号", "priority": "low"})
        path = "runs/" + run["run_id"]
        payload = {"action": "followup", "text": "再次创建，并解释结果"}
        with self.assertRaises(Problem):
            self.call("POST", path + "/continuations", payload, rev=run["revision"])
        self.decision(run)
        self.reply = "登记成功"
        self.assertTrue(self.app.runs.work_once())
        finished = self.call("GET", path)
        self.assertEqual(finished["continuation_actions"], ["followup"])
        with self.assertRaises(Problem):
            self.call("POST", path + "/continuations", {"action": "regenerate"}, rev=finished["revision"])
        with self.assertRaises(ValueError):
            self.call("POST", path + "/continuations", dict(payload, tool_id="create_requirement"), rev=finished["revision"])
        with self.assertRaises(ValueError):
            self.call("POST", path + "/continuations", dict(payload, project_id="example-project"), rev=finished["revision"])
        child = self.call("POST", path + "/continuations", payload, rev=finished["revision"], key="explain-once")
        self.assertEqual(child, self.call("POST", path + "/continuations", payload, rev=finished["revision"], key="explain-once"))
        self.assertEqual((child["mode"], child["project_id"], child["source_run_id"]), ("single", "default", run["run_id"]))
        self.assertNotIn("tool_id", child)
        self.assertEqual([node["node_id"] for node in child["nodes"]], ["task"])
        self.assertLessEqual(child["budget"]["limit_micros"], run["budget"]["limit_micros"])
        self.assertTrue(self.app.runs.work_once())
        completed = self.call("GET", "runs/" + child["run_id"])
        self.assertEqual(completed["status"], "completed")
        artifacts = {node["node_id"]: node["artifact"] for node in finished["nodes"]}
        turn = completed["conversation"]["turns"][0]
        self.assertEqual((turn["answer_ref"], turn["receipt_refs"]), (artifacts["answer"], [artifacts["confirm"]]))
        self.assertEqual(self.messages[-1][-2:], [
            {"role": "assistant", "content": "登记成功"}, {"role": "user", "content": payload["text"]},
        ])
        items = self.call("GET", "requirements")["items"]
        self.assertEqual(len(items), 1)
        self.assertIn(items[0]["id"], self.messages[-1][-3]["content"])
        self.assertFalse(self.app.runs.work_once())
        self.assertTrue(self.call("GET", "audit/verify")["valid"])

    def test_multistep_followup_cites_each_receipt_and_uses_published_final_text(self):
        item = self.call("POST", "requirements", {"project_id": "default", "title": "退款申请"})
        run = self.call("POST", "runs", {
            "project_id": "default", "mode": "tool", "allowed_tools": ["search_requirements", "search_reference"],
            "max_tool_steps": 2, "task": {"text": "汇总退款资料"}, "budget": {"currency": "CNY", "limit_micros": 100000},
        })
        path = "runs/" + run["run_id"]
        for tool in ("search_requirements", "search_reference"):
            self.reply = json.dumps({"action": "tool", "tool_id": tool, "arguments": {"query": "退款"}})
            self.assertTrue(self.app.runs.work_once())
            self.decision(self.call("GET", path))
        self.reply = json.dumps({"action": "finish", "answer": "找到需求和操作指引"})
        self.assertTrue(self.app.runs.work_once())
        parent = self.call("GET", path)
        self.assertEqual(parent["continuation_actions"], ["followup"])
        child = self.call("POST", path + "/continuations", {"action": "followup", "text": "列出编号"}, rev=parent["revision"])
        self.assertNotIn("allowed_tools", child)
        self.assertTrue(self.app.runs.work_once())
        child = self.call("GET", "runs/" + child["run_id"])
        self.assertEqual(child["status"], "completed")
        turn = child["conversation"]["turns"][0]
        self.assertEqual(turn["receipt_refs"], [n["artifact"] for n in parent["nodes"] if n["kind"] == "gate"])
        self.assertEqual(turn["answer_ref"], next(n["artifact"] for n in parent["nodes"] if n.get("is_agent_answer")))
        self.assertEqual(len(self.messages[-1]), 5)
        self.assertIn(item["id"], self.messages[-1][1]["content"])
        self.assertIn("payments", self.messages[-1][2]["content"])
        self.assertEqual(self.messages[-1][3], {"role": "assistant", "content": "找到需求和操作指引"})

    def test_reference_binding_survives_followup_and_is_checked_before_later_dispatch(self):
        run = self.propose("search_reference", {"query": "退款"})
        self.decision(run)
        self.assertTrue(self.app.runs.work_once())
        first = self.call("GET", "runs/" + run["run_id"])
        payload = {"action": "followup", "text": "引用来源"}
        child = self.call("POST", "runs/" + first["run_id"] + "/continuations", payload, rev=first["revision"])
        self.app.references["default"]["data_scope"] = "public"
        self.assertTrue(self.app.runs.work_once())
        second = self.call("GET", "runs/" + child["run_id"])
        self.assertEqual(second["task"]["data_scope"], "local_only")
        self.assertIn("payments", self.messages[-1][-3]["content"])
        self.assertIn("业务编号", self.messages[-1][-3]["content"])
        self.assertEqual(second["conversation"]["reference_digest"], self.app.references["default"]["digest"])
        next_path = "runs/" + second["run_id"] + "/continuations"
        queued = self.call("POST", next_path, payload, rev=second["revision"])
        self.app.references.clear()
        second = self.call("GET", "runs/" + second["run_id"])
        with self.assertRaisesRegex(Problem, "授权"):
            self.call("POST", next_path, payload, rev=second["revision"])
        self.assertTrue(self.app.runs.work_once())
        self.assertEqual(self.call("GET", "runs/" + queued["run_id"])["status"], "needs_input")
        self.assertEqual(len(self.messages), 3)

    def test_scoped_search_and_source_policy(self):
        other = self.call("POST", "projects", {"name": "其他项目"})
        for pid, title in [("default", "退款约定"), (other["id"], "退款秘密")]:
            self.call("POST", "requirements", {"project_id": pid, "title": title, "workspace_id":"default"})
        run = self.propose("search_requirements", {"query": "退款"})
        self.decision(run)
        self.assertTrue(self.app.runs.work_once())
        self.assertIn("退款约定", self.messages[-1][-1]["content"])
        self.assertNotIn("退款秘密", self.messages[-1][-1]["content"])
        run = self.propose("search_reference", {"query": "退款"})
        self.assertEqual(run["task"]["data_scope"], "local_only")
        self.decision(run)
        self.assertTrue(self.app.runs.work_once())
        self.assertIn("payments", self.messages[-1][-1]["content"])
        self.assertIn("业务编号", self.messages[-1][-1]["content"])
        run = self.propose("search_reference", {"query": "退款"})
        self.app.references.clear()
        with self.assertRaisesRegex(Problem, "资料源已变更"):
            self.decision(run)
