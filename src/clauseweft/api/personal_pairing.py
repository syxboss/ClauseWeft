"""Turn an unused local installation into a named personal workspace on explicit pairing."""

import json
import secrets
import time

from clauseweft import audit
from .accounts import BOOTSTRAP
from .http_contracts import Problem


def _eligible_deployment(db):
    if db.execute("SELECT 1 FROM platform_initialization WHERE name='personal-install-seed-v1'").fetchone():
        return True
    history = db.execute("""SELECT DISTINCT json_extract(payload,'$.after.config') FROM audit_entries
        WHERE action='deployment.started' AND resource='deployment' LIMIT 2""").fetchall()
    if len(history) != 1 or history[0][0] is None:
        return False
    config = json.loads(history[0][0])
    # A pre-pairing installation with an actual configured repository, data
    # source, remote gateway, or test suite remains under the existing owner.
    return (config.get("data_scope", "local_only") == "local_only"
            and not any(config.get(key) for key in ("repositories", "suites", "project_references", "agent_runners"))
            and all(item.get("url") == "http://127.0.0.1:8765" for item in config.get("gateways", {}).values()))


def _unused_installation(db):
    # Compare business state with the one-time installation seed. Opening the app,
    # reading configuration or restarting it does not claim a team installation.
    resources = {tuple(row) for row in db.execute(
        "SELECT kind,id,revision FROM platform_resources"
    )}
    seed = {("workspaces", "default", 1), ("workspaces", "example", 1),
            ("projects", "example-project", 1)}
    if resources != seed or not _eligible_deployment(db):
        return False
    if db.execute("""SELECT 1 WHERE EXISTS (SELECT 1 FROM platform_members)
        OR EXISTS (SELECT 1 FROM endpoint_revisions)
        OR EXISTS (SELECT 1 FROM api_evaluations)
        OR EXISTS (SELECT 1 FROM mw_spec)""").fetchone():
        return False
    examples = db.execute("""SELECT r.run_id,l.project_id,r.body FROM runs r
        LEFT JOIN platform_run_links l USING(run_id) LIMIT 2""").fetchall()
    if len(examples) != 1 or examples[0]["project_id"] != "example-project":
        return False
    run = examples[0]["run_id"]
    if json.loads(examples[0]["body"]).get("is_example") is not True:
        return False
    # Require the original system-created seed and no other business writes.
    # In particular, historical resource saves are evidence even when a legacy
    # installation no longer has the corresponding resource in its live tables.
    seed_actions = {
        ("resource.saved", "workspaces/default"), ("resource.saved", "workspaces/example"),
        ("resource.saved", "projects/example-project"), ("run.run_created", "runs/" + run),
        ("run.example_waiting_model", "runs/" + run), ("workspace.ownership_migrated", "workspaces"),
    }
    writes = db.execute("""SELECT actor_id,action,resource FROM audit_entries
        WHERE outcome='succeeded' AND NOT (action='deployment.started' AND actor_id=?)
        AND NOT (action='api.post' AND resource='auth/logout' AND actor_id=?) LIMIT 7""",
        (audit.SYSTEM["id"], BOOTSTRAP["id"])).fetchall()
    return (len(writes) == len(seed_actions)
            and all(row["actor_id"] == audit.SYSTEM["id"] for row in writes)
            and {(row["action"], row["resource"]) for row in writes} == seed_actions)


def pair(platform, principal):
    """Only the HTTP server's verified bootstrap credential may exchange for a session."""
    if principal["id"] != BOOTSTRAP["id"]:
        raise Problem(403, "PAIRING_TOKEN_REQUIRED", "配对需要本机管理员访问令牌")
    with platform.app.store.transaction() as db:
        existing = db.execute("SELECT owner_id FROM personal_installation WHERE slot=1").fetchone()
        if existing:
            owner = {"id": existing["owner_id"]}
        else:
            if not _unused_installation(db):
                raise Problem(409, "EXISTING_INSTALLATION", "已有成员或业务记录，请选择已有账号或管理员入口。个人配对可使用新的状态目录。")
            # save_revision attributes these explicit bootstrap writes to their
            # caller, even though pairing does not enter the generic API cache.
            platform.app.request_context.principal = principal
            try:
                owner = platform.add_member(db, {
                    "username": "local-owner", "display_name": "本机所有者", "role": "admin",
                    "password": secrets.token_urlsafe(48),
                })
                platform.save(db, "workspaces", {"name": "我的工作空间",
                              "description": "我的项目、任务与执行记录"}, "default", 1)
                platform.save(db, "projects", {"name": "我的项目", "workspace_id": "default"},
                              resource_id="default")
                db.execute("INSERT INTO personal_installation VALUES(1,?)", (owner["id"],))
                audit.record(db, principal, "auth.paired", "members/" + owner["id"],
                             details={"project_id": "default", "workspace_id": "default"})
            finally:
                del platform.app.request_context.principal
        return platform.accounts.issue_session(db, owner, time.time())
