"""Read and update a run's graph and ledger in the caller's transaction."""

import json

from clauseweft.storage import canonical


def load_run(db, run_id):
    row = db.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
    if row is None:
        raise KeyError(run_id)
    return row


def load_nodes(db, run_id):
    return {
        r["node_id"]: dict(r, definition=json.loads(r["definition"]))
        for r in db.execute("SELECT * FROM nodes WHERE run_id=? ORDER BY rowid", (run_id,))
    }


def input_refs(n, nodes):
    return {k: nodes[k]["artifact"] for k in n["definition"]["dependencies"]}


def insert_nodes(db, run_id, definitions):
    for n in definitions:
        db.execute(
            "INSERT INTO nodes(run_id,node_id,definition,state) VALUES(?,?,?,?)",
            (run_id, n["node_id"], canonical(n),
             "waiting" if n["dependencies"] or n["kind"] == "gate" else "ready"),
        )


def budget_balance(db, run):
    key = (run["run_id"],)
    ledger = db.execute("SELECT hold, settled FROM attempts WHERE run_id=?", key).fetchall()
    hold = sum(row["hold"] for row in ledger)
    settled = sum(row["settled"] for row in ledger)
    pending = [
        r[0]
        for r in db.execute(
            "SELECT attempt_id FROM attempts WHERE run_id=? AND accounting='uncertain'", key
        )
    ]
    return dict(
        currency=run["currency"],
        limit_micros=run["limit_micros"],
        held_micros=hold,
        settled_micros=settled,
        available_micros=run["limit_micros"] - hold - settled,
        uncertain_attempts=pending,
    )


def describe_run(db, run_id):
    run = load_run(db, run_id)
    body = json.loads(run["body"])
    role_info = {}
    if role := body.get("role_snapshot"):
        role_info.update(role_id=role["id"], role_name=role["name"], role_revision=role["revision"])
    nodes = []
    waiting = []
    for n in load_nodes(db, run_id).values():
        attempt = db.execute(
            "SELECT endpoint_id,model_identity FROM attempts WHERE attempt_id=?", (n["attempt_id"],)
        ).fetchone()
        item = {
            **n["definition"],
            **{
                k: n[k]
                for k in ("state", "generation", "input_digest", "artifact", "reason", "attempt_id")
            },
        }
        item.pop("prompt", None)
        item.pop("feedback", None)
        item.pop("decision_feedback", None)
        item.update(
            endpoint_id=attempt[0] if attempt else None,
            model_identity=attempt[1] if attempt else None,
        )
        nodes.append(item)
        if item["state"] in ("needs_input", "uncertain"):
            waiting.append(
                {
                    "node_id": item["node_id"],
                    "reason": item["reason"],
                    "actor": "owner",
                    "action": "reconcile"
                    if item["state"] == "uncertain"
                    else (item["node_id"] if item["kind"] == "gate" else "resume"),
                }
            )
    compaction = body.get("conversation", {}).get("compaction")
    return dict(
        run_id=run_id,
        **({k: body[k] for k in ("tool_id", "allowed_tools", "max_tool_steps", "requirement_query",
                                  "reference_digest", "tool_approval_policy", "max_fix_rounds",
                                  "mcp_tools", "mcp_digests", "memory_snapshots") if k in body}),
        **({"conversation_compaction": compaction} if compaction else {}),
        title=body.get("title") or body["task"]["text"].splitlines()[0][:120],
        project_id=body.get("project_id", "default"),
        requirement_id=body.get("requirement_id"),
        template_id=body.get("template_id"),
        template_revision=body.get("template_snapshot", {}).get("revision"),
        template_name=body.get("template_snapshot", {}).get("name"),
        source_run_id=body.get("source_run_id"),
        checkpoint_id=body.get("checkpoint_id"),
        budget_source=body.get("budget_source", "legacy"),
        revision=run["revision"],
        status=run["status"],
        mode=body["mode"],
        plan_revision=body.get("template_snapshot", {}).get("revision", 1),
        spec_version_id=run["spec_version_id"],
        base_sha=body.get("base_sha"),
        candidate_sha=run["candidate_sha"],
        nodes=nodes,
        budget=budget_balance(db, run),
        created=run["created"],
        waiting_actions=waiting,
        **role_info,
        events_after=db.execute("SELECT max(seq) FROM events WHERE run_id=?", (run_id,)).fetchone()[
            0
        ],
    )


def refresh(db, run_id):
    if load_run(db, run_id)["status"] in ("cancelled", "paused"):
        return
    nodes = load_nodes(db, run_id)
    for n in nodes.values():
        if n["state"] == "waiting" and all(
            nodes[k]["state"] == "succeeded" for k in n["definition"]["dependencies"]
        ):
            gate = n["definition"]["kind"] == "gate"
            n["state"] = "needs_input" if gate else "ready"
            db.execute(
                "UPDATE nodes SET state=?,reason=? WHERE run_id=? AND node_id=?",
                (n["state"], "awaiting_" + n["node_id"] if gate else None, run_id, n["node_id"]),
            )
    states = {n["state"] for n in nodes.values()}
    if states == {"succeeded"}:
        status = "completed"
    elif states & {"leased", "dispatched"}:
        status = "running"
    elif "ready" in states:
        status = "queued"
    else:
        status = "needs_input"
    db.execute("UPDATE runs SET status=? WHERE run_id=?", (status, run_id))
