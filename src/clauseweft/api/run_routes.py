"""Read run resources and apply versioned actions to a selected run."""

import json

from clauseweft import audit, project_tools, requirements, reviews
from clauseweft.evaluation import requirement_goal
from . import conversations
from .http_contracts import Problem, match, page, require_object


def list_runs(app, db, q):
    start, limit = int(q.get("cursor", 0)), int(q.get("limit", 50))
    if start < 0 or not 1 <= limit <= 500:
        raise ValueError("分页参数无效")
    search = q.get("q", "").strip()
    allowed = sorted(app.platform.allowed_project_ids(db, app.actor(), q.get("workspace_id") or "default"))
    if not allowed:
        return {"items": [], "next_cursor": None}
    placeholders = ",".join("?" for _ in allowed)
    rows = list(
        db.execute(
            f"""SELECT r.run_id FROM runs r JOIN platform_run_links l USING(run_id)
                 WHERE (? IS NULL OR r.status=?) AND (? IS NULL OR l.project_id=?)
                 AND (? IS NULL OR l.requirement_id=?)
                 AND l.project_id IN ({placeholders})
                 AND (?='' OR instr(lower(l.title),lower(?))>0
                      OR instr(lower(json_extract(r.body,'$.task.text')),lower(?))>0)
                 ORDER BY r.created DESC,r.run_id LIMIT ? OFFSET ?""",
            (
                q.get("status"),
                q.get("status"),
                q.get("project_id"),
                q.get("project_id"),
                q.get("requirement_id"),
                q.get("requirement_id"),
                *allowed,
                search, search, search,
                limit + 1,
                start,
            ),
        )
    )
    return {
        "items": [app.run(row[0]) for row in rows[:limit]],
        "next_cursor": start + limit if len(rows) > limit else None,
    }


def read(app, db, rid, path, q):
    run = app.run(rid)
    if path == ("context",):
        return app.harness.contexts(db, run)
    if path == ("checkpoints",):
        return page(app.harness.checkpoints(db, rid), q)
    if path == ("goal-assessment",):
        require_object(q, ("scenario", "query", "title", "workspace_id", "project_id"), ("scenario", "query", "title"))
        scope = {"project_id": run["project_id"], "workspace_id": run["workspace_id"]}
        if q.get("project_id", run["project_id"]) != run["project_id"]:
            raise Problem(403, "PROJECT_FORBIDDEN", "运行不属于所选项目")
        records = app.platform.list(db, "requirements", scope)
        decisions = [(row["kind"], json.loads(row["data"])) for row in db.execute(
            "SELECT kind,data FROM events WHERE run_id=? AND kind IN ('tool_executed','tool_rejected') ORDER BY seq", (rid,))]
        return requirement_goal.assess(run, q, records, decisions, app.store.read_blob)
    if not path:
        return app.detail(db, run)
    if path == ("events",):
        after, limit = int(q.get("after", 0)), int(q.get("limit", 100))
        if after < 0 or not 1 <= limit <= 500:
            raise ValueError("事件游标或数量无效")
        events = app.store.events_after(rid, after, limit)
        return {
            "items": events,
            "after": events[-1]["seq"] if events else after,
        }
    if path == ("attempts",):
        rows = db.execute(
            """SELECT attempt_id,node_id,generation,state,started,finished,endpoint_id,model_identity,
                           endpoint_revision,receipt,error,hold,settled,accounting,artifact,decision,context_ref,pricing_snapshot,usage,completion_metadata FROM attempts
                           WHERE run_id=? ORDER BY started,attempt_id""",
            (rid,),
        )
        return page(
            [
                dict(
                    r,
                    decision=json.loads(r["decision"])
                    if r["decision"]
                    else None,
                    pricing_snapshot=json.loads(r["pricing_snapshot"]) if r["pricing_snapshot"] else None,
                    usage=json.loads(r["usage"]) if r["usage"] else None,
                    completion_metadata=json.loads(r["completion_metadata"]) if r["completion_metadata"] else None,
                )
                for r in rows
            ],
            q,
        )
    if path == ("specs",):
        return page(requirements.list_specs(db, rid), q)
    if path == ("findings",):
        return page(reviews.finding_records(db, rid), q)
    if len(path) == 2 and path[0] == "artifacts":
        return app.delivery.artifact(db, run, path[1])
    raise Problem(404, "NOT_FOUND", "资源不存在")


def detail(app, db, run):
    result = app.delivery.detail(db, run)
    if run["mode"] == "tool":
        result["tool_proposal"] = project_tools.review(app, run)
    body = json.loads(db.execute("SELECT body FROM runs WHERE run_id=?", (run["run_id"],)).fetchone()[0])
    result["task"] = audit.sanitize(body.get("task", {}))
    result["continuation_actions"] = conversations.actions_for(run, body)
    if body.get("conversation"):
        result["conversation"] = audit.sanitize(body["conversation"])
    result["budget_source"] = body.get("budget_source", "legacy")
    return result


def _decide_gate(app, run, node_id, data):
    rid = run["run_id"]
    require_object(data, ("action", "reason"), ("action", "reason"))
    if run["mode"] != "sop":
        raise ValueError("研发交付流程须通过专用审批与实测接口")
    node = next(
        (node for node in run["nodes"] if node["node_id"] == node_id), None
    )
    if (
        node is None
        or node["kind"] != "gate"
        or node["state"] != "needs_input"
    ):
        raise RuntimeError("当前节点没有等待审批")
    if not isinstance(data["reason"], str) or not data["reason"].strip():
        raise ValueError("请填写审批依据")
    if data["action"] == "approve":
        app.runs.gate(
            rid,
            node_id,
            {
                "decision": "approved",
                "actor": app.actor()["id"],
                "reason": data["reason"],
            },
        )
    elif data["action"] == "reject":
        for dependency in node["dependencies"]:
            app.runs.invalidate(rid, dependency, feedback=data["reason"])
        if not node["dependencies"]:
            raise ValueError("没有可退回的上游节点")
    else:
        raise ValueError("审批动作须为 approve 或 reject")
    return 200, app.run(rid)


def write_action(app, db, rid, path, data, expected):
    run = app.run(rid)
    match(run["revision"], expected)
    if path == ("continuations",):
        return 201, conversations.create(app, db, run, data)
    if path == ("tool-decisions",):
        return 200, project_tools.decide(app, db, run, data)
    if path == ("checkpoints",):
        require_object(data, ("label", "reason"), ("label", "reason"))
        return 201, app.harness.checkpoint(
            db, run, data["label"], data["reason"], app.actor()
        )
    if path == ("forks",):
        require_object(
            data,
            ("checkpoint_id", "title", "reason", "budget"),
            ("checkpoint_id", "title", "reason", "budget"),
        )
        return 201, app.harness.fork(
            db,
            run,
            data["checkpoint_id"],
            data["title"],
            data["reason"],
            data["budget"],
            app.actor(),
        )
    if path == ("cancel",):
        return 200, app.runs.cancel(rid)
    if path == ("resume",):
        return 200, app.runs.resume(rid)
    if path == ("pause",):
        return 200, app.runs.pause(rid)
    if len(path) == 2 and path[0] == "gates":
        return _decide_gate(app, run, path[1], data)
    if path == ("reconciliations",):
        require_object(
            data,
            ("attempt_id", "actual_micros", "evidence"),
            ("attempt_id", "actual_micros", "evidence"),
        )
        return 200, app.runs.reconcile(
            rid,
            data["attempt_id"],
            data["actual_micros"],
            app.actor()["id"],
            data["evidence"],
        )
    return app.delivery.write(db, run, path, data)
