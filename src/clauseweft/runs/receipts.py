"""Settle model attempts, publish eligible results and reconcile uncertain charges."""

import json
import time

from clauseweft.storage import canonical
from clauseweft.usage import normalized_usage, pricing_snapshot

from .leases import expire
from .results import publish_result
from .state import describe_run, load_nodes, load_run, refresh


# These gateway failures occur before the request reaches the provider.
NO_CHARGE = {
    "gateway_credentials_unavailable",
    "revision_mismatch",
    "concurrency_limit",
    "server_busy",
    "unauthorized",
    "forbidden",
    "invalid_request",
    "request_too_large",
    "model_not_found",
    "upstream_credentials_unavailable",
}
UPSTREAM_REJECTIONS = {"upstream_authentication_failed", "upstream_rate_limited"}
BAD_BINDING = {
    "revision_mismatch",
    "model_not_found",
    "upstream_authentication_failed",
    "unauthorized",
    "gateway_credentials_unavailable",
    "upstream_credentials_unavailable",
}
TRANSIENT = {"upstream_rate_limited", "concurrency_limit", "server_busy"}
INCOMPLETE_OUTPUT = {"output_length_limit", "output_content_filtered", "empty_model_output"}


def _actual_cost(error, artifact, usage, prices):
    # Unknown upstream work keeps its hold until an operator checks the charge.
    if artifact and usage["status"] == "reported":
        amount = (
            usage["prompt_tokens"] * prices["input_micros_per_million"]
            + usage["completion_tokens"] * prices["output_micros_per_million"]
            + 999_999
        ) // 1_000_000
        if amount <= 2**62:
            return amount
    return 0 if error in NO_CHARGE else None


def _duplicate_receipt(db, catalog, receipt, attempt_id, gateway_id):
    others = db.execute(
        "SELECT endpoint_id,endpoint_revision FROM attempts WHERE receipt=? AND attempt_id!=?",
        (receipt, attempt_id),
    )
    return any(catalog.get(*row)["gateway_id"] == gateway_id for row in others)


def finish_attempt(store, catalog, attempt_id, text=None, usage=None, receipt=None, finish_reason=None,
                   effective_max_tokens=None, *, error=None):
    artifact = store.stage_blob(text) if text is not None and not error else None
    response_ref = artifact
    if artifact:
        if finish_reason == "length":
            error = "output_length_limit"
        elif finish_reason == "content_filter":
            error = "output_content_filtered"
        elif not text.strip():
            error = "empty_model_output"
    with store.transaction() as db:
        att = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if not att:
            raise KeyError(attempt_id)
        if att["state"] not in {"dispatched", "uncertain"} or att["receipt"] is not None:
            store.event(db, att["run_id"], "receipt_ignored", {
                "attempt_id": attempt_id, "receipt": receipt,
                "response_sha256": response_ref, "reason": "already_finished_or_superseded",
            })
            return
        expire(store, db)
        run_id = att["run_id"]
        n = load_nodes(db, run_id)[att["node_id"]]
        model = catalog.get(att["endpoint_id"], att["endpoint_revision"])
        prices = json.loads(att["pricing_snapshot"]) if att["pricing_snapshot"] else pricing_snapshot(model)
        reported_usage = normalized_usage(usage)
        active = n["attempt_id"] == attempt_id and n["state"] == "dispatched"
        actual = _actual_cost(error, artifact, reported_usage, prices)
        if receipt and _duplicate_receipt(db, catalog, receipt, attempt_id, model["gateway_id"]):
            error, actual, artifact = "duplicate_gateway_receipt", None, None
            reported_usage = {"status": "unverified_duplicate_receipt", "reported": reported_usage}
        if error:
            state = "needs_input" if error in NO_CHARGE | UPSTREAM_REJECTIONS | INCOMPLETE_OUTPUT else "uncertain"
        else:
            state = "succeeded" if artifact else "uncertain"
        published_artifact = artifact
        if active and state == "succeeded":
            published_artifact, error = publish_result(store, db, n, text, artifact)
            if error:
                state = "needs_input"
        # Human reconciliation is authoritative if it arrived before this receipt.
        if att["accounting"] == "settled":
            actual = att["settled"]
        hold = att["hold"] if actual is None else 0
        db.execute(
            """UPDATE attempts SET state=?,receipt=?,error=?,finished=?,accounting=?,hold=?,settled=?,artifact=?,usage=?,completion_metadata=?
                      WHERE attempt_id=?""",
            (
                state,
                receipt,
                error,
                time.time(),
                "uncertain" if actual is None else "settled",
                hold,
                att["settled"] if actual is None else actual,
                artifact,
                canonical(reported_usage),
                canonical({"finish_reason": finish_reason, "max_output_tokens": effective_max_tokens})
                if finish_reason else None,
                attempt_id,
            ),
        )
        if error in BAD_BINDING:
            latest = catalog.get(att["endpoint_id"])
            if latest["revision"] == att["endpoint_revision"]:
                affected = [latest]
                if error in {"unauthorized", "gateway_credentials_unavailable"}:
                    credentials = catalog.gateways[model["gateway_id"]]
                    affected = [
                        ep
                        for ep in catalog.list()
                        if catalog.gateways.get(ep["gateway_id"]) == credentials
                    ]
                for endpoint in affected:
                    if endpoint["enabled"]:
                        catalog.save(dict(endpoint, enabled=False))
        if active:
            if error in TRANSIENT:
                failures = db.execute(
                    """SELECT count(*) FROM attempts WHERE run_id=? AND node_id=? AND generation=?
                                      AND endpoint_id IS NOT NULL""",
                    (run_id, n["node_id"], n["generation"]),
                ).fetchone()[0]
                task = json.loads(load_run(db, run_id)["body"])["task"]
                if failures < 3 and not (
                    task.get("pinned_endpoint") or n["definition"].get("pinned_endpoint")
                ):
                    state = "ready"
            db.execute(
                """UPDATE nodes SET state=?,artifact=?,reason=?,ready_at=? WHERE attempt_id=?""",
                (state, published_artifact if state == "succeeded" else None, error,
                 time.time() + 0.1 if state == "ready" else 0, attempt_id),
            )
        store.event(
            db,
            run_id,
            "attempt_finished",
            {
                "attempt_id": attempt_id,
                "receipt": receipt,
                "artifact": artifact,
                "response_sha256": response_ref,
                "finish_reason": finish_reason,
                "effective_max_output_tokens": effective_max_tokens,
                "published": active and state == "succeeded",
                "error": error,
                "settled_micros": actual,
                "hold_micros": hold,
                "usage": reported_usage,
                "pricing_snapshot": prices,
            },
        )
        if actual is not None:
            db.execute(
                """UPDATE nodes SET state='ready',reason=NULL WHERE run_id=? AND state='needs_input'
                          AND reason LIKE '%budget_limit%'""",
                (run_id,),
            )
        refresh(db, run_id)


def reconcile(store, run_id, attempt_id, actual_micros, actor, evidence):
    if (
        type(actual_micros) is not int
        or not 0 <= actual_micros <= 2**60
        or any(not isinstance(x, str) or not x.strip() for x in (actor, evidence))
    ):
        raise ValueError("对账需要有效金额、操作人和依据")
    with store.transaction() as db:
        load_run(db, run_id)
        att = db.execute(
            "SELECT * FROM attempts WHERE run_id=? AND attempt_id=?", (run_id, attempt_id)
        ).fetchone()
        if not att:
            raise KeyError(attempt_id)
        if att["accounting"] not in {"uncertain", "settled"}:
            raise RuntimeError("该尝试尚不能对账")
        db.execute(
            "UPDATE attempts SET accounting='settled',settled=?,hold=0 WHERE attempt_id=?",
            (actual_micros, attempt_id),
        )
        store.event(
            db,
            run_id,
            "reconciled",
            dict(
                attempt_id=attempt_id,
                actor=actor,
                evidence=evidence,
                previous_micros=att["settled"],
                actual_micros=actual_micros,
            ),
        )
        return describe_run(db, run_id)
