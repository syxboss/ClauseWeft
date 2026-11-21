"""Persist the selected route, input evidence and budget hold before giving a worker permission to call a model."""

import json
import time

from clauseweft import workflow
from clauseweft.routing.preview import preview as route
from clauseweft.storage import canonical, digest
from clauseweft.usage import pricing_snapshot

from .state import budget_balance, input_refs, load_nodes, load_run, refresh
from .leases import LEASE_SECONDS, expire
from .requests import request_messages, route_snapshot


def _unlease(store, db, row, state, reason, decision=None, snapshot=None):
    db.execute(
        "UPDATE attempts SET state='blocked',error=?,finished=?,decision=?,snapshot=? WHERE attempt_id=?",
        (reason, time.time(), canonical(decision), canonical(snapshot), row["attempt_id"]),
    )
    delay = 0.25 if state == "ready" else 0
    db.execute(
        "UPDATE nodes SET state=?,reason=?,ready_at=? WHERE attempt_id=?",
        (state, reason, time.time() + delay, row["attempt_id"]),
    )
    store.event(db, row["run_id"], "dispatch_blocked", {"node_id": row["node_id"], "reason": reason})
    refresh(db, row["run_id"])


def _choose_endpoint(db, key, nodes, prepared, models, task, account, frozen):
    """Route current peers together, respecting occupied identities and rate-limit history."""
    group = nodes[key]["definition"]["group"]
    run_id = nodes[key]["run_id"]
    choices, occupied, decisions, snapshots = {}, set(), {}, {}
    for k in prepared:
        peer = nodes[k]
        previous = db.execute(
            "SELECT model_identity FROM attempts WHERE attempt_id=?", (peer["attempt_id"],)
        ).fetchone()
        if k != key and peer["state"] in {"succeeded", "dispatched", "uncertain"} and previous:
            occupied.add(previous[0])
            continue
        snapshot = route_snapshot(
            task,
            peer["definition"],
            prepared[k],
            models.values(),
            account["available_micros"],
            account["currency"],
            frozen,
        )
        decision = route(snapshot)
        rejected = {
            r[0]
            for r in db.execute(
                """SELECT endpoint_id FROM attempts WHERE run_id=? AND node_id=?
                      AND generation=? AND error='upstream_rate_limited'""",
                (run_id, k, peer["generation"]),
            )
        }
        eligible = [
            c
            for c in decision.get("candidates", [])
            if not c["reasons"] and (not group or models[c["endpoint_id"]]["identity_verified"])
        ]
        untried = [c for c in eligible if c["endpoint_id"] not in rejected]
        choices[k] = untried or eligible
        if rejected:
            decision["rate_limited_endpoints"] = sorted(rejected)
        if group:
            decision["independent_group"] = group
        decisions[k], snapshots[k] = decision, snapshot
    if group:
        candidate = workflow.allocate(choices, occupied).get(key)
    else:
        candidate = choices[key][0] if choices[key] else None
    return candidate, decisions[key], snapshots[key]


def dispatch_attempt(store, attempt_id, *, models_for, task_for):
    # Capture immutable input references first. Read files outside the write transaction.
    with store.transaction() as db:
        att = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if att is None:
            raise KeyError(attempt_id)
        run_id, key = att["run_id"], att["node_id"]
        run = load_run(db, run_id)
        body = json.loads(run["body"])
        nodes = load_nodes(db, run_id)
        current = nodes[key]
        if current["state"] != "leased" or current["attempt_id"] != attempt_id:
            return None
        group = current["definition"]["group"]
        peers = {
            k: n for k, n in nodes.items() if k == key or (group and n["definition"]["group"] == group)
        }
        refs = {k: input_refs(n, nodes) for k, n in peers.items()}
    try:
        prepared = {
            k: request_messages(store, body, n, refs[k], nodes) for k, n in peers.items()
        }
    except (OSError, UnicodeError, RuntimeError):
        with store.transaction() as db:
            current = load_nodes(db, run_id)[key]
            if current["attempt_id"] == attempt_id and current["state"] == "leased":
                _unlease(store, db, att, "needs_input", "invalid_input_artifact")
        return None
    with store.transaction() as db:
        expire(store, db)
        nodes = load_nodes(db, run_id)
        if (
            nodes[key]["attempt_id"] != attempt_id
            or nodes[key]["state"] != "leased"
            or any(refs[k] != input_refs(nodes[k], nodes) for k in peers)
        ):
            return None
        if load_run(db, run_id)["status"] == "paused":
            _unlease(store, db, att, "ready", "run_paused")
            return None
        models = {m["endpoint_id"]: m for m in models_for(dict(body, run_id=run_id))}
        account = budget_balance(db, run)
        task = task_for(dict(body, run_id=run_id))
        candidate, decision, snapshot = _choose_endpoint(
            db, key, nodes, prepared, models, task, account, body.get("endpoint_revisions")
        )
        if candidate is None:
            reasons = sorted({reason for c in decision.get("candidates", []) for reason in c["reasons"]})
            reason = (
                "distinct_models_required"
                if group and decision.get("selected_endpoint")
                else ",".join(reasons) or decision["status"]
            )
            _unlease(store, db, att, "needs_input", reason, decision, snapshot)
            return None
        if account["available_micros"] < 0:
            _unlease(store, db, att, "needs_input", "budget_limit", decision, snapshot)
            return None
        endpoint = models[candidate["endpoint_id"]]
        active = db.execute(
            "SELECT count(*) FROM attempts WHERE endpoint_id=? AND state='dispatched'",
            (endpoint["endpoint_id"],),
        ).fetchone()[0]
        if active >= endpoint["max_concurrency"]:
            _unlease(store, db, att, "ready", "endpoint_concurrency", decision, snapshot)
            return None
        messages = prepared[key]
        prices = pricing_snapshot(endpoint)
        json_decision = body["mode"] == "tool" and "allowed_tools" in body
        context_ref = store.stage_blob(canonical({
            "schema": "clauseweft-request-context-v1",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "node_id": key,
            "generation": nodes[key]["generation"],
            "messages": messages,
            "definition": nodes[key]["definition"],
            "input_refs": refs[key],
            **({"preferred_output_format": "json"} if json_decision else {}),
            **({"conversation_source": body["conversation"]["source_id"]} if body.get("conversation") else {}),
            "endpoint_id": endpoint["endpoint_id"],
            "model_identity": endpoint["model_identity"],
            "endpoint_revision": endpoint["revision"],
            "pricing_snapshot": prices,
            "gateway_etag": endpoint["gateway_etag"],
            "data_scope": snapshot["task"]["data_scope"],
            "max_output_tokens": candidate["max_output_tokens"],
            "skills": nodes[key]["definition"].get("skill_snapshots", []),
        }))
        fingerprint = digest(
            {
                "task": body["task"],
                "definition": nodes[key]["definition"],
                "refs": refs[key],
                **({"conversation": body["conversation"]} if body.get("conversation") else {}),
                "base": body.get("base_sha"),
            }
        )
        if endpoint["endpoint_id"] != decision["selected_endpoint"]:
            # The preview ranks each branch independently. Dispatch also accounts
            # for earlier rate limits and the other members of a parallel group.
            decision["ranked_first_endpoint"] = decision["selected_endpoint"]
            decision["selected_endpoint"] = endpoint["endpoint_id"]
            decision["rank_metric"] = candidate["rank_metric"]
        now = time.time()
        db.execute(
            """UPDATE attempts SET state='dispatched',hold=?,accounting='held',endpoint_id=?,model_identity=?,
                      endpoint_revision=?,snapshot=?,decision=?,lease_until=?,context_ref=?,pricing_snapshot=? WHERE attempt_id=?""",
            (
                candidate["estimated_micros"],
                endpoint["endpoint_id"],
                endpoint["model_identity"],
                endpoint["revision"],
                canonical(snapshot),
                canonical(decision),
                now + LEASE_SECONDS,
                context_ref,
                canonical(prices),
                attempt_id,
            ),
        )
        db.execute(
            "UPDATE nodes SET state='dispatched',input_digest=? WHERE attempt_id=?",
            (fingerprint, attempt_id),
        )
        store.event(
            db,
            run_id,
            "dispatched",
            {
                "attempt_id": attempt_id,
                "node_id": key,
                "endpoint_id": endpoint["endpoint_id"],
                "hold_micros": candidate["estimated_micros"],
                "input_digest": fingerprint,
                "context_sha256": context_ref,
                "endpoint_revision": endpoint["revision"],
                "pricing_snapshot": prices,
                "input_refs": refs[key],
            },
        )
        return (dict(endpoint, json_decision=True) if json_decision else endpoint), messages, candidate["max_output_tokens"]
