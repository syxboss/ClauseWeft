"""History queries filter authorized titles and original goals before paging."""

from unittest import TestCase

import test_simple_tasks


class RunSearchTest(TestCase):
    def test_goal_or_title_before_paging_with_project_boundaries(self):
        fixture = test_simple_tasks.SimpleTasksTest()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        app = fixture.app
        with app.store.transaction() as db:
            other = app.platform.save(db, "projects", {"name": "其他团队项目"})
            member = app.platform.add_member(db, {
                "username": "history-member", "role": "operator", "password": "fixture-password-123",
                "workspace_ids": ["default"], "project_ids": ["default"],
            })
        def make(title, goal, project="default"):
            return fixture.call("POST", "runs", {"project_id": project, "title": title,
                                                  "task": {"text": goal}})["run_id"]

        def search(text, **query):
            return app.handle("GET", ("runs",), dict(q=text, **query), {}, principal=member)[1]

        app.handle("PATCH", ("me", "preferences", "default"), {}, {"instructions": "私密标记"},
                   "memory", 0, principal=member)
        first = app.handle("POST", ("runs",), {}, {
            "project_id": "default", "title": "旧任务", "use_preferences": True,
            "task": {"text": "审核 API_% 对照", "materials": [{"name": "附件标记", "text": "仅在附件中的文字"}]},
        }, "attached", principal=member)[1]["run_id"]
        second = make("新 API_% 简报", "列出差异")
        make("无关任务", "API-计划")
        make("其他项目 API_%", "API_%", other["id"])
        found = search(" api_% ", limit=1)
        self.assertEqual([row["run_id"] for row in found["items"]], [second])
        more = search(" api_% ", cursor=found["next_cursor"], limit=1)
        self.assertEqual([row["run_id"] for row in more["items"]], [first])
        self.assertIsNone(more["next_cursor"])
        for term in ("私密标记", "附件标记", "仅在附件中的文字"):
            self.assertEqual(search(term)["items"], [])
        self.assertEqual(search("api", status="completed")["items"], [])
