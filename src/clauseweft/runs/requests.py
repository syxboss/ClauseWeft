"""Build the model messages and the routing snapshot used by preview and dispatch."""

import json
import re

from clauseweft.storage import canonical

from .materials import material_messages, validate_materials


TASK_FIELDS = {
    "text",
    "materials",
    "stage",
    "max_output_tokens",
    "required_capabilities",
    "data_scope",
    "preference",
    "pinned_endpoint",
    "input_tokens",
    "budget_micros",
}


def normalize_task(raw, mode):
    if not isinstance(raw, dict) or raw.keys() - TASK_FIELDS:
        raise ValueError("任务字段无效")
    task = dict(
        stage="general" if mode in ("complex", "tool") else "auto",
        max_output_tokens=256,
        required_capabilities=[],
        data_scope="local_only",
        preference="cost",
    )
    task.update(raw)
    if "materials" in task:
        validate_materials(task["materials"])
    if type(task["max_output_tokens"]) is not int or not 1 <= task["max_output_tokens"] <= 1_000_000:
        raise ValueError("无效的输出上限")
    return task


def budget_limit(body):
    budget = body.get("budget", {})
    if not isinstance(budget, dict):
        raise ValueError("无效的预算")
    if body.get("mode") == "complex" and set(budget) != {"currency", "limit_micros"}:
        raise ValueError("复杂任务需要显式填写总预算和币种")
    if body.get("mode") == "tool" and set(budget) != {"currency", "limit_micros"}:
        raise ValueError("工具任务需要显式填写总预算和币种")
    limit = budget.get("limit_micros", 0)
    currency = budget.get("currency", "USD")
    if type(limit) is not int or not 0 <= limit <= 2**60:
        raise ValueError("无效的预算上限")
    if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
        raise ValueError("无效的预算币种")
    return limit, currency


def build_messages(task, definition, inputs, body):
    messages = []
    if definition.get("prompt"):
        messages.append({"role": "system", "content": definition["prompt"]})
    for skill in definition.get("skill_snapshots", []):
        messages.append({"role": "system", "content": (
            f"技能 {skill['name']} ({skill['id']}@{skill['revision']})\n{skill['instructions']}"
        )})
    if definition.get("feedback"):
        messages.append({"role": "user", "content": "负责人的返工意见：" + definition["feedback"]})
    if definition.get("decision_feedback"):
        messages.append({"role": "user", "content": "上次决策未通过服务端校验：" + definition["decision_feedback"]})
    for turn in body.get("conversation", {}).get("turns", []):
        messages.extend(turn["messages"])
    if preference := body.get("personal_preferences"):
        messages.append({"role": "user", "content": "本次使用的个人偏好\n" + preference["instructions"]})
    messages.append({"role": "user", "content": task["text"]})
    messages.extend(material_messages(task.get("materials", [])))
    adaptive = body.get("mode") == "tool" and "allowed_tools" in body
    for key, text in inputs.items():
        if adaptive:
            receipt = json.loads(text)
            call = {"action": "tool", "tool_id": receipt["tool_id"], "arguments": receipt["arguments"]}
            messages.append({"role": "assistant", "content": canonical(call)})
            text = "已批准并执行以上调用。工具返回的数据如下：\n" + text
        messages.append({"role": "user", "content": f"{key}\n{text}"})
    if adaptive and inputs:
        messages[-1]["content"] += "\n本轮从上述回执继续。依据最初目标只提交下一步决策。"
    return messages


def request_messages(store, body, n, refs, nodes):
    definition = n["definition"]
    inputs = {key: store.read_blob(sha).decode() for key, sha in refs.items()}
    if body["mode"] == "complex" and "brief" in definition:
        inputs = {"子任务说明": definition["brief"]}
    elif body["mode"] == "complex" and n["node_id"] == "summary":
        inputs = {key: f"分工：{nodes[key]['definition']['brief']}\n结果：{text}"
                  for key, text in inputs.items()}
    return build_messages(body["task"], definition, inputs, body)


def route_snapshot(task, definition, messages, models, budget, currency, frozen=None):
    params = dict(
        task,
        stage=definition["stage"],
        input_tokens=len(canonical(messages).encode()) + 16,
        budget_micros=max(0, budget),
    )
    if any(skill["data_scope"] == "local_only" for skill in definition.get("skill_snapshots", [])):
        params["data_scope"] = "local_only"
    if "pinned_endpoint" in definition:
        params["pinned_endpoint"] = definition["pinned_endpoint"]
    pool = []
    for model in models:
        endpoint = dict(model)
        constraints = []
        if task.get("_workspace_blocked"):
            constraints.append("workspace_policy_changed")
        if endpoint["currency"] != currency:
            constraints.append("currency_mismatch")
        if frozen is not None and frozen.get(endpoint["endpoint_id"]) != endpoint["revision"]:
            constraints.append("outside_frozen_catalog")
        if constraints:
            endpoint["allowed"] = False
            endpoint["policy_reasons"] = constraints
        pool.append(endpoint)
    return {"task": params, "models": pool}
