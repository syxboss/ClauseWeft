"""Explicit team resources for engine/API regressions, separate from installation seeds."""

from clauseweft.api.platform import BOOTSTRAP


def prepare_team(app):
    with app.store.transaction() as db:
        project = db.execute("SELECT 1 FROM platform_resources WHERE kind='projects' AND id='default'").fetchone()
        if not project:
            app.platform.save(db, "projects", {"name": "测试项目", "workspace_id": "default"}, resource_id="default")
        workspace = app.platform.get(db, "workspaces", "default")
        endpoints = sorted(item["endpoint_id"] for item in app.catalog.list())
        repositories = sorted(app.delivery.repositories)
        if endpoints != workspace["endpoint_ids"] or repositories != workspace["repository_ids"]:
            app.platform.save(db, "workspaces", {"endpoint_ids": endpoints, "repository_ids": repositories}, "default", workspace["revision"])
        return app.platform.authorize_project(db, "default", BOOTSTRAP)
