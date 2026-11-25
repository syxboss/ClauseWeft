"""Enforce project ownership, workspace grants and current policies on requests and dispatches."""

from clauseweft import project_tools
from .http_contracts import Problem, require_object
from .platform import BOOTSTRAP, KINDS


def _execution_project(db, body):
    project_id = body.get("project_id")
    if not project_id and body.get("run_id"):
        row = db.execute(
            "SELECT project_id FROM platform_run_links WHERE run_id=?", (body["run_id"],)
        ).fetchone()
        project_id = row[0] if row else None
    return project_id


def current_scope(app, body):
    if not app.execution_authorized(body):
        return "blocked"
    restricted = False
    history_digest = body.get("conversation", {}).get("reference_digest")
    if history_digest or project_tools.uses_reference(body):
        source = app.references.get(body.get("project_id"))
        if not source or source["digest"] != (history_digest or body.get("reference_digest")):
            return "blocked"
        restricted = source["data_scope"] == "local_only"
    skills = list(body.get("skill_snapshots", []))
    for node in body.get("template_snapshot", {}).get("nodes", []):
        skills.extend(node.get("skill_snapshots", []))
    for frozen in skills:
        with app.store.transaction() as db:
            try:
                skill = app.platform.get(db, "skills", frozen["id"])
            except Problem:
                return "blocked"
        if not skill["enabled"]:
            return "blocked"
        if skill["data_scope"] == "local_only":
            restricted = True
    if any(m.get("data_scope") == "local_only" for m in body.get("memory_snapshots", [])):
        restricted = True
    for server_id, expected_digest in body.get("mcp_digests", {}).items():
        srv = app.mcp_servers.get(server_id)
        if not srv or srv["digest"] != expected_digest:
            return "blocked"
        if srv["data_scope"] == "local_only":
            restricted = True
    if body.get("repo_id"):
        repo = app.delivery.repositories.get(body["repo_id"])
        if (
            repo is None
            or (body.get("run_id") or "repo_binding" in body)
            and body.get("repo_binding") != app.repo_binding(repo)
        ):
            return "blocked"
        if repo.get("data_scope", "local_only") == "local_only":
            return "local_only"
    return "local_only" if restricted else app.scope


def execution_authorized(app, body, actor=None):
    """Check current grants for work already queued, as well as new dispatches."""
    with app.store.transaction() as db:
        project_id = _execution_project(db, body)
        # Standalone engine records predate platform ownership. The migration
        # links them before the application worker can dispatch them.
        if not project_id:
            return not body.get("created_by")
        if actor is None:
            actor_id = body.get("created_by", BOOTSTRAP["id"])
            actor = dict(BOOTSTRAP) if actor_id == BOOTSTRAP["id"] else {"id": actor_id}
        try:
            actor = app.platform.public_actor(db, actor)
            if "write" not in actor["capabilities"]:
                return False
            project = app.platform.authorize_project(db, project_id, actor)
            workspace = app.platform.authorize_workspace(db, project["workspace_id"], actor)
            if body.get("repo_id") and body["repo_id"] not in workspace["repository_ids"]:
                return False
        except Problem:
            return False
        return True


def execution_models(app, body):
    """Only models currently assigned to the run's workspace may be selected."""
    with app.store.transaction() as db:
        project_id = _execution_project(db, body)
        if not project_id:
            return app.catalog.list()
        project = app.platform.get(db, "projects", project_id)
        workspace = app.platform.get(db, "workspaces", project["workspace_id"])
        permitted = set(workspace["endpoint_ids"])
        return [model for model in app.catalog.list() if model["endpoint_id"] in permitted]


def authorize(app, db, method, parts, query, body):
    """Apply role and ownership checks before reads, preparation and cache replay."""
    actor = app.platform.public_actor(db, app.actor())
    app.request_context.principal = actor
    app.store.local.audit_actor = actor
    if parts in (("auth", "me"), ("auth", "logout")):
        return
    if parts[:2] == ("me", "preferences"):
        if len(parts) != 3 or query or method not in ("GET", "PATCH"):
            raise Problem(404, "NOT_FOUND", "个人偏好入口无效")
        if actor["id"] == BOOTSTRAP["id"]:
            raise Problem(403, "NAMED_ACCOUNT_REQUIRED", "请使用具名账号管理个人偏好")
        app.platform.authorize_workspace(db, parts[2], actor)
        if method == "PATCH":
            require_object(body, ("instructions",), ("instructions",))
        return
    if not parts:
        raise Problem(404, "NOT_FOUND", "资源不存在")
    category = parts[0]
    if category in {"members", "audit", "settings", "endpoints", "evaluations"}:
        if "admin" not in actor["capabilities"]:
            raise Problem(403, "ADMIN_REQUIRED", "此功能需要平台管理员权限")
        return
    if category == "usage" and "usage" not in actor["capabilities"]:
        raise Problem(403, "USAGE_FORBIDDEN", "当前角色不能查询模型用量与费用")
    if actor["role"] == "viewer":
        if method != "GET":
            raise Problem(403, "READ_ONLY", "只读成员不能修改资源或发起执行")
        if category in {"roles", "templates", "skills"}:
            raise Problem(403, "FORBIDDEN", "当前角色不能访问编排配置")
    if category == "workspaces":
        if method != "GET" and "admin" not in actor["capabilities"]:
            raise Problem(403, "ADMIN_REQUIRED", "工作空间管理需要平台管理员权限")
        if len(parts) > 2 and "admin" not in actor["capabilities"]:
            raise Problem(403, "ADMIN_REQUIRED", "工作空间配置历史需要平台管理员权限")
        if len(parts) > 1:
            app.platform.authorize_workspace(db, parts[1], actor)
        return
    workspace_id = query.get("workspace_id") or body.get("workspace_id")
    if workspace_id is not None:
        app.platform.authorize_workspace(db, workspace_id, actor)
    if query.get("project_id"):
        project = app.platform.authorize_project(db, query["project_id"], actor)
        if workspace_id and workspace_id != project["workspace_id"]:
            raise Problem(403, "WORKSPACE_FORBIDDEN", "项目不属于所选工作空间")
    if query.get("requirement_id"):
        app.platform.authorize_resource(db, "requirements", query["requirement_id"], actor)
    if method != "GET" and category in {"runs", "routing"} and len(parts) <= 2:
        project = app.platform.authorize_project(db, body.get("project_id", "default"), actor)
        if workspace_id and project["workspace_id"] != workspace_id:
            raise Problem(403, "WORKSPACE_FORBIDDEN", "项目不属于所选工作空间")
        for field, kind in (("requirement_id", "requirements"), ("template_id", "templates"), ("role_id", "roles")):
            if body.get(field):
                if not isinstance(body[field], str):
                    raise ValueError(f"{field} 须为文本")
                target = app.platform.authorize_resource(db, kind, body[field], actor)
                if target["workspace_id"] != project["workspace_id"]:
                    raise Problem(403, "WORKSPACE_FORBIDDEN", "引用不属于项目工作空间")
        for skill_id in body.get("skill_ids", []) if isinstance(body.get("skill_ids", []), list) else []:
            if not isinstance(skill_id, str):
                raise ValueError("skill_ids 须为技能 ID 列表")
            skill = app.platform.authorize_resource(db, "skills", skill_id, actor)
            if skill["workspace_id"] != project["workspace_id"]:
                raise Problem(403, "WORKSPACE_FORBIDDEN", "技能不属于项目工作空间")
    if category in KINDS and len(parts) > 1:
        resource = app.platform.authorize_resource(db, category, parts[1], actor)
        if workspace_id and resource.get("workspace_id") != workspace_id:
            raise Problem(403, "WORKSPACE_FORBIDDEN", "资源不属于所选工作空间")
    if category == "runs" and len(parts) > 1:
        row = db.execute(
            "SELECT project_id FROM platform_run_links WHERE run_id=?", (parts[1],)
        ).fetchone()
        if row is None:
            raise Problem(404, "RUN_NOT_FOUND", "运行不存在")
        project = app.platform.authorize_project(db, row[0], actor)
        if workspace_id and project["workspace_id"] != workspace_id:
            raise Problem(403, "WORKSPACE_FORBIDDEN", "运行不属于所选工作空间")
    if method != "GET" and category in KINDS:
        if body.get("project_id"):
            app.platform.authorize_project(db, body["project_id"], actor)
        if len(parts) == 1:
            app.platform.authorize_workspace(db, workspace_id or "default", actor)
    if category in {"overview", "workspace", "runs", "projects", "requirements", "roles", "templates", "skills", "usage"} and len(parts) == 1:
        app.platform.authorize_workspace(db, workspace_id or "default", actor)
