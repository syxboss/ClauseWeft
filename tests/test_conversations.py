"""Exercise project-owned conversation branches via the normal task API."""

import unittest
from uuid import uuid4

from clauseweft import audit
from clauseweft.api.conversations import MAX_HISTORY_BYTES, MAX_HISTORY_TURNS, append_turn
from clauseweft.api.server import Problem
import test_simple_tasks


class ConversationTest(unittest.TestCase):
    def setUp(self):
        fixture = test_simple_tasks.SimpleTasksTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.app = fixture.app
        self.calls = fixture.calls

    def api(self, method, path, body=None, revision=None, key=None, actor=None):
        return self.app.handle(method, tuple(path.split("/")), {}, body or {}, key or uuid4().hex,
                               revision, actor)[1]

    def source(self, **fields):
        run = self.api("POST", "runs", dict(project_id="default", **fields))
        self.assertTrue(self.app.runs.work_once())
        return self.api("GET", "runs/" + run["run_id"])

    def branch(self, parent, body, **kwargs):
        return self.api("POST", f"runs/{parent['run_id']}/continuations", body,
                        parent["revision"], **kwargs)

    def finish(self, run):
        self.assertTrue(self.app.runs.work_once())
        return self.api("GET", "runs/" + run["run_id"])

    def test_followup_uses_server_results_materials_and_new_budget(self):
        task = {"text": "解释材料", "materials": [{"name": "notes.txt", "text": "独立文本"}]}
        first = self.source(task=task)
        body = {"action": "followup", "text": "再简短一些"}
        child = self.branch(first, body, key="second-turn")
        self.assertEqual(self.branch(first, body, key="second-turn"), child)
        self.assertNotEqual(first["run_id"], child["run_id"])
        self.assertEqual(child["source_run_id"], first["run_id"])
        self.assertEqual(child["budget"]["settled_micros"], 0)
        second = self.finish(child)
        self.assertEqual(second["status"], "completed")
        expected = [
            {"role": "user", "content": "解释材料"}, {"role": "user", "content": "参考材料：notes.txt\n独立文本"},
            {"role": "assistant", "content": "整理后的内容与实施建议"}, {"role": "user", "content": "再简短一些"},
        ]
        self.assertEqual(self.calls[-1][1], expected)
        self.assertEqual(second["conversation"]["turns"][0]["answer_ref"], first["nodes"][0]["artifact"])
        self.assertGreater(second["budget"]["limit_micros"], first["budget"]["limit_micros"])
        third = self.finish(self.branch(second, {"action": "followup", "text": "继续"}))
        self.assertEqual(len(third["conversation"]["turns"]), 2)
        self.assertEqual(self.calls[-1][1][-3:], expected[-1:] + [
            {"role": "assistant", "content": "整理后的内容与实施建议"}, {"role": "user", "content": "继续"}])
        regen = self.finish(self.branch(third, {"action": "regenerate"}))
        self.assertEqual(regen["task"], third["task"])
        self.assertEqual(self.calls[-1][1], self.calls[-2][1])
        self.assertEqual(len(self.calls), 4)
        self.assertEqual(self.api("GET", "runs/" + first["run_id"])["nodes"][0]["artifact"], first["nodes"][0]["artifact"])
        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])

    def test_regenerate_preserves_zero_cap_and_rejects_forged_fields(self):
        first = self.source(task={"text": "受限操作"}, budget={"currency": "CNY", "limit_micros": 0})
        self.assertEqual(first["continuation_actions"], ["regenerate"])
        for extra in ({"project_id": "other"}, {"turns": []}, {"task": {"text": "覆盖"}}):
            with self.assertRaises(ValueError):
                self.branch(first, dict(action="regenerate", **extra))
        with self.assertRaises(Problem) as stale:
            self.branch(dict(first, revision=first["revision"] - 1), {"action": "regenerate"})
        self.assertEqual(stale.exception.status, 412)
        regenerated = self.finish(self.branch(first, {"action": "regenerate"}))
        self.assertEqual(regenerated["budget"]["limit_micros"], 0)
        self.assertFalse(self.calls)

    def test_branches_keep_requirement_links_without_changing_status_or_revision(self):
        item = self.api("POST", "requirements", {"project_id": "default", "title": "交付清单"})
        path = "requirements/" + item["id"]
        first = self.source(task={"text": "梳理清单"}, requirement_id=item["id"])
        active = self.api("GET", path)
        self.assertEqual(active["status"], "in_progress")
        for status in ("done", "review", "ready", "backlog", "in_progress"):
            with self.subTest(status=status):
                active = self.api("PATCH", path, {"status": status}, active["revision"])
                for payload in ({"action": "followup", "text": "补充说明"}, {"action": "regenerate"}):
                    first = self.api("GET", "runs/" + first["run_id"])
                    child = self.finish(self.branch(first, payload))
                    self.assertEqual((child["project_id"], child["requirement_id"]), ("default", item["id"]))
                    linked = self.api("GET", path)
                    self.assertEqual((linked["status"], linked["revision"]), (status, active["revision"]))
                    self.assertEqual(linked["latest_run_id"], child["run_id"])
        self.assertEqual(self.api("GET", path)["run_count"], 11)
        self.assertTrue(self.api("GET", "audit/verify")["valid"])

    def test_completed_complex_answer_becomes_a_single_continuation(self):
        replies = iter(['{"subtasks":["a","b"]}', "A analysis", "B analysis", "both results", "next"])
        outgoing = []

        def generate(endpoint, messages, limit):
            outgoing.append(messages)
            return next(replies), {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, uuid4().hex

        self.app.catalog.generate = generate
        source = self.source(mode="complex", task={"text": "organize"}, budget={"currency": "CNY", "limit_micros": 90000})
        with self.assertRaises(Problem):
            self.branch(source, {"action": "followup", "text": "more"})
        for _ in range(3):
            self.assertTrue(self.app.runs.work_once())
        parent = self.api("GET", "runs/" + source["run_id"])
        child = self.finish(self.branch(parent, {"action": "followup", "text": "more"}))
        self.assertEqual(child["mode"], "single")
        self.assertEqual([m["content"] for m in outgoing[-1]], ["organize", "both results", "more"])

    def test_revocation_blocks_cache_replay_and_queued_dispatch(self):
        first = self.source(task={"text": "project secret token=should-be-redacted"})
        with self.app.store.transaction() as db:
            member = self.app.platform.add_member(db, {
                "username": "member", "display_name": "Member", "role": "operator", "password": "safe-local-password",
                "workspace_ids": ["default"], "project_ids": ["default"],
            })
        payload = {"action": "followup", "text": "continue"}
        branch = self.branch(first, payload, key="member-turn", actor=member)
        with self.app.store.transaction() as db:
            stored = db.execute("SELECT body FROM runs WHERE run_id=?", (branch["run_id"],)).fetchone()[0]
        self.assertNotIn("should-be-redacted", stored)
        self.assertIn("[redacted]", stored)
        with self.app.store.transaction() as db:
            self.app.platform.update_member(db, member["id"], {"project_ids": []}, member["revision"])
        with self.assertRaises(Problem) as refused:
            self.branch(first, payload, key="member-turn", actor=member)
        self.assertIn(refused.exception.status, (403, 404))
        self.assertEqual(self.finish(branch)["status"], "needs_input")
        self.assertEqual(len(self.calls), 1)

    def test_historical_local_scope_survives_relaxation_and_a_changed_role(self):
        self.app.scope = "public"
        role = self.api("POST", "roles", {"name": "编辑", "stage": "general", "prompt": "最初的指令"})
        queued = self.api("POST", "runs", {"project_id": "default", "task": {"text": "编辑", "data_scope": "public"},
                                          "role_id": role["id"]})
        self.app.scope = "local_only"
        first = self.finish(queued)
        self.app.scope = "public"
        self.api("PATCH", "roles/" + role["id"], {"prompt": "新的指令"}, role["revision"])
        second = self.finish(self.branch(first, {"action": "followup", "text": "追问"}))
        self.assertEqual(second["task"]["data_scope"], "local_only")
        self.assertEqual(self.calls[-1][1][0], {"role": "system", "content": "最初的指令"})

    def test_history_is_bounded_by_utf8_bytes_and_complete_turns(self):
        context = {}
        for i in range(12):
            turns, omitted = append_turn(context, {"run_id": str(i)}, {"text": "问"}, "答", str(i))
            context = dict(turns=turns, omitted_turns=omitted)
        self.assertEqual((len(turns), omitted), (MAX_HISTORY_TURNS, 4))
        turns, omitted = append_turn(context, {"run_id": "long"}, {"text": "问题" * 16000},
                                     "回应" * 16000, "sha")
        self.assertEqual(len(turns), 1)
        self.assertEqual(omitted, 12)
        self.assertTrue(turns[0]["excerpted"])
        self.assertLessEqual(sum(len(m["content"].encode()) for m in turns[0]["messages"]), MAX_HISTORY_BYTES)
