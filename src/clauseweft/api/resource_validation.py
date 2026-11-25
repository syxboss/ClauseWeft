"""Validate team resource fields and resolve their workspace-scoped references."""

from copy import deepcopy
import re

from clauseweft import workflow
from .http_contracts import identifier_list, text


_FIELDS = {
    "workspaces": {"name", "description", "repository_ids", "endpoint_ids"},
    "projects": {"name", "description", "repository_id", "status", "color"},
    "roles": {
        "name",
        "description",
        "stage",
        "prompt",
        "endpoint_id",
        "skill_ids",
    },
    "skills": {"name", "description", "instructions", "data_scope", "enabled"},
    "templates": {"name", "description", "execution_mode", "nodes"},
    "requirements": {
        "title",
        "description",
        "project_id",
        "status",
        "priority",
        "assignee",
    },
}
KINDS = set(_FIELDS)


def validate_resource(platform, db, kind, data):
    if data.keys() - (_FIELDS[kind] | {"workspace_id", "is_example"}):
        raise ValueError("存在不支持的平台资源字段")
    result = deepcopy(data)
    name = "title" if kind == "requirements" else "name"
    result[name] = text(result.get(name), name, maximum=160)
    result["description"] = text(
        result.get("description", ""), "description", empty=True
    )
    result.setdefault("is_example", False)
    if type(result["is_example"]) is not bool:
        raise ValueError("is_example 须为布尔值")

    if kind == "workspaces":
        _validate_workspace(platform.app, result)
        return result

    result.setdefault("workspace_id", "default")
    text(result["workspace_id"], "workspace_id", maximum=128)
    workspace = platform.get(db, "workspaces", result["workspace_id"])
    result["is_example"] = workspace["is_example"]
    if kind == "projects":
        _validate_project(result, workspace)
    elif kind == "roles":
        _validate_role(platform, db, result, workspace)
    elif kind == "skills":
        _validate_skill(result)
    elif kind == "requirements":
        _validate_requirement(platform, db, result)
    elif kind == "templates":
        _validate_template(platform, db, result, workspace)
    return result


def _validate_workspace(app, result):
    if "workspace_id" in result:
        raise ValueError("工作空间不能从属于其他工作空间")
    result.setdefault("repository_ids", [])
    result.setdefault("endpoint_ids", [])
    identifier_list(result["repository_ids"], "repository_ids")
    identifier_list(result["endpoint_ids"], "endpoint_ids")
    if set(result["repository_ids"]) - set(app.delivery.repositories):
        raise ValueError("工作空间只能授权已登记仓库")
    if set(result["endpoint_ids"]) - {
        item["endpoint_id"] for item in app.catalog.list()
    }:
        raise ValueError("工作空间只能授权已登记模型端点")


def _validate_project(result, workspace):
    result.setdefault("status", "active")
    result.setdefault("color", "#5261d8")
    result.setdefault("repository_id", "")
    text(result["repository_id"], "repository_id", empty=True, maximum=128)
    if (
        result["status"] not in ("active", "archived")
        or not isinstance(result["color"], str)
        or not re.fullmatch(r"#[0-9a-fA-F]{6}", result["color"])
    ):
        raise ValueError("项目状态或颜色无效")
    if (
        result["repository_id"]
        and result["repository_id"] not in workspace["repository_ids"]
    ):
        raise ValueError("项目须绑定工作空间已授权的仓库")


def _validate_role(platform, db, result, workspace):
    result.setdefault("stage", "review")
    result.setdefault("prompt", "")
    result.setdefault("endpoint_id", "")
    if result["stage"] not in ("general", "requirements", "review", "code", "test"):
        raise ValueError("角色阶段无效")
    text(result["prompt"], "prompt", empty=True)
    text(result["endpoint_id"], "endpoint_id", empty=True, maximum=128)
    if result["endpoint_id"]:
        platform.app.catalog.get(result["endpoint_id"])
        if result["endpoint_id"] not in workspace["endpoint_ids"]:
            raise ValueError("角色模型须在工作空间授权范围内")
    result.setdefault("skill_ids", [])
    platform.freeze_skills(db, result["skill_ids"], result["workspace_id"])


def _validate_skill(result):
    result["instructions"] = text(result.get("instructions"), "instructions")
    result.setdefault("data_scope", "local_only")
    result.setdefault("enabled", True)
    if (
        result["data_scope"] not in ("local_only", "public")
        or type(result["enabled"]) is not bool
    ):
        raise ValueError("技能数据范围或启用状态无效")


def _validate_requirement(platform, db, result):
    result.setdefault("project_id", "default")
    result.setdefault("status", "backlog")
    result.setdefault("priority", "medium")
    result.setdefault("assignee", "")
    text(result["project_id"], "project_id", maximum=128)
    project = platform.get(db, "projects", result["project_id"])
    if project["workspace_id"] != result["workspace_id"]:
        raise ValueError("需求与项目必须属于同一工作空间")
    if result["status"] not in (
        "backlog",
        "ready",
        "in_progress",
        "review",
        "done",
    ) or result["priority"] not in ("low", "medium", "high"):
        raise ValueError("需求状态或优先级无效")
    text(result["assignee"], "assignee", empty=True, maximum=160)


def _validate_template(platform, db, result, workspace):
    result.setdefault("execution_mode", "sop")
    if result["execution_mode"] not in ("sop", "workflow"):
        raise ValueError("模板执行模式无效")
    if result["execution_mode"] == "workflow" and "nodes" not in result:
        # Delivery execution adds the engine's trusted prompts when the published
        # snapshot is compiled. The editable draft holds only additional instructions.
        result["nodes"] = [dict(node, prompt="") for node in workflow.compile({"mode": "workflow"})]
    workflow.compile_sop(result.get("nodes"))
    editable_node_fields = {
        "node_id",
        "label",
        "kind",
        "stage",
        "dependencies",
        "role_id",
        "group",
        "prompt",
        "pinned_endpoint",
        "runner_id",
        "repo_id",
        "base_sha",
    }
    for node in result["nodes"]:
        if node.keys() - editable_node_fields:
            raise ValueError("SOP 节点包含服务端保管或不支持的字段")
        if node.get("kind") == "agent":
            if node.get("runner_id") not in platform.app.agent_jobs.runners:
                raise ValueError("SOP agent 节点须引用已登记的外部执行器")
            if node.get("repo_id") not in workspace["repository_ids"]:
                raise ValueError("SOP agent 节点仓库须在工作空间授权范围内")
        if (
            node.get("pinned_endpoint")
            and node["pinned_endpoint"] not in workspace["endpoint_ids"]
        ):
            raise ValueError("节点固定模型须在工作空间授权范围内")
        if "role_id" in node and not isinstance(node["role_id"], str):
            raise ValueError("角色 ID 须为文本")
        if "label" in node:
            text(node["label"], "label", maximum=160)
        if node.get("role_id"):
            text(node["role_id"], "role_id", maximum=128)
            role = platform.get(db, "roles", node["role_id"])
            if role["workspace_id"] != result["workspace_id"]:
                raise ValueError("模板角色必须属于同一工作空间")
