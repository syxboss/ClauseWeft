"""Account- and workspace-scoped memory enters a model request only by opt-in."""

import json
from unittest import TestCase
from uuid import uuid4

from clauseweft.api.application import Application
from clauseweft.api.server import Problem
import test_simple_tasks


class PersonalPreferencesTest(TestCase):
    def setUp(self):
        fixture = test_simple_tasks.SimpleTasksTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.fixture = fixture
        self.app = fixture.app
        with self.app.store.transaction() as db:
            self.alice, self.bob = [
                self.app.platform.add_member(db, {
                    "username": name, "password": "sample-password-123",
                    "role": "operator" if name == "alice" else "viewer",
                    "workspace_ids": ["default", "example"] if name == "alice" else ["default"],
                    "project_ids": ["default"],
                }) for name in ("alice", "bob")
            ]
        self.path = ("me", "preferences", "default")

    def request(self, actor, method, path, body=None, *, revision=None, key=None):
        return self.app.handle(method, path, {}, body or {}, key or uuid4().hex,
                               revision, principal=actor)[1]

    def test_persistence_isolation_clear_and_audit(self):
        write = {"instructions": str(uuid4())}
        saved = self.request(self.alice, "PATCH", self.path, write, revision=0, key="save")
        self.assertEqual(saved, {"revision": 1})
        self.assertEqual(self.request(self.alice, "PATCH", self.path, write, revision=0, key="save"), saved)
        self.app = Application(self.fixture.root / "state", self.fixture.root / "config.json")
        self.assertEqual(self.request(self.alice, "GET", self.path)["instructions"], write["instructions"])
        self.assertEqual(self.request(self.alice, "GET", ("me", "preferences", "example"))["instructions"], "")
        self.assertEqual(self.request(self.bob, "GET", self.path)["instructions"], "")
        self.request(self.bob, "PATCH", self.path, {"instructions": "other member"}, revision=0, key="save")
        with self.assertRaises(Problem):
            self.request(self.bob, "GET", ("me", "preferences", "example"))
        with self.assertRaises(Problem):
            self.request(None, "GET", self.path)
        with self.assertRaises(Problem) as stale:
            self.request(self.alice, "PATCH", self.path, {"instructions": ""}, revision=0)
        self.assertEqual(stale.exception.status, 412)
        self.request(self.alice, "PATCH", self.path, {"instructions": ""}, revision=1)
        self.assertEqual(self.request(self.alice, "GET", self.path), {"instructions": "", "revision": 2})
        self.assertEqual(self.request(self.bob, "GET", self.path)["instructions"], "other member")
        proof = self.request(None, "GET", ("audit", "exports"))
        self.assertFalse(write["instructions"] in json.dumps(proof, ensure_ascii=False))

    def test_opt_in_sends_frozen_preference_in_its_own_message(self):
        task = {"project_id": "default", "task": {"text": "整理变更说明"}}
        self.request(self.alice, "PATCH", self.path, {"instructions": "请给中英双语摘要"}, revision=0)
        self.request(self.alice, "POST", ("runs",), task)
        opted = self.request(self.alice, "POST", ("runs",), dict(task, use_preferences=True))
        with self.assertRaises(ValueError):
            self.request(self.alice, "POST", ("runs",), dict(task, mode="complex", use_preferences=True))
        self.request(self.alice, "PATCH", self.path, {"instructions": ""}, revision=1)
        with self.assertRaises(ValueError):
            self.request(self.alice, "POST", ("runs",), dict(task, use_preferences=True))
        self.assertTrue(self.app.runs.work_once())
        self.assertTrue(self.app.runs.work_once())
        preference = {"role": "user", "content": "本次使用的个人偏好\n请给中英双语摘要"}
        messages = [call[1] for call in self.fixture.calls]
        self.assertEqual(sum(preference in batch for batch in messages), 1)
        context = self.request(self.alice, "GET", ("runs", opted["run_id"], "context"))
        self.assertIn(preference, context["items"][0]["messages"])
        self.assertEqual(context["items"][0]["messages"][-1], {"role": "user", "content": task["task"]["text"]})
