"""Validate successful model outputs and publish any graph changes in the receipt transaction."""

import json

from clauseweft import project_tools, workflow
from clauseweft.storage import canonical

from .state import input_refs, insert_nodes, load_nodes, load_run


def publish_result(store, db, node, text, artifact):
    """Return the published artifact and a validation error, if any. The caller fences stale attempts."""
    key = node["node_id"]
    if key not in ("plan", "answer") and not node["definition"].get("tool_step"):
        return artifact, None
    run_id = node["run_id"]
    body = json.loads(load_run(db, run_id)["body"])
    if key == "plan" and body["mode"] == "complex":
        try:
            children = workflow.expand_complex(text, node["definition"])
        except (ValueError, RecursionError):
            return None, "invalid_complex_plan"
        insert_nodes(db, run_id, children)
        store.event(db, run_id, "complex_plan_expanded", {
            "generation": node["generation"], "attempt_id": node["attempt_id"],
            "nodes": [child["node_id"] for child in children],
        })
    if body["mode"] == "tool":
        if key == "answer" and project_tools.requests_another_tool(text):
            return None, "additional_tool_requested"
        if node["definition"].get("tool_step") and "allowed_tools" in body:
            try:
                return _publish_tool_decision(store, db, run_id, node, body, text, artifact), None
            except (ValueError, RecursionError) as exc:
                definition = dict(node["definition"], decision_feedback=str(exc)[:500])
                db.execute("UPDATE nodes SET definition=? WHERE run_id=? AND node_id=?",
                           (canonical(definition), run_id, key))
                return None, getattr(exc, "code", "invalid_agent_decision")
    return artifact, None


def _publish_tool_decision(store, db, run_id, current, body, text, raw_artifact):
    definition = current["definition"]
    receipts = [json.loads(store.read_blob(ref))
                for ref in input_refs(current, load_nodes(db, run_id)).values()]
    decision = project_tools.decision(body, text, receipts)
    if "answer" in decision:
        answer = store.stage_blob(decision["answer"])
        db.execute("UPDATE nodes SET definition=? WHERE run_id=? AND node_id=?",
                   (canonical(dict(definition, is_agent_answer=True, label="最终答复")), run_id, current["node_id"]))
        store.event(db, run_id, "agent_answer", {
            "node_id": current["node_id"], "artifact": answer, "response_sha256": raw_artifact,
        })
        return answer
    gate = workflow.tool_gate(definition["tool_step"], project_tools.TOOLS[decision["id"]][0])
    insert_nodes(db, run_id, [gate])
    store.event(db, run_id, "tool_proposed", {
        "node_id": gate["node_id"], "tool_id": decision["id"], "proposal_ref": raw_artifact,
    })
    return raw_artifact
