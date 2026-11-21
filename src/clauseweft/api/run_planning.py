"""Prepare authorized task snapshots and budgets before starting a run."""

import re

from clauseweft import project_tools
from . import personal_preferences
from .http_contracts import Problem, encode, require_object


def _request_fields(data):
    require_object(
        data,
        (
            "mode",
            "task",
            "repo_id",
            "base_sha",
            "plan_template_id",
            "budget",
            "suite_id",
            "max_concurrency",
            "tool_id",
            "allowed_tools",
            "max_tool_steps",
            "requirement_query",
            "tool_approval_policy",
            "max_fix_rounds",
            "pinned_endpoints",
            "title",
            "project_id",
            "requirement_id",
            "template_id",
            "role_id",
            "skill_ids",
            "use_preferences",
            "workspace_id",
        ),
        ("task",),
    )
    data = dict(data)
    if data.get("mode") == "tool":
        project_tools.validate_plan(data)
        if set(data).intersection({"repo_id", "base_sha", "suite_id", "template_id", "plan_template_id", "role_id", "skill_ids", "pinned_endpoints", "max_fix_rounds"}):
            raise ValueError("项目工具只使用所选工具与本次任务配置")
    elif {"tool_id", "allowed_tools", "max_tool_steps", "requirement_query", "tool_approval_policy"}.intersection(data):
        raise ValueError("工具授权与工具执行上限仅用于工具任务")
    if "max_fix_rounds" in data:
        rounds = data["max_fix_rounds"]
        if type(rounds) is not int or not 0 <= rounds <= 3:
            raise ValueError("自动修复轮次上限须为 0 至 3 的整数")
        if data.get("mode", "single") != "workflow" and not data.get("template_id"):
            raise ValueError("max_fix_rounds 仅用于代码交付流程")
    use_preferences = data.pop("use_preferences", False)
    if type(use_preferences) is not bool:
        raise ValueError("use_preferences 须为布尔值")
    if use_preferences and (data.get("mode", "single") != "single" or data.get("template_id")):
        raise ValueError("个人偏好仅用于单任务或追问")
    task = dict(require_object(
        data["task"],
        ("text", "materials", "stage", "preference", "pinned_endpoint", "data_scope",
         "max_output_tokens", "required_capabilities"),
        ("text",),
    ))
    if "role_id" in data and (
        not isinstance(data["role_id"], str) or not data["role_id"].strip()
        or data.get("mode", "single") != "single" or data.get("template_id")
    ):
        raise ValueError("role_id 仅用于将普通任务交给指定角色")
    if data.get("mode") == "complex" and any(
        field in data for field in ("template_id", "plan_template_id", "repo_id", "base_sha", "suite_id", "pinned_endpoints")
    ):
        raise ValueError("复杂任务自动生成执行图；固定模型请使用 task.pinned_endpoint")
    for field in ("requirement_id", "template_id"):
        if field in data and not isinstance(data[field], str):
            raise ValueError("需求和模板 ID 须为文本")
    if "title" in data and (
        not isinstance(data["title"], str)
        or not data["title"].strip()
        or len(data["title"]) > 160
    ):
        raise ValueError("运行标题须为 1 到 160 个字符")
    return data, task, use_preferences


def _freeze_project_inputs(app, db, data, task, use_preferences):
    project_id = data.get("project_id", "default")
    if not isinstance(project_id, str):
        raise ValueError("项目 ID 无效")
    project = app.platform.authorize_project(db, project_id)
    workspace_id = data.get("workspace_id", project["workspace_id"])
    if workspace_id != project["workspace_id"]:
        raise Problem(403, "WORKSPACE_FORBIDDEN", "项目不属于所选工作空间")
    workspace = app.platform.authorize_workspace(db, workspace_id)
    data["workspace_id"] = workspace_id
    data["project_id"] = project_id
    if project_tools.uses_reference(data):
        source = app.references.get(project_id)
        if source is None:
            raise ValueError("当前项目尚未登记知识资料源")
        data["reference_digest"] = source["digest"]
        if source["data_scope"] == "local_only":
            task["data_scope"] = "local_only"
    if use_preferences:
        snapshot = personal_preferences.read(db, app.actor(), workspace_id)
        if not snapshot["instructions"]:
            raise ValueError("当前工作空间尚未保存个人偏好")
        data["personal_preferences"] = snapshot
    if data.get("requirement_id"):
        requirement = app.platform.get(
            db, "requirements", data["requirement_id"]
        )
        app.platform.authorize_resource(db, "requirements", data["requirement_id"])
        if requirement["project_id"] != project_id:
            raise ValueError("需求与项目不匹配")
    if data.get("template_id"):
        template = app.platform.authorize_resource(db, "templates", data["template_id"])
        if template["workspace_id"] != workspace_id:
            raise Problem(403, "WORKSPACE_FORBIDDEN", "SOP 不属于当前工作空间")
        release = app.platform.release(db, data["template_id"])
        data["template_snapshot"] = release
        data["mode"] = release["execution_mode"]
        if "max_fix_rounds" in data and data["mode"] != "workflow":
            raise ValueError("max_fix_rounds 仅用于代码交付流程")
        if data["mode"] == "workflow" and project["repository_id"]:
            data.setdefault("repo_id", project["repository_id"])
    if data.get("role_id"):
        role = app.platform.freeze_role(db, data["role_id"], workspace_id)
        data["role_snapshot"] = role
        task["stage"] = role["stage"]
        if role["endpoint_id"]:
            overrides = data.get("pinned_endpoints", {})
            if (not isinstance(overrides, dict)
                    or overrides.get("task", role["endpoint_id"]) != role["endpoint_id"]
                    or task.get("pinned_endpoint", role["endpoint_id"]) != role["endpoint_id"]):
                raise ValueError("指定模型与角色绑定不一致")
            task["pinned_endpoint"] = role["endpoint_id"]
    if "skill_ids" in data or "role_snapshot" in data:
        if data.get("template_id"):
            raise ValueError("SOP 的技能请配置在角色中并发布模板")
        data["skill_snapshots"] = app.platform.freeze_skills(
            db, data.get("skill_ids", []), workspace_id=workspace_id,
            inherited=data.get("role_snapshot", {}).get("skill_ids", []),
        )
    if data.get("repo_id"):
        if data["repo_id"] not in workspace["repository_ids"]:
            raise Problem(403, "REPOSITORY_FORBIDDEN", "仓库未授权给当前工作空间")
        if project["repository_id"] and data["repo_id"] != project["repository_id"]:
            raise Problem(403, "REPOSITORY_FORBIDDEN", "仓库与项目绑定不一致")


def plan(app, data, *, inherited=None):
    data, task, use_preferences = _request_fields(data)
    with app.store.transaction() as db:
        _freeze_project_inputs(app, db, data, task, use_preferences)
    if not isinstance(task["text"], str) or not task["text"].strip():
        raise ValueError("任务文本不能为空")
    if any(
        not isinstance(data[k], str) or not data[k].strip()
        for k in ("repo_id", "suite_id", "base_sha")
        if k in data
    ):
        raise ValueError("仓库、套件与提交须为非空标识")
    task.setdefault("data_scope", app.scope)
    if app.scope == "local_only" and task["data_scope"] != "local_only":
        raise Problem(403, "DATA_SCOPE", "本工作区只允许本地处理")
    mode = data.get("mode", "single")
    if mode in ("single", "complex", "tool"):
        task.setdefault("stage", "general")
    if inherited:
        data.update(inherited)
    budget, budget_source = app.task_budget(data, task, mode)
    require_object(
        budget, ("currency", "limit_micros"), ("currency", "limit_micros")
    )
    if (
        type(budget["limit_micros"]) is not int
        or not 0 <= budget["limit_micros"] <= 2**60
    ):
        raise ValueError("预算上限无效")
    if not isinstance(budget["currency"], str) or not re.fullmatch(
        "[A-Z]{3}", budget["currency"]
    ):
        raise ValueError("预算币种无效")
    if (
        mode not in ("single", "complex", "workflow", "sop", "tool")
        or data.get("plan_template_id", "delivery-v1") != "delivery-v1"
    ):
        raise ValueError("运行模式或模板无效")
    return dict(data, mode=mode, task=task, budget=budget, budget_source=budget_source)


def task_budget(app, data, task, mode):
    """Infer a finite single-call ceiling from configured prices, never a fixed spend."""
    if "budget" in data:
        return data["budget"], "explicit"
    if mode == "complex":
        raise ValueError("复杂任务需要显式填写总预算和币种")
    if mode == "tool":
        raise ValueError("工具任务需要覆盖全部决策与答复的总预算")
    if mode != "single":
        return {"currency": "USD", "limit_micros": 0}, "explicit_required_for_paid_workflow"
    models = app.execution_models(data)
    pin = task.get("pinned_endpoint")
    if pin:
        selected = [model for model in models if model["endpoint_id"] == pin]
        if not selected:
            raise Problem(403, "MODEL_FORBIDDEN", "所选模型未授权给当前工作空间")
        models = selected
    if not models:
        return {"currency": "USD", "limit_micros": 0}, "no_configured_model"
    currencies = sorted({model["currency"] for model in models})
    estimates = []
    for currency in currencies:
        prepared = dict(data, mode=mode, task=task,
                        budget={"currency": currency, "limit_micros": 2**60})
        decision = app.runs.preview(prepared)
        eligible = [item for item in decision.get("candidates", []) if not item["reasons"]]
        if eligible:
            estimates.append({"currency": currency, "limit_micros": max(item["estimated_micros"] for item in eligible)})
    if len(estimates) > 1:
        raise ValueError("已配置模型使用不同币种，请选择一个模型或在高级选项中指定币种和预算")
    if estimates:
        return estimates[0], "model_estimate"
    # Preserve the engine's precise blocked/needs_input result without inventing
    # an unbounded allowance or a configured model's price.
    return {"currency": currencies[0], "limit_micros": 0}, "model_not_eligible"


def start(app, db, plan, context):
    project = app.platform.authorize_project(db, plan.get("project_id", "default"))
    if plan.get("workspace_id", project["workspace_id"]) != project["workspace_id"]:
        raise Problem(403, "WORKSPACE_FORBIDDEN", "运行与项目工作空间不匹配")
    if plan.get("template_snapshot"):
        source_template = app.platform.authorize_resource(db, "templates", plan["template_snapshot"]["id"])
        if source_template["workspace_id"] != project["workspace_id"]:
            raise Problem(403, "WORKSPACE_FORBIDDEN", "模板与项目工作空间不匹配")
    plan = dict(plan, workspace_id=project["workspace_id"])
    if saved := plan.get("personal_preferences"):
        current = personal_preferences.read(db, app.actor(), project["workspace_id"])
        if saved != current:
            raise Problem(409, "PREFERENCES_CHANGED", "个人偏好已变化，请重新提交")
    if not app.execution_authorized(plan, app.actor()):
        raise Problem(403, "EXECUTION_FORBIDDEN", "当前账号或工作空间已失去执行授权")
    plan = dict(plan, created_by=app.actor()["id"])
    if context and "repo_id" in context:
        plan = dict(
            plan,
            base_sha=context["base_sha"],
            repo_binding=app.repo_binding(
                app.delivery.repositories[context["repo_id"]]
            ),
        )
    result = app.runs.start(plan)
    db.execute(
        "INSERT INTO api_context VALUES (?,?)",
        (result["run_id"], encode(context or {})),
    )
    app.platform.link_run(db, result["run_id"], plan, app.actor())
    return app.platform.enrich_run(db, result)


def admin_run_scope(app, db, data):
    project_id = data.get("project_id")
    workspace_id = data.get("workspace_id")
    if any(key in data and (not isinstance(data[key], str) or not data[key].strip())
           for key in ("project_id", "workspace_id")):
        raise ValueError("项目和工作空间 ID 须为非空文本")
    if project_id:
        project = app.platform.authorize_project(db, project_id)
        if workspace_id and project["workspace_id"] != workspace_id:
            raise Problem(403, "WORKSPACE_FORBIDDEN", "项目不属于所选工作空间")
        if project["status"] != "active":
            raise Problem(409, "PROJECT_ARCHIVED", "归档项目不能启动新运行")
    else:
        projects = app.platform.list(db, "projects", {"workspace_id": workspace_id or "default", "status": "active"})
        project = next((p for p in projects if p["id"] == "default"), None)
        if project is None:
            if len(projects) != 1:
                raise ValueError("请在所选工作空间创建或指定一个活跃项目（project_id）")
            project = projects[0]
    return {"project_id": project["id"], "workspace_id": project["workspace_id"]}
