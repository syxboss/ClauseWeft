"""Build a new directly billed turn from a project-owned, immutable answer."""

import json

from clauseweft import audit
from clauseweft.runs.materials import material_messages
from .http_contracts import Problem


MAX_HISTORY_TURNS = 8
MAX_HISTORY_BYTES = 24 * 1024
MAX_COMPACTION_BYTES = 4096
EXCERPT_MARKER = "\n〔中间内容已节选〕\n"


def final_answer(run):
    target = {"single": "task", "complex": "summary", "tool": "answer"}.get(run["mode"])
    for node in run["nodes"]:
        matches = node["node_id"] == target or (run["mode"] == "tool" and node.get("is_agent_answer"))
        if matches and node["state"] == "succeeded" and node["artifact"]:
            return node
    return None


def actions_for(run, body):
    if body.get("repo_id") or run["mode"] not in ("single", "complex", "tool"):
        return []
    if run["status"] == "completed" and final_answer(run):
        return ["followup"] if run["mode"] == "tool" else ["followup", "regenerate"]
    if (run["mode"] == "single" and run["status"] in ("cancelled", "failed", "needs_input")
            and not run["budget"]["uncertain_attempts"]):
        return ["regenerate"]
    return []


def excerpt(text, budget):
    raw = text.encode("utf-8")
    if len(raw) <= budget:
        return text
    half = (budget - len(EXCERPT_MARKER.encode())) // 2
    return raw[:half].decode("utf-8", "ignore") + EXCERPT_MARKER + raw[-half:].decode("utf-8", "ignore")


def tool_receipts(app, db, run):
    """Use confirmed, project-owned results. A proposal alone is never evidence."""
    receipts = []
    for node in run["nodes"]:
        if node["kind"] != "gate" or node["state"] != "succeeded" or not node["artifact"]:
            continue
        receipt = json.loads(app.delivery.artifact(db, run, node["artifact"])["content"])
        if receipt.get("project_id") != run["project_id"]:
            raise Problem(403, "TOOL_SOURCE", "工具回执所属项目不匹配")
        result = json.dumps(receipt["result"], ensure_ascii=False, separators=(",", ":"))
        receipts.append((node["artifact"], f"已确认的项目工具回执（{receipt['tool_id']}）\n{result}"))
    return receipts


def _compact_turns(previous_compaction, dropped_turns):
    if not dropped_turns:
        return previous_compaction
    base = previous_compaction or {}
    lines = [base["summary"]] if base.get("summary") else []
    run_ids = list(base.get("source_run_ids", []))
    answer_refs = list(base.get("source_answer_refs", []))
    for item in dropped_turns:
        user_msg = next((m["content"] for m in item["messages"] if m["role"] == "user"), "")
        asst_msg = next((m["content"] for m in reversed(item["messages"]) if m["role"] == "assistant"), "")
        q = " ".join(user_msg.split())[:160]
        a = " ".join(asst_msg.split())[:240]
        lines.append(f"- [{item['run_id']}] 问：{q} | 答：{a}")
        if item["run_id"] not in run_ids:
            run_ids.append(item["run_id"])
        if item.get("answer_ref") and item["answer_ref"] not in answer_refs:
            answer_refs.append(item["answer_ref"])
    summary = excerpt("\n".join(lines), MAX_COMPACTION_BYTES)
    return {
        "summary": summary,
        "compacted_turns": base.get("compacted_turns", 0) + len(dropped_turns),
        "source_run_ids": run_ids[-64:],
        "source_answer_refs": answer_refs[-64:],
    }


def append_turn_with_compaction(previous, run, task, answer, answer_ref, receipts=()):
    user_text = task["text"]
    if task.get("attachments"):
        user_text = f"{user_text}\n〔附带 {len(task['attachments'])} 张图片附件〕"
    messages = [{"role": "user", "content": user_text}]
    messages.extend(material_messages(task.get("materials", [])))
    messages.extend({"role": "user", "content": content} for _, content in receipts)
    messages.append({"role": "assistant", "content": answer})
    turn = {"run_id": run["run_id"], "answer_ref": answer_ref, "messages": messages}
    if receipts:
        turn["receipt_refs"] = [ref for ref, _ in receipts]
    if sum(len(m["content"].encode()) for m in messages) > MAX_HISTORY_BYTES:
        quota = MAX_HISTORY_BYTES // len(messages)
        turn["messages"] = [dict(m, content=excerpt(m["content"], quota)) for m in messages]
        turn["excerpted"] = True
    candidates = [*previous.get("turns", []), turn]
    remaining = MAX_HISTORY_BYTES
    kept = []
    for candidate in reversed(candidates[-MAX_HISTORY_TURNS:]):
        size = sum(len(m["content"].encode()) for m in candidate["messages"])
        if size > remaining:
            break
        kept.append(candidate)
        remaining -= size
    kept = list(reversed(kept))
    dropped = candidates[:len(candidates) - len(kept)]
    omitted = previous.get("omitted_turns", 0) + len(dropped)
    compaction = _compact_turns(previous.get("compaction"), dropped)
    return kept, omitted, compaction


def append_turn(previous, run, task, answer, answer_ref, receipts=()):
    kept, omitted, _ = append_turn_with_compaction(previous, run, task, answer, answer_ref, receipts)
    return kept, omitted


def create(app, db, run, data):
    operation = data.get("action")
    if operation not in ("followup", "regenerate"):
        raise ValueError("action 须为 followup 或 regenerate")
    allowed = {"action", "budget", "use_preferences"}
    if operation == "followup":
        allowed.update(("text", "materials", "attachments"))
    elif operation == "regenerate":
        allowed.add("max_output_tokens")
    if data.keys() - allowed:
        raise ValueError("此操作包含不支持的字段")
    rid = run["run_id"]
    body = json.loads(db.execute("SELECT body FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
    if operation not in actions_for(run, body):
        raise Problem(409, "CONTINUATION_UNAVAILABLE", "当前任务状态不支持此操作")
    previous = body.get("conversation", {})
    source_task = body["task"]
    if run.get("created_by") != app.actor()["id"]:
        # Reusing a teammate's turn must respect the same redacted input view
        # that they can read on the source record.
        previous = audit.sanitize(previous)
        source_task = {**source_task, "text": audit.sanitize(source_task["text"])}
        if "materials" in source_task:
            source_task["materials"] = audit.sanitize(source_task["materials"])
        source_task.pop("attachments", None)
    turns, omitted = previous.get("turns", []), previous.get("omitted_turns", 0)
    compaction = previous.get("compaction")
    task = {key: source_task[key] for key in (
        "text", "materials", "attachments", "stage", "data_scope", "pinned_endpoint", "preference",
        "required_capabilities", "max_output_tokens",
    ) if key in source_task}
    if operation == "regenerate" and "max_output_tokens" in data:
        task["max_output_tokens"] = data["max_output_tokens"]
    if operation == "followup":
        if not isinstance(data.get("text"), str) or not data["text"].strip():
            raise ValueError("请填写追问内容")
        node = final_answer(run)
        answer = app.delivery.artifact(db, run, node["artifact"])["content"]
        receipts = tool_receipts(app, db, run) if run["mode"] == "tool" else ()
        turns, omitted, compaction = append_turn_with_compaction(
            previous, run, source_task, answer, node["artifact"], receipts
        )
        task["text"] = data["text"].strip()
        task.pop("materials", None)
        task.pop("attachments", None)
        if "materials" in data:
            task["materials"] = data["materials"]
        if "attachments" in data:
            task["attachments"] = data["attachments"]

    # An answer can have been produced under a stricter policy than the original
    # request. Preserve that restriction even after instance or skill settings change.
    current_scope = app.current_scope(dict(body, created_by=app.actor()["id"]))
    if current_scope == "blocked":
        raise Problem(403, "DATA_SCOPE", "来源运行的执行授权或技能策略已变化")
    prior_local = db.execute(
        """SELECT 1 FROM attempts WHERE (run_id=? OR attempt_id IN
           (SELECT attempt_id FROM nodes WHERE run_id=?))
           AND json_extract(snapshot,'$.task.data_scope')='local_only' LIMIT 1""",
        (rid, rid),
    ).fetchone()
    frozen_local = (
        any(s["data_scope"] == "local_only" for s in body.get("skill_snapshots", []))
        or any(m.get("data_scope") == "local_only" for m in body.get("memory_snapshots", []))
    )
    if current_scope == "local_only" or prior_local or frozen_local:
        task["data_scope"] = "local_only"

    inherited = {
        k: body[k] for k in ("role_id", "role_snapshot", "skill_snapshots", "memory_snapshots", "endpoint_revisions")
        if k in body
    }
    if pin := body.get("pinned_endpoints", {}).get("task"):
        task["pinned_endpoint"] = pin
    inherited.update(source_run_id=rid, conversation={
        "action": operation, "source_id": rid, "source_revision": run["revision"],
        "turns": turns, "omitted_turns": omitted,
        **({"compaction": compaction} if compaction else {}),
    })
    reference_digest = body.get("reference_digest") or previous.get("reference_digest")
    if reference_digest:
        inherited["conversation"]["reference_digest"] = reference_digest
    request = {k: data[k] for k in ("budget", "use_preferences") if k in data}
    request.update(project_id=run["project_id"], task=task)
    if run.get("requirement_id"):
        request["requirement_id"] = run["requirement_id"]
    plan = app.plan(request, inherited=inherited)
    # An inferred ceiling may shrink for a shorter turn. Raising an explicit
    # source ceiling takes an explicit new budget from the caller.
    source_cap = previous.get("budget_ceiling")
    if body.get("budget_source") == "explicit":
        source_cap = {k: run["budget"][k] for k in ("currency", "limit_micros")}
    if "budget" not in request and source_cap:
        if plan["budget"]["currency"] != source_cap["currency"]:
            raise ValueError("币种已变化，请明确填写新任务预算")
        plan["conversation"]["budget_ceiling"] = source_cap
        if source_cap["limit_micros"] <= plan["budget"]["limit_micros"]:
            plan["budget"]["limit_micros"] = source_cap["limit_micros"]
            plan["budget_source"] = "source_limit"
    child = app.start(db, plan, {})
    app.store.event(db, rid, "conversation_branched", {"action": operation, "child_run_id": child["run_id"]})
    app.store.event(db, child["run_id"], "conversation_started", {
        "action": operation, "source_run_id": rid, "source_revision": run["revision"],
        "answer_refs": [t["answer_ref"] for t in turns],
    })
    return app.run(child["run_id"])
