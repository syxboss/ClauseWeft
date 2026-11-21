"""Create platform tables and migrate existing workspaces, permissions and run ownership."""

import json

from clauseweft import audit
from .accounts import BOOTSTRAP


def init_schema(platform, db):
    audit.init_schema(db)
    db.execute("""CREATE TABLE IF NOT EXISTS platform_resources (
        kind TEXT, id TEXT, revision INTEGER NOT NULL, payload TEXT NOT NULL,
        created REAL NOT NULL, updated REAL NOT NULL, PRIMARY KEY(kind,id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS platform_releases (
        template_id TEXT, revision INTEGER, snapshot TEXT NOT NULL,
        published REAL NOT NULL, actor TEXT NOT NULL, PRIMARY KEY(template_id,revision))""")
    db.execute("""CREATE TABLE IF NOT EXISTS platform_members (
        id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE, display_name TEXT NOT NULL,
        role TEXT NOT NULL, password_hash TEXT NOT NULL, salt TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1, active INTEGER NOT NULL DEFAULT 1, created REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS personal_installation (
        slot INTEGER PRIMARY KEY CHECK(slot=1), owner_id TEXT NOT NULL UNIQUE REFERENCES platform_members(id))""")
    db.execute("""CREATE TABLE IF NOT EXISTS platform_sessions (
        token_hash TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES platform_members(id),
        created REAL NOT NULL, expires REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS platform_login_failures (
        username TEXT PRIMARY KEY, attempts INTEGER NOT NULL, until REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS platform_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL, actor_id TEXT NOT NULL,
        actor TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL, details TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS platform_run_links (
        run_id TEXT PRIMARY KEY REFERENCES runs(run_id), project_id TEXT NOT NULL,
        requirement_id TEXT, title TEXT NOT NULL, template_id TEXT, template_revision INTEGER,
        created_by TEXT NOT NULL)""")
    for column in ("workspace_ids", "project_ids"):
        if column not in {
            row["name"] for row in db.execute("PRAGMA table_info(platform_members)")
        }:
            db.execute(
                f"ALTER TABLE platform_members ADD COLUMN {column} TEXT NOT NULL DEFAULT '[]'"
            )
    db.execute(
        "CREATE TABLE IF NOT EXISTS platform_initialization(name TEXT PRIMARY KEY)"
    )
    # Old state directories have no marker. Pairing permits their unmodified
    # local seed only, and preserves any registered team infrastructure.
    if not db.execute("SELECT 1 FROM platform_initialization LIMIT 1").fetchone() and not db.execute(
        "SELECT 1 FROM platform_resources UNION ALL SELECT 1 FROM runs LIMIT 1"
    ).fetchone():
        db.execute("INSERT INTO platform_initialization VALUES('personal-install-seed-v1')")
    audit.migrate_legacy(db)
    _initialize_workspaces(platform, db)
    _adopt_unlinked_runs(platform, db)


def _initialize_workspaces(platform, db):
    """One-time ownership migration and a single isolated, honest example."""
    if db.execute(
        "SELECT 1 FROM platform_initialization WHERE name='workspace-ownership-v1'"
    ).fetchone():
        return
    if not db.execute(
        "SELECT 1 FROM platform_resources WHERE kind='workspaces' AND id='default'"
    ).fetchone():
        platform.save(
            db,
            "workspaces",
            {
                "name": "团队工作空间",
                "description": "团队的实际项目与研发记录",
                "repository_ids": sorted(platform.app.delivery.repositories),
                "endpoint_ids": sorted(
                    item["endpoint_id"] for item in platform.app.catalog.list()
                ),
            },
            resource_id="default",
        )
    if not db.execute(
        "SELECT 1 FROM platform_resources WHERE kind='workspaces' AND id='example'"
    ).fetchone():
        platform.save(
            db,
            "workspaces",
            {
                "name": "示例工作空间",
                "description": "独立演示区，包含一个示例项目，不会产生模型调用或费用",
                "is_example": True,
                "repository_ids": [],
                "endpoint_ids": [],
            },
            resource_id="example",
        )
    for kind in ("projects", "roles", "skills", "templates", "requirements"):
        for row in list(
            db.execute(
                "SELECT id,revision,payload FROM platform_resources WHERE kind=?",
                (kind,),
            )
        ):
            payload = json.loads(row["payload"])
            if "workspace_id" not in payload:
                workspace_id = "default"
                if kind == "requirements":
                    workspace_id = platform.get(
                        db, "projects", payload.get("project_id", "default")
                    )["workspace_id"]
                platform.save(
                    db,
                    kind,
                    {"workspace_id": workspace_id},
                    row["id"],
                    row["revision"],
                )
    # Existing accounts receive no implicit grants. An administrator assigns
    # explicit workspaces/projects before those accounts may read data.
    for row in list(db.execute("SELECT id,revision FROM platform_members")):
        platform.update_member(db, row["id"], {}, row["revision"])
    _seed_example(platform, db)
    audit.record(
        db,
        audit.SYSTEM,
        "workspace.ownership_migrated",
        "workspaces",
        details={
            "legacy_workspace_id": "default",
            "member_policy": "explicit_grants",
            "example_workspace_id": "example",
        },
    )
    db.execute(
        "INSERT INTO platform_initialization VALUES('workspace-ownership-v1')"
    )


def _adopt_unlinked_runs(platform, db):
    """Recover standalone/probe runs without creating duplicate demo content."""
    unlinked = list(
        db.execute(
            "SELECT r.run_id,r.body FROM runs r LEFT JOIN platform_run_links l USING(run_id) WHERE l.run_id IS NULL ORDER BY r.created,r.run_id"
        )
    )
    if not unlinked:
        return
    if not db.execute(
        "SELECT 1 FROM platform_resources WHERE kind='projects' AND id='default'"
    ).fetchone():
        platform.save(
            db,
            "projects",
            {
                "name": "历史项目",
                "description": "未归属项目的历史或系统运行记录",
                "workspace_id": "default",
            },
            resource_id="default",
        )
    for row in unlinked:
        body = json.loads(row["body"])
        db.execute(
            "INSERT INTO platform_run_links VALUES(?,?,?,?,?,?,?)",
            (
                row["run_id"],
                "default",
                None,
                body.get("task", {}).get("text", "系统运行")[:160],
                None,
                None,
                body.get("created_by", BOOTSTRAP["id"]),
            ),
        )
        audit.record(
            db,
            audit.SYSTEM,
            "workspace.run_adopted",
            "runs/" + row["run_id"],
            project_id="default",
            run_id=row["run_id"],
            details={"workspace_id": "default", "reason": "missing_project_link"},
        )


def _seed_example(platform, db):
    project = platform.save(
        db,
        "projects",
        {
            "name": "示例项目 · 我的第一个任务",
            "description": "了解如何提交任务和查看结果。请先接入自己的模型，示例不会生成虚构回复或费用。",
            "workspace_id": "example",
            "is_example": True,
        },
        resource_id="example-project",
    )
    plan = {
        "mode": "single",
        "task": {"text": "请整理这个项目的目标，并给出一份清晰的实施建议。", "stage": "general"},
        "title": "示例任务 · 提交内容并查看结果",
        "project_id": project["id"],
        "workspace_id": "example",
        "is_example": True,
        "budget": {"currency": "USD", "limit_micros": 0},
        "budget_source": "no_configured_model",
        "created_by": audit.SYSTEM["id"],
    }
    run = platform.app.runs.start(plan)
    platform.link_run(db, run["run_id"], plan, audit.SYSTEM)
    db.execute("UPDATE nodes SET state='needs_input',reason='model_configuration_required' WHERE run_id=?", (run["run_id"],))
    platform.app.store.event(db, run["run_id"], "example_waiting_model", {"reason": "model_configuration_required", "example": True})
    platform.app.runs._refresh(db, run["run_id"])
