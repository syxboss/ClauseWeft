"""Create reproducible evaluations and report observations and blind grades."""

import json
import re

from clauseweft import evaluation
from .http_contracts import Problem, encode, ident, match, require_object


def begin_evaluation(app, db, data):
    require_object(
        data, ("mode", "dataset", "fixed_endpoint", "currency", "project_id", "workspace_id"), ("mode", "dataset")
    )
    dataset = evaluation.freeze_dataset(data["dataset"])
    mode = data["mode"]
    scope = {}
    if mode == "replay":
        result = evaluation.replay(dataset)
    elif mode == "paired":
        scope = app.admin_run_scope(db, data)
        currency = data.get("currency", "USD")
        if not isinstance(currency, str) or not re.fullmatch("[A-Z]{3}", currency):
            raise ValueError("预算币种无效")
        for item in dataset["items"]:
            snapshot = item.get("snapshot")
            if not isinstance(snapshot, dict) or not isinstance(
                snapshot.get("task"), dict
            ):
                # Missing inputs stay in the denominator. Neither arm receives a run.
                continue
            task = dict(snapshot["task"])
            limit = task.pop("budget_micros", None)
            task.pop("input_tokens", None)
            task.pop("pinned_endpoint", None)
            try:
                plan = app.plan(
                    {
                        **scope,
                        "task": task,
                        "budget": {"currency": currency, "limit_micros": limit},
                    }
                )
                item["snapshot"] = app.runs.snapshot(plan)
            except ValueError:
                # Keep an incomplete question in the denominator. Drop the client
                # model list so neither arm can dispatch from an unbound snapshot.
                item["snapshot"] = {"task": snapshot["task"]}

        def start(item, pin):
            snapshot = item["snapshot"]
            task = dict(snapshot["task"])
            limit = task.pop("budget_micros")
            task.pop("input_tokens")
            plan = app.plan(
                {
                    **scope,
                    "task": task,
                    "budget": {"currency": currency, "limit_micros": limit},
                }
            )
            plan["endpoint_revisions"] = {
                m["endpoint_id"]: m["revision"]
                for m in snapshot["models"]
                if m["allowed"] and m["endpoint_id"] in item["allowed_endpoints"]
            }
            return app.start(db, plan, {})["run_id"]

        result = evaluation.paired_run(dataset, data.get("fixed_endpoint"), start)
        result.update(scope, currency=currency)
    else:
        raise ValueError("mode 须为 replay 或 paired")
    eid = ident()
    db.execute(
        "INSERT INTO api_evaluations VALUES (?,?,?,?,1)",
        (eid, mode, encode(result), "{}"),
    )
    return 202, {"evaluation_id": eid, "revision": 1, "mode": mode, **scope}


def observations(app, db, experiment):
    observed = {}
    for item in experiment["items"]:
        for arm in item["arms"].values():
            rid = arm["run_id"]
            if rid is None:
                continue
            node = app.run(rid)["nodes"][0]
            attempts = list(
                db.execute(
                    """SELECT * FROM attempts
                WHERE run_id=? AND endpoint_id IS NOT NULL ORDER BY started,rowid""",
                    (rid,),
                )
            )
            state = node["state"]
            if (
                attempts
                and attempts[-1]["error"] == "upstream_timeout"
                and state in ("uncertain", "needs_input")
            ):
                state = "timed_out"
            elif attempts and state == "needs_input":
                state = "failed"
            elapsed = None
            if attempts and attempts[-1]["finished"] is not None:
                elapsed = max(
                    0,
                    round(
                        (attempts[-1]["finished"] - attempts[0]["started"]) * 1000
                    ),
                )
            artifact = (
                app.store.read_blob(node["artifact"]).decode()
                if node["artifact"]
                else None
            )
            observed[rid] = dict(
                executed=bool(attempts),
                state=state,
                elapsed_ms=elapsed,
                artifact=artifact,
                attempts=[
                    dict(
                        settled_micros=a["settled"],
                        pending=a["accounting"] != "settled" or a["hold"] > 0,
                    )
                    for a in attempts
                ],
            )
    return observed


def assessment(app, db, parts, query):
    row = db.execute(
        "SELECT * FROM api_evaluations WHERE id=?", (parts[1],)
    ).fetchone()
    if not row:
        raise Problem(404, "NOT_FOUND", "评测不存在")
    experiment = json.loads(row["payload"])
    path = parts[2:]
    if row["kind"] == "replay" and not path:
        result = experiment
    elif row["kind"] == "paired" and path in ((), ("blind",)):
        obs = app.observations(db, experiment)
        if path == ("blind",):
            result = {"items": evaluation.blind_items(experiment, obs)}
        else:
            result = evaluation.summarize(
                experiment, obs, json.loads(row["grades"])
            )
            result.update(
                dataset_version=experiment["version"],
                dataset_digest=experiment["dataset_digest"],
                ruleset=experiment["ruleset"],
                currency=experiment.get("currency", "USD"),
            )
            result.update({key: experiment[key] for key in ("project_id", "workspace_id") if key in experiment})
    else:
        raise Problem(404, "NOT_FOUND", "评测资源不存在")
    return dict(result, evaluation_id=parts[1], revision=row["revision"])


def grade(app, db, eid, expected, data):
    blind = app.assessment(db, ("evaluations", eid, "blind"), {})
    match(blind["revision"], expected)
    eligible = {item["blind_id"] for item in blind["items"]}
    if (
        not data
        or data.keys() - eligible
        or any(type(v) is not bool for v in data.values())
    ):
        raise ValueError("评分须对应已生成的匿名工件")
    grades = json.loads(
        db.execute(
            "SELECT grades FROM api_evaluations WHERE id=?", (eid,)
        ).fetchone()[0]
    )
    if grades.keys() & data.keys():
        raise Problem(409, "ALREADY_GRADED", "工件已有评分")
    grades.update(data)
    db.execute(
        "UPDATE api_evaluations SET grades=?,revision=revision+1 WHERE id=?",
        (encode(grades), eid),
    )
    return 200, app.assessment(db, ("evaluations", eid), {})
