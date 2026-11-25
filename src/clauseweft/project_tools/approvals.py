"""Review proposed calls (single, batch, or MCP) and commit approved effects with their run receipt."""

import json

from clauseweft import audit

from .decisions import (
    arguments, checked_arguments, checked_text, is_read_only, read_decision,
    tool_label, tool_risk, uses_reference,
)
from .mcp import call_mcp_tool, is_mcp_tool, parse_mcp_tool
from .references import search


def _batch_risk(calls, body):
    risks = [tool_risk(c["id"], body) for c in calls]
    if "external_effect" in risks:
        return "external_effect"
    if any(r != "read_only" for r in risks):
        return "mutating"
    return "read_only"


def review(app, run):
    if "allowed_tools" in run:
        gate = next((n for n in run["nodes"] if n["kind"] == "gate" and n["state"] == "needs_input"), None)
        if not gate:
            return None
        proposal = next(n for n in run["nodes"] if n["node_id"] == gate["dependencies"][0])
        value = read_decision(app.store.read_blob(proposal["artifact"]))
        if value.get("action") == "tools":
            calls = [
                {
                    "id": item["tool_id"],
                    "name": tool_label(item["tool_id"], run),
                    "risk": tool_risk(item["tool_id"], run),
                    "arguments": checked_arguments(item["tool_id"], item["arguments"], body=run),
                }
                for item in value["calls"]
            ]
            return {
                "id": "batch",
                "name": "并行工具调用（" + "、".join(c["name"] for c in calls) + "）",
                "risk": _batch_risk(calls, run),
                "calls": calls,
                "proposal_ref": proposal["artifact"],
                "gate_id": gate["node_id"],
                "node_id": proposal["node_id"],
            }
        tool = value["tool_id"]
        return {
            "id": tool, "name": tool_label(tool, run), "risk": tool_risk(tool, run),
            "arguments": checked_arguments(tool, value["arguments"], body=run),
            "proposal_ref": proposal["artifact"], "gate_id": gate["node_id"], "node_id": proposal["node_id"],
        }
    proposal = next(node for node in run["nodes"] if node["node_id"] == "propose")
    result = {
        "id": run["tool_id"], "name": tool_label(run["tool_id"], run),
        "risk": tool_risk(run["tool_id"], run),
        "proposal_ref": proposal["artifact"], "gate_id": "confirm", "node_id": "propose",
    }
    if proposal["artifact"]:
        try:
            result["arguments"] = arguments(run["tool_id"], app.store.read_blob(proposal["artifact"]), body=run)
        except (ValueError, OSError, RuntimeError) as exc:
            result["error"] = str(exc)
    return result


def _check_mcp_binding(app, run, tool_id):
    server_id, tool_name = parse_mcp_tool(tool_id)
    server = app.mcp_servers.get(server_id)
    digests = run.get("mcp_digests", {})
    if not server or server["digest"] != digests.get(server_id) or tool_name not in server["tools"]:
        raise RuntimeError("MCP 服务配置已变更，请重新发起工具任务")
    return server, tool_name


def _execute_tool(app, db, run, project, actor, tool, args, dynamic, source):
    if is_mcp_tool(tool):
        server, tool_name = _check_mcp_binding(app, run, tool)
        return call_mcp_tool(server, tool_name, args)
    if tool == "create_requirement":
        existing = None
        if dynamic:
            records = app.platform.list(db, "requirements", {
                "project_id": project["id"], "workspace_id": project["workspace_id"]}, actor)
            query = run.get("requirement_query")
            existing = next((item for item in records if (
                query.casefold() in (item["title"] + "\n" + item["description"]).casefold()
                if query else item["title"].casefold() == args["title"].casefold())), None)
        item = existing or app.platform.save(db, "requirements", dict(
            args, project_id=project["id"], workspace_id=project["workspace_id"]))
        output = {key: item[key] for key in ("id", "title", "description", "priority", "revision")}
        if dynamic:
            output["disposition"] = "existing" if existing else "created"
        return output
    if tool == "search_requirements":
        records = app.platform.list(db, "requirements", {
            "project_id": project["id"], "workspace_id": project["workspace_id"]}, actor)
        return {"query": args["query"], "items": search(
            (dict(item, text=item["description"]) for item in records), args["query"])}
    return {"source": source["name"], "source_sha256": source["digest"],
            "query": args["query"], "items": search(source["docs"].values(), args["query"])}


def _execute_evidence(app, db, run, project, actor, evidence, dynamic, source, details):
    if "calls" in evidence:
        executed = []
        for call in evidence["calls"]:
            out = _execute_tool(app, db, run, project, actor, call["id"], call["arguments"], dynamic, source)
            executed.append({"tool_id": call["id"], "arguments": call["arguments"], "result": out})
        return dict(details, calls=executed, results=[item["result"] for item in executed])
    args = evidence["arguments"]
    output = _execute_tool(app, db, run, project, actor, evidence["id"], args, dynamic, source)
    return dict(details, arguments=args, result=output)


def auto_approve(app, run_id):
    """Automatically execute a pending read-only tool gate (single or batch) when policy allows."""
    with app.store.transaction() as db:
        row = db.execute("SELECT body, status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or row["status"] != "needs_input":
            return False
        body = json.loads(row["body"])
        if body.get("mode") != "tool" or body.get("tool_approval_policy") != "auto_read_only":
            return False
        run = app.platform.enrich_run(db, app.runs.get(run_id))
        evidence = review(app, run)
        if (not evidence or "error" in evidence
                or not any(n["state"] == "needs_input" and n["node_id"] == evidence["gate_id"]
                           for n in run["nodes"])):
            return False
        if "calls" in evidence:
            if not all(is_read_only(c["id"], run) for c in evidence["calls"]):
                return False
        elif not is_read_only(evidence["id"], run):
            return False
        creator = run.get("created_by")
        try:
            actor = app.platform.public_actor(db, {"id": creator} if creator else app.actor())
            project = app.platform.authorize_project(db, run["project_id"], actor)
        except Exception:
            return False
        if project["status"] != "active" or not app.execution_authorized(body, actor):
            return False
        source = app.references.get(project["id"])
        if uses_reference(run) and (not source or source["digest"] != run.get("reference_digest")):
            return False
        dynamic = "allowed_tools" in run
        details = dict(tool_id=evidence["id"], proposal_ref=evidence["proposal_ref"], node_id=evidence["gate_id"],
                       actor=actor["id"], project_id=project["id"],
                       reason="只读工具按分级策略自动执行", auto_approved=True)
        try:
            receipt = _execute_evidence(app, db, run, project, actor, evidence, dynamic, source, details)
        except RuntimeError:
            return False
        app.store.event(db, run_id, "tool_auto_approved", details)
        app.store.event(db, run_id, "tool_executed", details)
        audit.record(db, audit.SYSTEM, "tool.auto_approved", f"runs/{run_id}/gates/{evidence['gate_id']}",
                     run_id=run_id, project_id=project["id"], details=details)
        app.runs.gate(run_id, evidence["gate_id"], receipt)
        return True


def decide(app, db, run, data):
    if run["mode"] != "tool" or run["status"] != "needs_input":
        raise RuntimeError("此运行没有等待确认的工具调用")
    evidence = review(app, run)
    if not evidence or not any(n["state"] == "needs_input" and n["node_id"] == evidence["gate_id"] for n in run["nodes"]):
        raise RuntimeError("工具调用尚未就绪或已经处理")
    if data.keys() != {"action", "proposal_ref", "reason"}:
        raise ValueError("工具决定须指定 action、proposal_ref 和 reason")
    if not evidence["proposal_ref"] or evidence["proposal_ref"] != data["proposal_ref"]:
        raise RuntimeError("工具提议已经变化，请重新查看")
    reason = checked_text(data["reason"], 2000, "处理说明")
    actor = app.actor()
    project = app.platform.authorize_project(db, run["project_id"], actor)
    tool = evidence["id"]
    dynamic = "allowed_tools" in run
    details = dict(tool_id=tool, proposal_ref=evidence["proposal_ref"], node_id=evidence["gate_id"],
                   actor=actor["id"], project_id=project["id"], reason=reason)
    action, rid = data["action"], run["run_id"]
    if action == "reject":
        app.store.event(db, rid, "tool_rejected", details)
        return app.runs.cancel(rid)
    if project["status"] != "active":
        raise RuntimeError("项目已归档")
    if action == "revise":
        app.store.event(db, rid, "tool_revision_requested", details)
        if dynamic:
            db.execute("DELETE FROM nodes WHERE run_id=? AND node_id=? AND state='needs_input'", (rid, evidence["gate_id"]))
        return app.runs.invalidate(rid, evidence["node_id"], feedback=reason)
    if action != "approve":
        raise ValueError("未知的工具处理决定")
    if not app.execution_authorized(run):
        raise RuntimeError("任务发起人当前已失去执行授权")
    if "error" in evidence:
        raise ValueError(evidence["error"])
    source = app.references.get(project["id"])
    if uses_reference(run) and (not source or source["digest"] != run.get("reference_digest")):
        raise RuntimeError("项目资料源已变更，请重新发起工具任务")
    receipt = _execute_evidence(app, db, run, project, actor, evidence, dynamic, source, details)
    app.store.event(db, rid, "tool_executed", details)
    return app.runs.gate(rid, evidence["gate_id"], receipt)
