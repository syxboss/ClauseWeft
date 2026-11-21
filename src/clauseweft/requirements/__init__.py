"""Immutable requirement versions with atomic, review-backed baseline approval."""

import json

from clauseweft import reviews
from clauseweft.reviews._records import Conflict, digest, encode, init_schema as init_schema, many, now, one, string, strings


def _clauses(clauses):
    if not isinstance(clauses, list) or not clauses:
        raise ValueError("clauses 必须为非空条款列表")
    result = []
    for item in clauses:
        if not isinstance(item, dict):
            raise ValueError("条款必须为对象")
        acceptance = item.get("acceptance")
        if isinstance(acceptance, list):
            acceptance = strings(acceptance, "acceptance")
        else:
            acceptance = string(acceptance, "acceptance")
        result.append(dict(id=string(item.get("id"), "id"),
                           text=string(item.get("text"), "text"), acceptance=acceptance))
    strings([c["id"] for c in result], "clause_ids")
    return result


def current(conn, run_id):
    row = conn.execute(
        """SELECT s.body,h.spec=h.approved FROM mw_decision_head h JOIN mw_spec s
           ON s.run_id=h.run_id AND s.version=h.spec WHERE h.run_id=?""", (run_id,)).fetchone()
    if row:
        return dict(json.loads(row[0]), approved=bool(row[1]))
    return None


def list_specs(conn, run_id):
    items = many(conn, "SELECT body FROM mw_spec WHERE run_id=? ORDER BY rowid", (run_id,))
    for item in items:
        item["approval"] = one(conn, """SELECT body FROM mw_signed_decision
                                   WHERE run_id=? AND kind='baseline' AND target=?""",
                               (run_id, item["version"]))
        item["approved"] = item["approval"] is not None
    return items


def _changed(left, right):
    before = {c["id"]: c for c in left}
    after = {c["id"]: c for c in right}
    return sorted(k for k in before.keys() | after.keys() if before.get(k) != after.get(k))


def compare(conn, run_id, left, right):
    versions = [one(conn, "SELECT body FROM mw_spec WHERE run_id=? AND version=?", (run_id, v))
                for v in (left, right)]
    if any(v is None for v in versions):
        raise Conflict("需求版本不存在")
    a, b = [{c["id"]: c for c in v["clauses"]} for v in versions]
    return [dict(id=k, before=a.get(k), after=b.get(k))
            for k in _changed(versions[0]["clauses"], versions[1]["clauses"])]


def revise(conn, run_id, parent, clauses, reason, actor, required_reviewers=("product", "engineering")):
    """Call under BEGIN IMMEDIATE. Revision revokes the active baseline and delivery pointers."""
    current_spec = current(conn, run_id)
    if parent != (current_spec["version"] if current_spec else None):
        raise Conflict("父版本已过期")
    doc = dict(parent=parent, clauses=_clauses(clauses), reason=string(reason, "reason"),
               actor=string(actor, "actor"), required_reviewers=strings(required_reviewers, "required_reviewers"))
    version = digest("spec", [run_id, doc])
    result = dict(doc, version=version, changed_clauses=_changed(
        current_spec["clauses"] if current_spec else [], doc["clauses"]), created_at=now())
    conn.execute("INSERT INTO mw_spec VALUES (?,?,?)", (run_id, version, encode(result)))
    conn.execute("""INSERT INTO mw_decision_head(run_id,spec) VALUES (?,?)
                    ON CONFLICT(run_id) DO UPDATE SET spec=excluded.spec,approved=NULL,
                    candidate=NULL,delivery=NULL""", (run_id, version))
    target = dict(target=version, kind="spec", version=version, required_reviewers=doc["required_reviewers"])
    conn.execute("INSERT INTO mw_review_target VALUES (?,?,?)", (run_id, version, encode(target)))
    return dict(result, approved=False)


def freeze(conn, run_id, version, actor, reason):
    item = current(conn, run_id)
    if item is None or item["version"] != version:
        raise Conflict("需求审批目标已过期")
    if item["approved"]:
        raise Conflict("需求已有基线决议")
    reviews.require_ready(conn, run_id, version)
    decision = reviews._sign(conn, run_id, "baseline", version, actor, reason)
    conn.execute("UPDATE mw_decision_head SET approved=? WHERE run_id=?", (version, run_id))
    return dict(item, approved=True, approval=decision)
