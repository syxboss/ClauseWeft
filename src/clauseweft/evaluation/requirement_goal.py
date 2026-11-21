"""Evaluate a requirement lookup/create goal against a run and its project's live records."""

import json

from clauseweft.project_tools.decisions import checked_text


def assess(run, expected, records, decisions, read_blob):
    if run["mode"] != "tool" or "allowed_tools" not in run:
        raise ValueError("需求查重验收仅适用于多步工具任务")
    scenario = expected.get("scenario")
    if scenario not in ("existing", "created", "rejected"):
        raise ValueError("scenario 须为 existing、created 或 rejected")
    keyword = checked_text(expected.get("query"), 160, "预期检索关键词")
    title = checked_text(expected.get("title"), 160, "预期需求标题")
    live_ids = [r["id"] for r in records if r["title"] == title]
    gates = sorted((n for n in run["nodes"] if n["kind"] == "gate" and n["state"] == "succeeded"),
                   key=lambda n: n["tool_step"])
    receipts = [json.loads(read_blob(gate["artifact"])) for gate in gates]
    executed = [data for kind, data in decisions if kind == "tool_executed"]
    rejected = [data for kind, data in decisions if kind == "tool_rejected"]
    approvals_match = len(executed) == len(receipts) and all(
        receipt.get("actor") and receipt.get("project_id") == run["project_id"] and
        all(receipt.get(k) == event.get(k) for k in ("tool_id", "node_id", "proposal_ref", "actor", "project_id"))
        for receipt, event in zip(receipts, executed)
    )
    search = next((r for r in receipts if r["tool_id"] == "search_requirements"
                   and r["arguments"]["query"].casefold() == keyword.casefold()), None)
    hits = search["result"]["items"] if search else []
    matching_hits = [r["id"] for r in hits if r["title"] == title]
    calls = [r["tool_id"] for r in receipts]
    answer_node = next((n for n in run["nodes"] if n.get("is_agent_answer") and n["state"] == "succeeded"), None)
    answer_ref = answer_node["artifact"] if answer_node else None
    answer = read_blob(answer_ref).decode() if answer_ref else ""
    checks = {"confirmed_receipts": approvals_match, "searched_for_query": search is not None}
    if scenario == "rejected":
        decision = rejected[0] if len(rejected) == 1 else {}
        proposal_ref = decision.get("proposal_ref")
        proposal = json.loads(read_blob(proposal_ref)) if proposal_ref else {}
        checks.update(
            search_was_empty=not hits,
            only_search_executed=calls == ["search_requirements"],
            rejected_target_creation=(decision.get("tool_id") == "create_requirement"
                and decision.get("project_id") == run["project_id"] and bool(decision.get("actor"))
                and proposal.get("arguments", {}).get("title") == title),
            cancelled=run["status"] == "cancelled", target_absent=not live_ids,
        )
    else:
        checks.update(completed=run["status"] == "completed", final_answer_available=bool(answer.strip()),
                      unique_live_requirement=len(live_ids) == 1,
                      answer_cites_requirement=len(live_ids) == 1 and live_ids[0] in answer)
        if scenario == "existing":
            checks.update(only_search_executed=calls == ["search_requirements"],
                          hit_matches_live_requirement=len(live_ids) == 1 and live_ids[0] in matching_hits)
        else:
            create = next((r for r in receipts if r["tool_id"] == "create_requirement"), {})
            created = create.get("result", {})
            checks.update(
                search_was_empty=not hits, search_then_create=calls == ["search_requirements", "create_requirement"],
                approved_target_created=(create.get("arguments", {}).get("title") == title and
                                         created.get("disposition") == "created" and created.get("title") == title),
                created_id_matches_project=len(live_ids) == 1 and created.get("id") == live_ids[0],
            )
    status = "pending"
    if run["status"] in ("completed", "cancelled"):
        status = "passed" if all(checks.values()) else "failed"
    return {
        "run_id": run["run_id"], "project_id": run["project_id"], "workspace_id": run["workspace_id"],
        "scenario": scenario, "query": keyword, "title": title, "status": status, "checks": checks,
        "evidence": {"receipts": [{"node_id": n["node_id"], "artifact": n["artifact"], "tool_id": r["tool_id"]}
                                  for n, r in zip(gates, receipts)],
                     "final_answer": answer_ref, "project_requirement_ids": live_ids,
                     "rejected_proposals": [r["proposal_ref"] for r in rejected]},
    }
