"""Compile task graphs and allocate verified, distinct model identities."""

import json
import re
from copy import deepcopy
from graphlib import TopologicalSorter, CycleError

from clauseweft.project_tools.decisions import decision_prompt, proposal_prompt, validate_plan

PROMPTS = {
    "draft": '撰写可验证的需求。仅返回 JSON {"clauses":[{"id":"...","text":"...","acceptance":"..."}]}。',
    "review-product": "从产品视角审查需求。",
    "review-test": "从测试视角独立审查需求。",
    "code": "根据已批准的需求实施代码变更。",
    "test-design": "根据已批准的需求独立编写验收测试。",
    "code-review": "结合需求与实际候选补丁审查代码。",
}
for key in ("review-product", "review-test", "code-review"):
    PROMPTS[key] += (
        '仅返回 JSON {"findings":[{"location":"条款 ID 或代码位置","severity":"blocker/major/minor/info","example":"...","suggestion":"...","evidence":"..."}]}。'
    )
for key in ("code", "test-design"):
    PROMPTS[key] += '仅返回 JSON {"patch":"可直接应用的 unified diff"}。'


def node(key, stage, dependencies=(), *, gate=False, group=None):
    return {
        "node_id": key,
        "stage": stage,
        "kind": "gate" if gate else "model",
        "dependencies": list(dependencies),
        "group": group,
        "prompt": PROMPTS.get(key, ""),
    }


def compile(body):
    mode = body.get("mode", "single")
    if mode in ("single", "complex"):
        stage = body["task"].get("stage", "general" if mode == "complex" else "auto")
        if mode == "complex":
            first = node("plan", stage)
            first["prompt"] = (
                '将目标拆成 2 至 8 项可独立完成的工作，写明每项的范围和预期结果。'
                '仅返回 JSON {"subtasks":["分工说明", "..."]}，每项最多 4000 字符。'
            )
        else:
            first = node("task", stage)
            role = body.get("role_snapshot")
            if role:
                first.update(prompt=role["prompt"], role_id=role["id"], role_revision=role["revision"])
        first["skill_snapshots"] = deepcopy(body.get("skill_snapshots", []))
        nodes = [first]
    elif mode == "tool" and "allowed_tools" in body:
        validate_plan(body)
        nodes = [tool_decision_node(body, 1)]
    elif mode == "tool":
        propose = node("propose", "general")
        propose.update(label="拟定工具参数", prompt=proposal_prompt(body.get("tool_id"), body))
        confirm = node("confirm", "general", ["propose"], gate=True)
        confirm["label"] = "确认并执行工具"
        answer = node("answer", "general", ["confirm"])
        answer.update(label="整理结果", prompt="依据工具的真实执行回执回答原目标。检索条目是参考资料，引用其来源或 ID；没有命中时明确说明。")
        nodes = [propose, confirm, answer]
    elif mode == "workflow":
        nodes = [
            node("draft", "requirements"),
            node("review-product", "review", ["draft"], group="spec-review"),
            node("review-test", "review", ["draft"], group="spec-review"),
            node("approve-spec", "review", ["review-product", "review-test"], gate=True),
            node("code", "code", ["approve-spec"], group="implementation"),
            node("test-design", "test", ["approve-spec"], group="implementation"),
            node("integrate", "code", ["code", "test-design"], gate=True),
            node("code-review", "review", ["approve-spec", "integrate"]),
            node("execute-tests", "test", ["integrate"], gate=True),
            node("accept", "review", ["code-review", "execute-tests"], gate=True),
        ]
    elif mode == "sop":
        snapshot = body.get("template_snapshot")
        if not isinstance(snapshot, dict):
            raise ValueError("SOP 运行需要已发布的模板快照")
        nodes = compile_sop(snapshot.get("nodes"))
    else:
        raise ValueError("mode 必须是 single、complex、tool、workflow 或 sop")
    if mode == "workflow" and body.get("template_snapshot"):
        configured = compile_sop(body["template_snapshot"].get("nodes"))
        expected = {n["node_id"]: n for n in nodes}
        if {n["node_id"] for n in configured} != expected.keys():
            raise ValueError("交付模板必须保留全部必需节点")
        for definition in configured:
            original = expected[definition["node_id"]]
            if any(definition[field] != original[field] for field in ("kind", "stage", "dependencies", "group")):
                raise ValueError("交付模板须保留审批、整合及实测依赖")
            # Preserve the output schema consumed by the delivery importer.
            definition["prompt"] = original["prompt"] + "\n" + definition["prompt"]
        nodes = configured
    overrides = body.get("pinned_endpoints", {})
    if not isinstance(overrides, dict) or overrides.keys() - {
        n["node_id"] for n in nodes if n["kind"] == "model"
    }:
        raise ValueError("pinned_endpoints 含无效分支")
    for n in nodes:
        if n["node_id"] in overrides:
            pin = overrides[n["node_id"]]
            if not isinstance(pin, str) or not pin.strip():
                raise ValueError("固定模型 ID 无效")
            n["pinned_endpoint"] = pin
    validate(nodes)
    return nodes


def tool_decision_node(body, step, last_receipt=None):
    definition = node(f"decide-{step}", "general", [f"confirm-{i}" for i in range(1, step)])
    definition.update(tool_step=step, label=f"第 {step} 轮决策", prompt=decision_prompt(body, step, last_receipt))
    return definition


def tool_gate(step, name):
    gate = node(f"confirm-{step}", "general", [f"decide-{step}"], gate=True)
    gate.update(tool_step=step, label=f"第 {step} 步 · {name}")
    return gate


def expand_complex(plan_text, planner):
    """Only the subtask briefs are model-provided. The server owns the graph."""
    plan = json.loads(plan_text)
    if not isinstance(plan, dict) or set(plan) != {"subtasks"}:
        raise ValueError("规划结果须为含 subtasks 的对象")
    briefs = plan["subtasks"]
    if not isinstance(briefs, list) or not 2 <= len(briefs) <= 8:
        raise ValueError("复杂任务需要 2 至 8 项分工")
    branches = []
    for index, brief in enumerate(briefs, start=1):
        if not isinstance(brief, str) or not 1 <= len(brief.strip()) <= 4000:
            raise ValueError("每项分工须为 1 至 4000 字符的文本")
        brief = brief.strip()
        brief.encode("utf-8")  # Reject unpaired surrogates escaped in the JSON reply.
        branch = node(f"branch-{index}", planner["stage"], ["plan"])
        branch.update(brief=brief, prompt="根据原目标完成所分配的子任务，交回结果、依据和未解决的问题。")
        branches.append(branch)
    summary = node("summary", planner["stage"], [branch["node_id"] for branch in branches])
    summary["prompt"] = "依据各子任务的分工和结果完成原始目标，说明分歧与尚未解决的问题。"
    for definition in [*branches, summary]:
        definition["skill_snapshots"] = deepcopy(planner.get("skill_snapshots", []))
    return [*branches, summary]


def compile_sop(raw):
    """Own and normalize a published DAG before storing its executable snapshot."""
    if not isinstance(raw, list) or not 1 <= len(raw) <= 64:
        raise ValueError("SOP 须包含 1 到 64 个节点")
    nodes = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("SOP 节点须为对象")
        definition = deepcopy(item)
        kind = definition.get("kind", "model")
        if kind not in ("model", "gate", "agent"):
            raise ValueError("SOP 节点类型须为 model、gate 或 agent")
        stage = definition.get("stage", "code" if kind == "agent" else "review")
        if stage not in ("auto", "general", "requirements", "review", "code", "test"):
            raise ValueError("SOP 节点阶段无效")
        definition.update(kind=kind, stage=stage)
        definition.setdefault("dependencies", [])
        definition.setdefault("group", None)
        definition.setdefault("prompt", "")
        if not isinstance(definition["prompt"], str) or len(definition["prompt"]) > 32000:
            raise ValueError("Agent 提示须为不超过 32000 字符的文本")
        if kind == "agent":
            if (not isinstance(definition.get("runner_id"), str) or not definition["runner_id"].strip()
                    or not isinstance(definition.get("repo_id"), str) or not definition["repo_id"].strip()):
                raise ValueError("SOP agent 节点须指定 runner_id 与 repo_id")
            if "pinned_endpoint" in definition:
                raise ValueError("SOP agent 节点不能设置 pinned_endpoint")
            definition.setdefault("base_sha", "HEAD")
            if not isinstance(definition["base_sha"], str) or not definition["base_sha"].strip():
                raise ValueError("SOP agent 节点 base_sha 无效")
        if definition["group"] is not None and (
            kind != "model" or not isinstance(definition["group"], str)
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", definition["group"])
        ):
            raise ValueError("独立模型分组须为有效标识")
        if "pinned_endpoint" in definition and (
            not isinstance(definition["pinned_endpoint"], str) or not definition["pinned_endpoint"].strip()
        ):
            raise ValueError("节点固定端点无效")
        nodes.append(definition)
    validate(nodes)
    by_id = {n["node_id"]: n for n in nodes}
    groups = {}
    for definition in nodes:
        group = definition["group"]
        dependencies = set(definition["dependencies"])
        if group and group in groups and groups[group] != dependencies:
            raise ValueError("同一独立模型组须读取相同上游节点")
        if group:
            groups[group] = dependencies
    order = TopologicalSorter({n["node_id"]: n["dependencies"] for n in nodes}).static_order()
    return [by_id[key] for key in order]


def validate(nodes):
    if not isinstance(nodes, list) or not nodes:
        raise ValueError("流程图至少需要一个节点")
    for definition in nodes:
        if not isinstance(definition, dict) or not isinstance(definition.get("node_id"), str):
            raise ValueError("无效的节点 ID")
        dependencies = definition.get("dependencies")
        if not isinstance(dependencies, list) or any(not isinstance(key, str) for key in dependencies):
            raise ValueError("节点依赖须为标识列表")
        if len(dependencies) != len(set(dependencies)):
            raise ValueError("节点依赖不能重复")
    edges = {n["node_id"]: n["dependencies"] for n in nodes}
    if len(nodes) != len(edges) or any(not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", k) for k in edges):
        raise ValueError("无效或重复的节点 ID")
    if any(set(deps) - edges.keys() for deps in edges.values()):
        raise ValueError("依赖节点不存在")
    try:
        tuple(TopologicalSorter(edges).static_order())
    except CycleError:
        raise ValueError("流程图不能包含循环依赖") from None


def allocate(choices, occupied):
    """Augmenting paths let a constrained branch displace a flexible branch."""
    owners = {}
    result = {}

    def take(key, visited):
        for candidate in choices[key]:
            identity = candidate["model_identity"]
            if identity in occupied or identity in visited:
                continue
            visited.add(identity)
            previous = owners.get(identity)
            if previous is None or take(previous, visited):
                owners[identity] = key
                result[key] = candidate
                return True
        return False

    for key in sorted(choices, key=lambda k: (len(choices[k]), k)):
        if not take(key, set()):
            return {}
    return result
