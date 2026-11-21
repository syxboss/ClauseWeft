"""Independent, version-scoped review submissions, resolutions and signed deliveries."""

import json
import re

from ._records import Conflict, digest, encode, many, now, one, string, strings


def _head(conn, run_id):
    return conn.execute("SELECT spec,approved,candidate,delivery FROM mw_decision_head WHERE run_id=?",
                        (run_id,)).fetchone()


def _target_status(head, target):
    if not head or target not in (head[0], head[2]) or not target:
        return "historical"
    if target == head[1] or (target == head[2] and head[3] is not None):
        return "signed"
    return "open"


def _active(conn, run_id, target):
    status = _target_status(_head(conn, run_id), target)
    if status == "historical":
        raise Conflict("评审目标已过期或不存在")
    if status == "signed":
        raise Conflict("已签核目标不可变更")
    return _target(conn, run_id, target)


def _target(conn, run_id, target):
    result = one(conn, "SELECT body FROM mw_review_target WHERE run_id=? AND target=?", (run_id, target))
    if result is None:
        raise Conflict("评审目标不存在")
    return result


def set_candidate(conn, run_id, version, candidate_sha, required_reviewers=("code-review",), *, generation=None):
    reviewers = strings(required_reviewers, "required_reviewers")
    if not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", string(candidate_sha, "candidate_sha")):
        raise ValueError("candidate_sha 必须为完整 Git 提交散列")
    head = conn.execute("SELECT approved,candidate FROM mw_decision_head WHERE run_id=?", (run_id,)).fetchone()
    if not head or not version or version != head[0]:
        raise Conflict("必须使用当前获批需求生成候选")
    fields = [run_id, version, candidate_sha, reviewers]
    if generation is not None:
        if type(generation) is not int or generation < 1:
            raise ValueError("generation 必须是正整数")
        fields.append(generation)
    target = digest("code-review", fields)
    result = dict(kind="code", target=target, candidate_sha=candidate_sha, version=version,
                  required_reviewers=reviewers)
    if generation is not None:
        result["generation"] = generation
    conn.execute("INSERT OR IGNORE INTO mw_review_target VALUES (?,?,?)", (run_id, target, encode(result)))
    if head[1] != target:
        conn.execute("UPDATE mw_decision_head SET candidate=?,delivery=NULL WHERE run_id=?", (target, run_id))
    return result


def candidate(conn, run_id):
    return one(conn, """SELECT t.body FROM mw_decision_head h JOIN mw_review_target t ON
                        h.run_id=t.run_id AND h.candidate=t.target WHERE h.run_id=?""", (run_id,))


def _validate_findings(findings):
    if not isinstance(findings, list):
        raise ValueError("findings 必须为列表")
    entries = []
    for item in findings:
        if not isinstance(item, dict):
            raise ValueError("意见必须为对象")
        severity = item.get("severity")
        if severity not in ("blocker", "major", "minor", "info"):
            raise ValueError("severity 必须为 blocker/major/minor/info")
        entries.append(dict(severity=severity, **{k: string(item.get(k), k)
                            for k in ("location", "example", "suggestion", "evidence")}))
    return entries


def submit(conn, run_id, target, branch, author, model_identity, findings):
    """One immutable submission per branch. Identical retries return the original record."""
    doc = dict(target=string(target, "target"), branch=string(branch, "branch"),
               author=string(author, "author"), model_identity=string(model_identity, "model_identity"),
               findings=_validate_findings(findings))
    submission = digest("review", [run_id, doc])
    old = one(conn, "SELECT body FROM mw_review WHERE run_id=? AND target=? AND branch=?",
              (run_id, target, branch))
    if old:
        if old["submission_id"] == submission:
            return old
        raise Conflict("该分支已提交不同内容")
    scope = _active(conn, run_id, target)
    if branch not in scope["required_reviewers"]:
        raise Conflict("未登记的评审分支")
    existing = many(conn, "SELECT body FROM mw_review WHERE run_id=? AND target=?", (run_id, target))
    if any(review["model_identity"] == model_identity for review in existing):
        raise Conflict("独立评审分支不能复用同一模型身份")
    findings = [dict(item, id=digest("finding", [submission, index]), target=target, branch=branch,
                     author=author, model_identity=model_identity) for index, item in enumerate(doc["findings"])]
    result = dict(doc, submission_id=submission, findings=findings, created_at=now())
    conn.execute("INSERT INTO mw_review VALUES (?,?,?,?)", (run_id, target, branch, encode(result)))
    conn.executemany("INSERT INTO mw_finding VALUES (?,?,?,?,?,?)",
                     [(run_id, f["id"], target, branch, f["severity"], encode(f)) for f in findings])
    return result


def list_findings(conn, run_id, target=None):
    sql = """SELECT f.body,r.body FROM mw_finding f LEFT JOIN mw_resolution r
             ON f.id=r.id AND f.run_id=r.run_id WHERE f.run_id=?"""
    params = [run_id]
    if target is not None:
        sql += " AND f.target=?"
        params.append(target)
    return [dict(json.loads(body), resolution=json.loads(resolution) if resolution else None)
            for body, resolution in conn.execute(sql + " ORDER BY f.rowid", params)]


def finding_records(conn, run_id):
    """Read live target status alongside the original findings, outside frozen evidence."""
    head = _head(conn, run_id)
    return [dict(item, target_status=_target_status(head, item["target"]))
            for item in list_findings(conn, run_id)]


def resolve(conn, run_id, finding_id, actor, action, reason, evidence=None, related_ids=()):
    item = one(conn, "SELECT body FROM mw_finding WHERE run_id=? AND id=?", (run_id, finding_id))
    if not item:
        raise Conflict("该运行中无此意见")
    _active(conn, run_id, item["target"])
    if action not in ("accept", "fix", "reject", "duplicate"):
        raise ValueError("action 必须为 accept/fix/reject/duplicate")
    if evidence is not None or item["severity"] == "blocker":
        evidence = string(evidence, "复核证据")
    related = strings(related_ids, "related_ids", allow_empty=action != "duplicate")
    if related:
        available = {f["id"]: f for f in list_findings(conn, run_id, item["target"]) if f["id"] != finding_id}
        if not set(related) <= available.keys():
            raise Conflict("关联意见必须来自同一目标且不能指向自身")
        if action == "duplicate":
            pending = related.copy()
            visited = set()
            while pending:
                linked = pending.pop()
                if linked == finding_id:
                    raise Conflict("重复意见之间不可循环引用")
                if linked in visited:
                    continue
                visited.add(linked)
                previous = available[linked]["resolution"]
                if previous and previous["action"] == "duplicate":
                    pending.extend(previous["related_ids"])
    decision = dict(finding_id=finding_id, actor=string(actor, "actor"), action=action,
                    reason=string(reason, "reason"), evidence=evidence, related_ids=related, created_at=now())
    if one(conn, "SELECT body FROM mw_resolution WHERE run_id=? AND id=?", (run_id, finding_id)):
        raise Conflict("意见已有终结决议")
    conn.execute("INSERT INTO mw_resolution VALUES (?,?,?)", (run_id, finding_id, encode(decision)))
    return decision


def status(conn, run_id, target):
    scope = _target(conn, run_id, target)
    submissions = many(conn, "SELECT body FROM mw_review WHERE run_id=? AND target=? ORDER BY branch",
                       (run_id, target))
    missing = sorted(set(scope["required_reviewers"]) - {r["branch"] for r in submissions})
    findings = list_findings(conn, run_id, target)
    closed = {f["id"] for f in findings if f["resolution"] and f["resolution"]["action"] != "duplicate"}
    # A merged blocker inherits the outcome of its originals, including pending dispositions.
    duplicates = {f["id"]: set(f["resolution"]["related_ids"]) for f in findings
                  if f["resolution"] and f["resolution"]["action"] == "duplicate"}
    while True:
        resolved = {fid for fid, links in duplicates.items() if links <= closed} - closed
        if not resolved:
            break
        closed.update(resolved)
    blockers = [f["id"] for f in findings if f["severity"] == "blocker" and f["id"] not in closed]
    return dict(target=target, missing_reviewers=missing, open_blockers=blockers,
                submissions=submissions, ready=not (missing or blockers))


def require_ready(conn, run_id, target):
    review = status(conn, run_id, target)
    if not review["ready"]:
        raise Conflict(f"缺少评审 {review['missing_reviewers']}，未关闭阻断意见 {review['open_blockers']}")


def _sign(conn, run_id, kind, target, actor, reason, **fields):
    result = dict(kind=kind, target=target, actor=string(actor, "actor"), reason=string(reason, "reason"),
                  created_at=now(), **fields)
    result["decision_id"] = digest("decision", [run_id, result])
    conn.execute("INSERT INTO mw_signed_decision VALUES (?,?,?,?,?)",
                 (run_id, result["decision_id"], kind, target, encode(result)))
    return result


def invalidate_candidate(conn, run_id):
    conn.execute("UPDATE mw_decision_head SET candidate=NULL,delivery=NULL WHERE run_id=?", (run_id,))


def invalidate_delivery(conn, run_id):
    conn.execute("UPDATE mw_decision_head SET delivery=NULL WHERE run_id=?", (run_id,))


def delivery(conn, run_id):
    return one(conn, """SELECT d.body FROM mw_decision_head h JOIN mw_signed_decision d ON
                        h.run_id=d.run_id AND h.delivery=d.id WHERE h.run_id=?""", (run_id,))


def accept(conn, run_id, version, candidate_sha, actor, reason, *, report, manifest):
    """Sign within the transaction that also checks cancellation and the trusted current suite."""
    scope = candidate(conn, run_id)
    if not scope or scope["version"] != version or scope["candidate_sha"] != candidate_sha:
        raise Conflict("需要当前已批准基线及当前候选")
    if delivery(conn, run_id):
        raise Conflict("当前交付已有接纳决定")
    require_ready(conn, run_id, scope["target"])
    if (not isinstance(report, dict) or report.get("status") != "passed"
            or report.get("spec_version_id") != version or report.get("candidate_sha") != candidate_sha
            or any(not report.get(k) for k in ("execution_id", "suite_digest", "env_digest", "cases"))):
        raise Conflict("缺少绑定当前候选与需求的有效通过报告")
    if not isinstance(manifest, dict):
        raise ValueError("manifest 必须为对象")
    result = _sign(conn, run_id, "delivery", scope["target"], actor, reason, version=version,
                   candidate_sha=candidate_sha, report=report, manifest=manifest)
    conn.execute("UPDATE mw_decision_head SET delivery=? WHERE run_id=?", (result["decision_id"], run_id))
    return result
