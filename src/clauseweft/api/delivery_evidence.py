"""Import, inspect and sanitize the evidence for each delivery run."""

import json

from clauseweft import audit, requirements, reviews
from clauseweft.workspace.credentials import FIELD, redact

from .http_contracts import Problem

SPEC_REVIEWERS = ("review-product", "review-test")
REVIEW_NODES = (*SPEC_REVIEWERS, "code-review")


def nodes(run):
    return {node["node_id"]: node for node in run["nodes"]}


def output(value):
    text = value.strip()
    if text.startswith("```json\n") and text.endswith("\n```"):
        text = text[8:-4]
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("模型交付物须为 JSON 对象")
    return data


def export_record(value):
    """Remove credentials from exported free text. Preserve replayable patch bytes exactly."""
    if isinstance(value, dict):
        result = {}
        for key, field in value.items():
            if FIELD.fullmatch(key):
                result[key] = "[redacted]"
            else:
                result[key] = export_record(field)
                if key == "patch" and result[key] != field:
                    raise RuntimeError("候选补丁含凭据，请返工以生成可安全导出的差异")
        return result
    if isinstance(value, list):
        return [export_record(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    return value


def scan_results(store, db, run):
    rid = run["run_id"]
    if run["mode"] != "workflow" or run["status"] == "cancelled":
        return
    for key in ("draft", *REVIEW_NODES):
        node = nodes(run)[key]
        if node["state"] != "succeeded" or not node["artifact"]:
            continue
        stamp = (rid, key, node["generation"], node["artifact"])
        if db.execute(
            "SELECT 1 FROM api_imports WHERE run_id=? AND node=? AND generation=? AND artifact=?",
            stamp,
        ).fetchone():
            continue
        err = None
        parsed = None
        imported = None
        before = requirements.current(db, rid)
        try:
            # Roll back the entire import if a later finding is malformed.
            with store.transaction():
                parsed = output(store.read_blob(node["artifact"]).decode())
                spec = requirements.current(db, rid)
                if key == "draft":
                    if not spec:
                        imported = requirements.revise(
                            db,
                            rid,
                            None,
                            parsed["clauses"],
                            "模型起草",
                            node["model_identity"] or audit.SYSTEM["id"],
                            SPEC_REVIEWERS,
                        )
                else:
                    target = (
                        reviews.candidate(db, rid) if key == "code-review" else spec
                    )
                    if target is None:
                        continue
                    imported = reviews.submit(
                        db,
                        rid,
                        target.get("target") or target["version"],
                        key,
                        node["endpoint_id"],
                        node["model_identity"],
                        parsed["findings"],
                    )
        except (ValueError, TypeError, KeyError) as exc:
            err = f"{key} 工件格式或评审数据无效 ({type(exc).__name__})"
        db.execute("INSERT INTO api_imports VALUES (?,?,?,?,?)", (*stamp, err))
        store.event(
            db,
            rid,
            "artifact_checked",
            {
                "node": key,
                "error": err,
                "generation": node["generation"],
                "artifact": node["artifact"],
                "imported_target": (
                    imported.get("target") or imported.get("version")
                )
                if imported
                else None,
            },
        )
        model = (
            {
                "id": "model:" + node["model_identity"],
                "display_name": node["model_identity"],
            }
            if node["model_identity"]
            else audit.SYSTEM
        )
        audit.record(
            db,
            model,
            "delivery.artifact_checked",
            "runs/" + rid + "/nodes/" + key,
            outcome="failed" if err else "succeeded",
            run_id=rid,
            project_id=run.get("project_id"),
            before=before,
            after=imported,
            details={
                "node_id": key,
                "generation": node["generation"],
                "artifact": node["artifact"],
                "model_identity": node["model_identity"],
                "endpoint_id": node["endpoint_id"],
                "parsed": parsed,
                "error": err,
                "applied": imported is not None,
            },
        )


def detail(app, db, run):
    rid = run["run_id"]
    if run["mode"] == "workflow":
        spec = requirements.current(db, rid)
        candidate = reviews.candidate(db, rid)
        run = dict(
            run,
            spec_version_id=spec["version"] if spec else None,
            candidate_sha=candidate["candidate_sha"] if candidate else None,
            baseline_approved=bool(spec and spec["approved"]),
            suite_id=app.context(db, rid).get("suite_id"),
            jobs=[
                dict(row)
                for row in db.execute(
                    "SELECT id,kind,state,reason FROM api_jobs WHERE run_id=? ORDER BY rowid",
                    (rid,),
                )
            ],
        )
        active = nodes(run)
        for row in db.execute(
            "SELECT * FROM api_imports WHERE run_id=? AND error IS NOT NULL", (rid,)
        ):
            n = active.get(row["node"])
            if n and (n["generation"], n["artifact"]) == (
                row["generation"],
                row["artifact"],
            ):
                run["waiting_actions"].append(
                    {
                        "reason": row["error"],
                        "actor": "owner",
                        "action": "修订需求或重新运行该评审节点",
                        "node_id": row["node"],
                    }
                )
    return run


def artifact(store, db, run, aid):
    rid = run["run_id"]
    if (
        any(n["artifact"] == aid for n in run["nodes"])
        or db.execute(
            "SELECT 1 FROM attempts WHERE run_id=? AND artifact=?", (rid, aid)
        ).fetchone()
    ):
        return {"id": aid, "content": store.read_blob(aid).decode()}
    row = db.execute(
        "SELECT result FROM api_jobs WHERE run_id=? AND id=?", (rid, aid)
    ).fetchone()
    if row and row["result"]:
        return {"id": aid, "content": json.loads(row["result"])}
    raise Problem(404, "ARTIFACT_NOT_FOUND", "该运行中无此工件")


def gate_artifact(store, run, key):
    node = nodes(run)[key]
    if node["state"] != "succeeded" or not node["artifact"]:
        raise RuntimeError(f"尚缺有效的 {key} 工件")
    return json.loads(store.read_blob(node["artifact"]))


def has_import_error(db, run, key):
    node = nodes(run)[key]
    return db.execute(
        "SELECT 1 FROM api_imports WHERE run_id=? AND node=? AND generation=? AND artifact=? AND error IS NOT NULL",
        (run["run_id"], key, node["generation"], node["artifact"]),
    ).fetchone()
