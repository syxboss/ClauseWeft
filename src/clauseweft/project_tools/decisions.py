"""Define tool permissions, model prompts, argument schemas and decision validation."""

import json


TOOLS = {
    "search_requirements": ("检索项目需求", '{"query":"一个具体关键词"}'),
    "search_reference": ("检索项目知识库", '{"query":"一个具体关键词"}'),
    "create_requirement": (
        "新建项目需求",
        '{"title":"简明标题","description":"需求与验收条件","priority":"medium"}',
    ),
}
TOOL_RISKS = {
    "search_requirements": "read_only",
    "search_reference": "read_only",
    "create_requirement": "mutating",
}
APPROVAL_POLICIES = ("always", "auto_read_only")


def is_read_only(tool_id):
    return TOOL_RISKS.get(tool_id) == "read_only"


def checked_text(value, limit, name):
    if not isinstance(value, str) or not 1 <= len(value.strip()) <= limit:
        raise ValueError(f"{name} 应为 1 至 {limit} 字的文本")
    value.strip().encode("utf-8")
    return value.strip()


def proposal_prompt(tool_id):
    if not isinstance(tool_id, str) or tool_id not in TOOLS:
        raise ValueError("请选择已登记的项目工具")
    name, schema = TOOLS[tool_id]
    prompt = f"为用户选定的工具「{name}」拟定参数。只返回一个符合此结构的 JSON 对象：{schema}。"
    if tool_id == "create_requirement":
        prompt += "priority 取 low、medium 或 high。"
    return prompt + "等待负责人确认后才执行。"


def validate_plan(data):
    policy = data.get("tool_approval_policy", "always")
    if not isinstance(policy, str) or policy not in APPROVAL_POLICIES:
        raise ValueError("工具审批策略须为 always 或 auto_read_only")
    if "allowed_tools" not in data:
        if "max_tool_steps" in data or "requirement_query" in data:
            raise ValueError("多步工具设置需要 allowed_tools")
        proposal_prompt(data.get("tool_id"))
        return
    tools = data["allowed_tools"]
    if ("tool_id" in data or not isinstance(tools, list) or not 1 <= len(tools) <= len(TOOLS)
            or any(not isinstance(k, str) or k not in TOOLS for k in tools) or len(set(tools)) != len(tools)):
        raise ValueError("allowed_tools 须为本次允许使用的已登记工具，不能重复或同时指定 tool_id")
    steps = data.setdefault("max_tool_steps", 4)
    if type(steps) is not int or not 1 <= steps <= 8:
        raise ValueError("工具执行次数上限为 1 至 8 次")
    if "requirement_query" in data:
        data["requirement_query"] = checked_text(data["requirement_query"], 160, "需求查重关键词")
        if not {"search_requirements", "create_requirement"} <= set(tools) or steps < 2:
            raise ValueError("查重后新建需要授权检索与新建需求，并至少允许两次工具调用")


def uses_reference(data):
    return data.get("tool_id") == "search_reference" or "search_reference" in data.get("allowed_tools", ())


def decision_prompt(body, step, last_receipt=None):
    format_rule = "你正在执行一个工具任务。此轮只输出一个 JSON 对象。禁止给出计划、代码块、示例或对象外的解释。"
    query = body.get("requirement_query")
    if query:
        if not last_receipt:
            expected = {"action": "tool", "tool_id": "search_requirements", "arguments": {"query": query}}
            return format_rule + "\n本轮是查重。输出以下检索申请并等待回执：\n" + json.dumps(expected, ensure_ascii=False)
        if last_receipt["tool_id"] == "search_requirements" and not last_receipt["result"]["items"]:
            return (format_rule + "\n已检索，未命中。本轮申请新建。title 和 description 都是必填的非空文本。"
                    f"标题包含「{query}」。用一句具体的话填写用户要求的验收条件。\n"
                    '{"action":"tool","tool_id":"create_requirement","arguments":{"title":"需求标题",'
                    '"description":"根据本次用户目标填写的验收条件","priority":"medium"}}')
        return (format_rule + '\n已得到需求回执。本轮只输出 {"action":"finish","answer":"给用户的中文答复"}。'
                "说明命中已有或创建结果，在答复中逐字引用实际需求 ID。")
    finish = '输出 {"action":"finish","answer":"给用户的答复"}，说明实际操作并逐字引用回执中的条目 ID。'
    if step > body["max_tool_steps"]:
        return format_rule + "\n已到工具执行次数上限。根据已有回执回答并交代未完成事项。" + finish
    schemas = {
        "search_requirements": "search_requirements 在本项目查找需求。arguments 只含 query，使用用户给的检索关键词。",
        "search_reference": "search_reference 检索知识库。arguments 只含 query，使用用户给的关键词。",
        "create_requirement": "create_requirement 申请新建需求。arguments 必填 title、description、priority。标题和验收条件取自用户原文；优先级取 low、medium 或 high。",
    }
    prompt = (format_rule + '\n申请工具时输出 {"action":"tool","tool_id":"所选标识","arguments":{参数}}。'
              "只做当前这一步，按用户写明的先后顺序执行。权限清单：\n"
              + "\n".join(schemas[key] for key in body["allowed_tools"]))
    if not last_receipt:
        return prompt + "\n目前没有已执行的动作。按用户的第一项要求选择一个工具，等待结果。"
    output = last_receipt["result"]
    if "items" in output:
        prompt += f"\n上一步是检索，命中 {len(output['items'])} 项。"
        if not output["items"]:
            prompt += "相同的检索已经返回空结果。若用户要求缺失时新建，下一步应调用 create_requirement，原样采用用户指定的标题、优先级与验收条件。"
        elif last_receipt["tool_id"] == "search_requirements":
            ids = [item["id"] for item in output["items"]]
            example = {"action": "finish", "answer": "已找到项目需求：" + "、".join(ids)}
            prompt += "\n如果用户要求查到后结束，直接提交答复，例如 " + json.dumps(example, ensure_ascii=False)
        else:
            prompt += "如果已查到所需材料，直接答复并引用命中 ID。"
    else:
        prompt += "\n上一步已有执行回执。"
    prompt += "\n如果用户目标已经实现，" + finish + "如果尚有用户要求的动作，继续申请相应工具。"
    return prompt


class InvalidDecision(ValueError):
    def __init__(self, message, code="invalid_agent_decision"):
        super().__init__(message)
        self.code = code


def read_decision(raw):
    value = json.loads(raw)
    if isinstance(value, dict) and set(value) == {"tool", "arguments"}:
        return {"action": "tool", "tool_id": value["tool"], "arguments": value["arguments"]}
    return value


def requests_another_tool(text):
    try:
        value = read_decision(text)
    except (ValueError, RecursionError):
        return False
    return (isinstance(value, dict) and value.get("action") == "tool"
            and isinstance(value.get("tool_id"), str) and "arguments" in value)


def decision(body, raw, receipts):
    if len(raw) > 32000:
        raise InvalidDecision("决策超出大小上限")
    value = read_decision(raw)
    if not isinstance(value, dict):
        raise InvalidDecision("决策须为 JSON 对象")
    query = body.get("requirement_query")
    if value.get("action") == "finish" and set(value) == {"action", "answer"}:
        if not receipts:
            raise InvalidDecision("请先申请执行与目标相关的工具")
        answer = checked_text(value["answer"], 32000, "最终答复")
        if query:
            records = receipts[0]["result"]["items"]
            if not records and len(receipts) == 1:
                raise InvalidDecision("检索无匹配，尚需申请新建需求")
            if not any(record["id"] in answer for record in (records or [receipts[-1]["result"]])):
                raise InvalidDecision("最终答复须引用真实需求 ID，请从已执行的工具回执中完整复制")
        return {"answer": answer}
    if value.get("action") != "tool" or set(value) != {"action", "tool_id", "arguments"}:
        raise InvalidDecision("决策须包含完整的工具提议或最终答复")
    if len(receipts) >= body["max_tool_steps"]:
        raise InvalidDecision("已到工具执行次数上限，请依据现有回执给出最终答复", "tool_step_limit")
    tool = value["tool_id"]
    if tool not in body["allowed_tools"]:
        raise InvalidDecision("所提议的工具超出本次授权范围")
    args = checked_arguments(tool, value["arguments"])
    if not query and tool == "create_requirement" and receipts and receipts[-1]["tool_id"] == "search_requirements":
        for record in receipts[-1]["result"]["items"]:
            if record["title"].casefold() == args["title"].casefold():
                raise InvalidDecision("刚才的检索已找到同名需求 " + record["id"] + "。请引用回执或按用户目标选择其他操作")
    if query:
        if not receipts:
            if tool != "search_requirements" or args["query"].casefold() != query.casefold():
                raise InvalidDecision("请先使用指定关键词检索项目需求")
        elif (receipts[0]["result"]["items"] or len(receipts) != 1 or tool != "create_requirement"
              or query.casefold() not in args["title"].casefold()):
            raise InvalidDecision("仅在查重无匹配时允许创建一次，标题须包含查重关键词")
    return {"id": tool, "arguments": args}


def arguments(tool_id, raw):
    if len(raw) > 32000:
        raise ValueError("提议超出大小上限")
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("模型提议须为有效的 JSON 对象") from None
    return checked_arguments(tool_id, value)


def checked_arguments(tool_id, value):
    fields = {"query": 160} if tool_id != "create_requirement" else {
        "title": 160, "description": 4000, "priority": 6,
    }
    if not isinstance(value, dict) or value.keys() != fields.keys():
        raise ValueError("提议字段与所选工具不匹配")
    values = {key: checked_text(value[key], size, key) for key, size in fields.items()}
    if "priority" in values and values["priority"] not in ("low", "medium", "high"):
        raise ValueError("优先级须为 low、medium 或 high")
    return values
