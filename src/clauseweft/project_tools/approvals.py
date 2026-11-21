"""Review proposed calls and commit approved effects with their run receipt."""

import json

from clauseweft import audit

from .decisions import (
    TOOL_RISKS, TOOLS, arguments, checked_arguments, checked_text, is_read_only,
    read_decision, uses_reference,
)
from .references import search


def review(app, run):
    if "allowed_tools" in run:
        gate = next((n for n in run["nodes"] if n["kind"] == "gate" and n["state"] == "needs_input"), None)
        if not gate:
            return None
        proposal = next(n for n in run["nodes"] if n["node_id"] == gate["dependencies"][0])
        value = read_decision(app.store.read_blob(proposal["artifact"]))
        tool = value["tool_id"]
        return {"id": tool, "name": TOOLS[tool][0], "risk": TOOL_RISKS[tool],
                "arguments": checked_arguments(tool, value["arguments"]),
                "proposal_ref": proposal["artifact"], "gate_id": gate["node_id"], "node_id": proposal["node_id"]}
    proposal = next(node for node in run["nodes"] if node["node_id"] == "propose")
    result = {"id": run["tool_id"], "name": TOOLS[run["tool_id"]][0], "risk": TOOL_RISKS[run["tool_id"]],
              "proposal_ref": proposal["artifact"], "gate_id": "confirm", "node_id": "propose"}
    if proposal["artifact"]:
        try:
            result["arguments"] = arguments(run["tool_id"], app.store.read_blob(proposal["artifact"]))
        except (ValueError, OSError, RuntimeError) as exc:
            result["error"] = str(exc)
    return result


def _execute_tool(app, db, run, project, actor, tool, args, dynamic, source):
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


def auto_approve(app, run_id):
    """Automatically execute a pending read-only tool gate when policy allows."""
    with app.store.transaction() as db:
        row = db.execute("SELECT body, status FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if not row or row["status"] != "needs_input":
            return False
        body = json.loads(row["body"])
        if body.get("mode") != "tool" or body.get("tool_approval_policy") != "auto_read_only":
            return False
        run = app.platform.enrich_run(db, app.runs.get(run_id))
        evidence = review(app, run)
        if (not evidence or "error" in evidence or not is_read_only(evidence["id"])
                or not any(n["state"] == "needs_input" and n["node_id"] == evidence["gate_id"]
                           for n in run["nodes"])):
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
        tool = evidence["id"]
        args = evidence["arguments"]
        dynamic = "allowed_tools" in run
        details = dict(tool_id=tool, proposal_ref=evidence["proposal_ref"], node_id=evidence["gate_id"],
                       actor=actor["id"], project_id=project["id"],
                       reason="只读工具按分级策略自动执行", auto_approved=True)
        output = _execute_tool(app, db, run, project, actor, tool, args, dynamic, source)
        receipt = dict(details, arguments=args, result=output)
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
            # This pending gate has no executed receipt or successors yet.
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
    args = evidence["arguments"]
    output = _execute_tool(app, db, run, project, actor, tool, args, dynamic, source)
    receipt = dict(details, arguments=args, result=output)
    app.store.event(db, rid, "tool_executed", details)
    return app.runs.gate(rid, evidence["gate_id"], receipt)
