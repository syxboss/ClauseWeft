"""Deterministic route preview over an explicit snapshot; performs no network I/O."""

import hashlib
import json
import sys
from pathlib import Path

INTENTS = {
    "general": (),
    "requirements": ("梳理需求", "撰写需求", "写需求", "需求分析", "验收标准"),
    "review": ("评审", "审查", "评估", "review"),
    "code": ("实现", "编写代码", "重构", "implement"),
    "test": ("编写测试", "运行测试", "补充测试", "test case"),
}
RULESET = "preview-4"


def enum_value(value, choices, name):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f"{name} 必须是 {' / '.join(choices)} 之一")
    return value


def read_int(data, field, *, optional=False, minimum=0, maximum=None):
    value = data.get(field)
    if value is None and optional:
        return None
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{field} 超出允许的整数范围")
    return value


def read_text(data, field):
    value = data.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 必须是非空文本")
    return value


def read_list(data, field):
    items = data.get(field)
    if not isinstance(items, list) or any(not isinstance(i, str) or not i for i in items):
        raise ValueError(f"{field} 必须是字符串列表")
    return set(items)


def model_candidate(model, task, intent):
    if not isinstance(model, dict):
        raise ValueError("模型配置必须为对象")
    endpoint = read_text(model, "endpoint_id")
    identity = read_text(model, "model_identity")
    location = enum_value(model.get("location"), ("local", "cloud"), "location")
    for flag in ("enabled", "allowed"):
        if type(model.get(flag)) is not bool:
            raise ValueError(f"{flag} 必须为布尔值")
    capabilities = read_list(model, "capabilities")
    context = read_int(model, "context_tokens", minimum=1)
    endpoint_limit = read_int(model, "max_output_tokens", optional=True, minimum=1, maximum=1_048_576)
    output_limit = min(task["max_output_tokens"], endpoint_limit) if endpoint_limit else task["max_output_tokens"]
    fits = model.get("fit")
    if not isinstance(fits, dict) or any(k not in INTENTS for k in fits):
        raise ValueError("fit 必须为意图到适配等级的映射")
    for key in fits:
        read_int(fits, key, minimum=1, maximum=5)
    fit = fits.get(intent, 3 if intent == "general" else 0)
    in_price = read_int(model, "input_micros_per_million", optional=True)
    out_price = read_int(model, "output_micros_per_million", optional=True)
    estimate = None
    if in_price is not None and out_price is not None:
        amount = in_price * task["input_tokens"] + out_price * output_limit
        estimate = (amount + 999_999) // 1_000_000
    reasons = []
    if not model["allowed"]:
        reasons.extend(sorted(read_list(model, "policy_reasons")) if "policy_reasons" in model else [])
    if not model["enabled"] or not model["allowed"]:
        reasons.append("endpoint_unavailable")
    if task.get("pinned_endpoint") and endpoint != task["pinned_endpoint"]:
        reasons.append("different_from_pin")
    if task["data_scope"] == "local_only" and location != "local":
        reasons.append("data_scope")
    if task["input_tokens"] + output_limit > context:
        reasons.append("context_limit")
    if not set(task["required_capabilities"]) <= capabilities:
        reasons.append("missing_capability")
    if not fit:
        reasons.append("unsupported_intent")
    if estimate is None:
        reasons.append("unknown_price")
    elif estimate > task["budget_micros"]:
        reasons.append("budget_limit")
    return dict(endpoint_id=endpoint, model_identity=identity, location=location, fit=fit, max_output_tokens=output_limit,
                estimated_micros=estimate, latency_ms=read_int(model, "latency_ms", optional=True),
                quality_bp=read_int(model, "quality_bp", optional=True, maximum=10000),
                priority=read_int(model, "priority"), reasons=reasons)


def preview(snapshot):
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("task"), dict):
        raise ValueError("缺少 task 对象")
    task = snapshot["task"]
    text = read_text(task, "text").casefold()
    stage = enum_value(task.get("stage", "auto"), ("auto", *INTENTS), "stage")
    scope = enum_value(task.get("data_scope"), ("local_only", "public"), "data_scope")
    preference = enum_value(task.get("preference"), ("cost", "quality", "latency"), "preference")
    for field in ("input_tokens", "max_output_tokens", "budget_micros"):
        read_int(task, field)
    if task.get("pinned_endpoint") is not None:
        read_text(task, "pinned_endpoint")
    read_list(task, "required_capabilities")
    if not isinstance(snapshot.get("models"), list):
        raise ValueError("缺少 models 列表")
    encoded = json.dumps(snapshot, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    result = dict(ruleset=RULESET, snapshot_sha256=hashlib.sha256(encoded).hexdigest(),
                  selected_endpoint=None, data_scope=scope)
    matches = [intent for intent, words in INTENTS.items() if any(word in text for word in words)]
    if stage == "auto" and len(matches) != 1:
        return dict(result, status="needs_input", intent_options=matches or list(INTENTS))
    intent = matches[0] if stage == "auto" else stage
    candidates = [model_candidate(m, task, intent) for m in snapshot["models"]]
    ids = [c["endpoint_id"] for c in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("endpoint_id 必须唯一")
    eligible = [c for c in candidates if not c["reasons"]]
    metric = {"cost": "estimated_micros", "quality": "quality_bp", "latency": "latency_ms"}[preference]
    fallback_fit = {c["fit"] for c in eligible if c[metric] is None}
    for candidate in eligible:
        candidate["rank_metric"] = "priority" if candidate["fit"] in fallback_fit else metric

    def order(c):
        basis = c["rank_metric"]
        score = -c[basis] if basis == "quality_bp" else c[basis]
        return (-c["fit"], score, c["endpoint_id"])

    eligible.sort(key=order)
    rejected = sorted((c for c in candidates if c["reasons"]), key=lambda c: c["endpoint_id"])
    return dict(result, status="selected" if eligible else "blocked", intent=intent,
                selected_endpoint=eligible[0]["endpoint_id"] if eligible else None,
                rank_metric=eligible[0]["rank_metric"] if eligible else metric, candidates=eligible + rejected)


def main_from_file(path):
    try:
        with Path(path).open(encoding="utf-8") as source:
            result = preview(json.load(source))
    except (ValueError, OSError) as exc:
        print(f"路由预览失败：{exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "selected" else 1
