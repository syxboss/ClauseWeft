"""Queue delivery jobs and publish Git integration or isolated test evidence."""

import hashlib
import json
import time

from clauseweft import audit, requirements, reviews, testing
from clauseweft.workspace.credentials import redact

from .delivery_evidence import export_record, nodes, output
from .http_contracts import encode, ident


def format_test_feedback(report, round_num, max_rounds):
    lines = [
        f"自动验收测试未通过（第 {round_num}/{max_rounds} 轮，状态：{report.get('status')}）。"
        "请根据以下真实测试失败证据修复代码并仅修改允许路径："
    ]
    if report.get("reason"):
        lines.append(f"原因：{report['reason']}")
    for case in report.get("cases", []):
        if case.get("status") != "passed":
            log = (case.get("log") or "").strip()[:800]
            line = (
                f"- 用例 {case.get('id')}（条款 {case.get('clause_id')}）："
                f"状态={case.get('status')}，退出码={case.get('exit_code')}"
            )
            if case.get("reason"):
                line += f"，原因={case['reason']}"
            if log:
                line += f"\n  输出日志：{log}"
            lines.append(line)
    return "\n".join(lines)[:3000]


def job_event(
    app,
    db,
    run_id,
    event,
    job_id,
    kind,
    payload,
    ctx,
    *,
    result=None,
    reason=None,
    state=None,
):
    context = {key: value for key, value in ctx.items() if key != "files"}
    context["files"] = [
        {key: file[key] for key in ("path", "sha256")}
        for file in ctx.get("files", [])
    ]
    evidence = audit.sanitize(
        dict(
            id=job_id,
            kind=kind,
            generation=payload.get("gen"),
            spec_version_id=payload.get("version"),
            candidate_sha=payload.get("candidate_sha"),
            input=payload,
            context=context,
            result=result,
            reason=reason,
            state=state,
        )
    )
    if result is not None:
        evidence["result_sha256"] = hashlib.sha256(
            encode(evidence["result"]).encode()
        ).hexdigest()
    app.store.event(db, run_id, event, evidence)
    # Preserve the originating request even after a different worker takes the job.
    audit.record(
        db,
        audit.SYSTEM,
        event,
        "runs/" + run_id + "/jobs/" + job_id,
        outcome="failed"
        if state == "failed"
        else "stale"
        if state == "stale"
        else "uncertain"
        if state == "interrupted"
        else "succeeded",
        run_id=run_id,
        project_id=app.run(run_id).get("project_id"),
        after=evidence,
        correlation_id=payload.get("request_correlation_id"),
        details={"requested_by": payload.get("requested_by"), "kind": kind},
    )


def enqueue(delivery, db, run, kind, payload):
    rid = run["run_id"]
    if db.execute(
        "SELECT 1 FROM api_jobs WHERE run_id=? AND kind=? AND state IN ('queued','running')",
        (rid, kind),
    ).fetchone():
        raise RuntimeError("该操作仍在执行")
    job = ident()
    payload = dict(
        payload,
        requested_by=delivery.app.actor(),
        request_correlation_id=getattr(
            delivery.app.request_context, "correlation_id", None
        ),
    )
    db.execute(
        "INSERT INTO api_jobs(id,run_id,kind,state,input) VALUES (?,?,?,'queued',?)",
        (job, rid, kind, encode(payload)),
    )
    delivery.job_event(
        db,
        rid,
        "delivery_queued",
        job,
        kind,
        payload,
        delivery.app.context(db, rid),
        state="queued",
    )
    return 202, {
        "id": job,
        "status": "queued",
        "revision": delivery.app.run(rid)["revision"],
    }


def _job_current(delivery, db, job, payload, ctx, expected_state):
    rid, kind, job_id = job["run_id"], job["kind"], job["id"]
    run = delivery.app.run(rid)
    body = json.loads(db.execute("SELECT body FROM runs WHERE run_id=?", (rid,)).fetchone()[0])
    spec = requirements.current(db, rid)
    state = db.execute(
        "SELECT state FROM api_jobs WHERE id=?", (job_id,)
    ).fetchone()[0]
    return (
        state == expected_state
        and delivery.app.execution_authorized(body, payload.get("requested_by"))
        and run["status"] != "cancelled"
        and spec
        and spec["approved"]
        and spec["version"] == payload["version"]
        and nodes(run)[kind]["generation"] == payload["gen"]
        and (
            kind != "execute-tests"
            or delivery.current_environment(ctx, spec, payload["suite"])
        )
    )


def _integrate(delivery, workspace, ctx, payload):
    store = delivery.app.store
    patches = []
    work_packages = []
    for name in payload["selected"]:
        sha = payload["inputs"][name]
        data = output(store.read_blob(sha).decode())
        patch = workspace.stage_patch(
            ctx["base_sha"], data["patch"], paths=ctx["scopes"].get(name)
        )
        patches.append(patch["patch_id"])
        work_packages.append(dict(node_id=name, artifact=sha, **patch))
    if "resolution_patch" in payload:
        # A resolution may only edit paths granted to the selected work packages.
        allowed = delivery.repositories[ctx["repo_id"]]["allowlist"]
        permitted = sorted(
            {
                path
                for name in payload["selected"]
                for path in ctx["scopes"].get(name, allowed)
            }
        )
        resolved = workspace.stage_patch(
            ctx["base_sha"], payload["resolution_patch"], paths=permitted
        )
        candidate = workspace.integrate(ctx["base_sha"], [resolved["patch_id"]])
        candidate["source_patch_ids"] = patches
    else:
        candidate = workspace.integrate(ctx["base_sha"], patches)
    return dict(
        candidate,
        version=payload["version"],
        selected=payload["selected"],
        work_packages=work_packages,
        reason=payload["reason"],
    )


def work_once(delivery):
    store = delivery.app.store
    with store.transaction() as db:
        row = db.execute(
            """SELECT n.run_id FROM nodes n
            WHERE n.state='succeeded' AND n.node_id IN ('draft','review-product','review-test','code-review')
            AND NOT EXISTS (SELECT 1 FROM api_imports i WHERE i.run_id=n.run_id AND i.node=n.node_id
            AND i.generation=n.generation AND i.artifact=n.artifact)
            AND EXISTS (SELECT 1 FROM runs r WHERE r.run_id=n.run_id AND r.status!='cancelled'
                        AND json_extract(r.body,'$.mode')='workflow')
            LIMIT 1"""
        ).fetchone()
        if row:
            delivery.scan_results(db, delivery.app.run(row[0]))
        job = db.execute(
            """SELECT j.* FROM api_jobs j JOIN runs r USING(run_id)
               WHERE j.state='queued' AND r.status!='paused' ORDER BY j.rowid LIMIT 1"""
        ).fetchone()
        if not job:
            return False
        rid, kind, job_id = job["run_id"], job["kind"], job["id"]
        payload = json.loads(job["input"])
        ctx = delivery.app.context(db, rid)

        if not _job_current(delivery, db, job, payload, ctx, "queued"):
            stale = {
                "status": "stale",
                "reason": "需求、候选或执行环境已变化，任务未启动",
            }
            db.execute(
                "UPDATE api_jobs SET state='stale',result=?,reason=? WHERE id=?",
                (encode(stale), stale["reason"], job_id),
            )
            delivery.job_event(
                db,
                rid,
                "delivery_job_stale",
                job_id,
                kind,
                payload,
                ctx,
                result=stale,
                reason=stale["reason"],
                state="stale",
            )
            return True
        db.execute(
            "UPDATE api_jobs SET state='running', started=? WHERE id=?",
            (time.time(), job_id),
        )
        candidate = (
            delivery.gate_artifact(delivery.app.run(rid), "integrate")
            if kind == "execute-tests"
            else None
        )
        delivery.job_event(
            db,
            rid,
            "delivery_job_started",
            job_id,
            kind,
            payload,
            ctx,
            result={"candidate": candidate} if candidate else None,
            state="running",
        )
    result = None
    try:
        ws = delivery.workspace(ctx)
        if kind == "integrate":
            result = _integrate(delivery, ws, ctx, payload)
            # Reject credentials before publishing a patch for review.
            export_record(result)
        else:
            result = testing.start(
                candidate, payload["suite"], ws.files(candidate["candidate_sha"])
            )
        with store.transaction() as db:
            current = _job_current(delivery, db, job, payload, ctx, "running")
            state = "succeeded" if current else "stale"
            reason = None
            if (
                current
                and kind == "execute-tests"
                and not testing.verify(result, candidate, payload["suite"])
            ):
                state = "failed"
                reason = result.get("reason") or result.get("status", "未通过")
            if state == "succeeded":
                delivery.app.runs.gate(rid, kind, result)
                if kind == "integrate":
                    reviews.set_candidate(
                        db,
                        rid,
                        payload["version"],
                        result["candidate_sha"],
                        generation=payload["gen"],
                    )
            db.execute(
                "UPDATE api_jobs SET state=?,result=?,reason=? WHERE id=?",
                (state, encode(result), reason, job_id),
            )
            delivery.job_event(
                db,
                rid,
                "delivery_job_finished",
                job_id,
                kind,
                payload,
                ctx,
                result=result,
                reason=reason,
                state=state,
            )
            if (
                current
                and kind == "execute-tests"
                and state == "failed"
                and result.get("status") != "needs_configuration"
            ):
                _maybe_auto_rework(delivery, db, rid, job_id, result, reason)
    except Exception as exc:
        detail = (
            redact(str(exc))
            if isinstance(exc, (RuntimeError, ValueError))
            else type(exc).__name__
        )
        failed = {
            "status": "failed",
            "reason": detail[:500],
            "error_type": type(exc).__name__,
            "partial_result": result,
        }
        with store.transaction() as db:
            db.execute(
                "UPDATE api_jobs SET state='failed',reason=?,result=? WHERE id=?",
                (detail[:500], encode(failed), job_id),
            )
            delivery.job_event(
                db,
                rid,
                "delivery_job_failed",
                job_id,
                kind,
                payload,
                ctx,
                result=failed,
                reason=detail[:500],
                state="failed",
            )
    return True


def _maybe_auto_rework(delivery, db, rid, job_id, report, reason):
    run_row = db.execute("SELECT body, status FROM runs WHERE run_id=?", (rid,)).fetchone()
    if not run_row or run_row["status"] in ("cancelled", "paused"):
        return False
    body = json.loads(run_row["body"])
    max_rounds = body.get("max_fix_rounds", 0)
    if type(max_rounds) is not int or max_rounds < 1:
        return False
    used = db.execute(
        "SELECT count(*) FROM events WHERE run_id=? AND kind='delivery_auto_rework'",
        (rid,),
    ).fetchone()[0]
    if used >= max_rounds:
        return False
    run = delivery.app.run(rid)
    run_nodes = nodes(run)
    code_node = run_nodes.get("code")
    if not code_node or code_node["state"] != "succeeded":
        return False
    budget = run["budget"]
    if budget["limit_micros"] > 0 and budget["available_micros"] <= 0:
        return False
    feedback = format_test_feedback(report, used + 1, max_rounds)
    delivery.app.runs.invalidate(rid, "code", feedback=feedback)
    reviews.invalidate_candidate(db, rid)
    details = {
        "round": used + 1,
        "max_fix_rounds": max_rounds,
        "job_id": job_id,
        "node_id": "code",
        "reason": reason,
        "feedback": feedback,
    }
    delivery.app.store.event(db, rid, "delivery_auto_rework", details)
    audit.record(
        db,
        audit.SYSTEM,
        "delivery.auto_rework",
        f"runs/{rid}/rework",
        run_id=rid,
        project_id=run.get("project_id"),
        details=details,
    )
    return True
