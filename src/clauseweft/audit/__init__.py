"""Transactional, redacted audit records with a verifiable hash chain.

The chain detects changes against its recorded head or an independently retained
export anchor. A database administrator can rewrite both a database and its head.
"""

import hashlib
import json
import re
import time
from uuid import uuid4

from clauseweft.workspace.credentials import redact

FORMAT = "clauseweft-audit-v1"
ZERO_HASH = "0" * 64
SYSTEM = {"id": "system-worker", "display_name": "系统执行器"}
LEGACY = {"id": "legacy-unknown", "display_name": "历史记录，操作者未知"}
_SECRET_FIELDS = re.compile(
    r"^(?:password|password_hash|passphrase|salt|secret|client_secret|secret_key|"
    r"api_key|authorization|access_token|refresh_token|auth_token|session_token|"
    r"token|credential|credentials)$",
    re.I,
)


def _canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha(value):
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def sanitize(value):
    """Return JSON-safe evidence while withholding credential material."""
    if isinstance(value, dict):
        return {
            str(key): "[redacted]"
            if _SECRET_FIELDS.fullmatch(str(key))
            else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [sanitize(item) for item in value]
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, bytes):
        return {"sha256": hashlib.sha256(value).hexdigest(), "size": len(value)}
    if value is None or type(value) in (int, float, bool):
        return value
    raise ValueError("审计证据须为 JSON 可序列化数据")


def _exists(db, table):
    return (
        db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        is not None
    )


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS audit_entries (
        seq INTEGER PRIMARY KEY, at REAL NOT NULL, actor_id TEXT NOT NULL,
        actor TEXT NOT NULL, action TEXT NOT NULL, resource TEXT NOT NULL,
        outcome TEXT NOT NULL, project_id TEXT, run_id TEXT, correlation_id TEXT,
        payload TEXT NOT NULL, prev_hash TEXT NOT NULL, hash TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS audit_head (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), ledger_id TEXT NOT NULL,
        seq INTEGER NOT NULL, hash TEXT NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS audit_migrations (
        source TEXT PRIMARY KEY, migrated_at REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS audit_resource_revisions (
        kind TEXT, resource_id TEXT, revision INTEGER, at REAL NOT NULL,
        payload TEXT NOT NULL, sha256 TEXT NOT NULL, source TEXT NOT NULL,
        audit_seq INTEGER NOT NULL REFERENCES audit_entries(seq),
        PRIMARY KEY(kind,resource_id,revision))""")
    for table in ("audit_entries", "audit_resource_revisions"):
        for operation in ("UPDATE", "DELETE"):
            db.execute(f"""CREATE TRIGGER IF NOT EXISTS {table}_{operation.lower()}_blocked
                BEFORE {operation} ON {table} BEGIN
                SELECT RAISE(ABORT,'audit history is append-only'); END""")
    if not db.execute("SELECT 1 FROM audit_head WHERE singleton=1").fetchone():
        if db.execute("SELECT 1 FROM audit_entries LIMIT 1").fetchone():
            raise RuntimeError("审计账本缺少链头，须从可信备份核验")
        db.execute("INSERT INTO audit_head VALUES(1,?,0,?)", (uuid4().hex, ZERO_HASH))
    migrate_legacy(db)


def _head(db):
    row = db.execute(
        "SELECT ledger_id,seq,hash FROM audit_head WHERE singleton=1"
    ).fetchone()
    if row is None:
        raise RuntimeError("审计账本尚未初始化")
    return dict(row)


def record(
    db,
    actor,
    action,
    resource,
    outcome="succeeded",
    details=None,
    before=None,
    after=None,
    project_id=None,
    run_id=None,
    correlation_id=None,
):
    """Append to the caller's transaction. It must commit with the business write."""
    head = _head(db)
    last = db.execute(
        "SELECT seq,hash FROM audit_entries ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    if (last is None and head["seq"] != 0) or (
        last and (last[0], last[1]) != (head["seq"], head["hash"])
    ):
        raise RuntimeError("审计链头与记录不一致，已停止追加")
    if isinstance(actor, str):
        actor = {"id": actor, "display_name": actor}
    actor = actor or LEGACY
    raw = {"details": details or {}, "before": before, "after": after}
    evidence = sanitize(raw)
    evidence.update(
        before_sha256=None if before is None else _sha(evidence["before"]),
        after_sha256=None if after is None else _sha(evidence["after"]),
        redacted=evidence != raw,
    )
    at = time.time()
    seq = head["seq"] + 1
    entry = dict(
        ledger_id=head["ledger_id"],
        seq=seq,
        at=at,
        actor_id=redact(str(actor.get("id", LEGACY["id"]))),
        actor=redact(
            str(
                actor.get(
                    "display_name",
                    actor.get("username", actor.get("id", LEGACY["display_name"])),
                )
            )
        ),
        action=redact(str(action)),
        resource=redact(str(resource)),
        outcome=str(outcome),
        project_id=project_id,
        run_id=run_id,
        correlation_id=correlation_id,
        **evidence,
        prev_hash=head["hash"],
    )
    entry["hash"] = _sha(entry)
    db.execute(
        "INSERT INTO audit_entries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            seq,
            at,
            entry["actor_id"],
            entry["actor"],
            entry["action"],
            entry["resource"],
            entry["outcome"],
            project_id,
            run_id,
            correlation_id,
            _canonical(dict(evidence, ledger_id=head["ledger_id"])),
            head["hash"],
            entry["hash"],
        ),
    )
    db.execute(
        "UPDATE audit_head SET seq=?,hash=? WHERE singleton=1", (seq, entry["hash"])
    )
    return dict(entry, id=seq)


def _entry(row):
    value = dict(row)
    value.update(json.loads(value.pop("payload")))
    value["id"] = value["seq"]
    return value


def list_entries(db, filters=None):
    filters = filters or {}
    allowed = (
        "project_id",
        "run_id",
        "resource",
        "outcome",
        "action",
        "actor_id",
        "correlation_id",
    )
    clauses, values = [], []
    for key in allowed:
        if filters.get(key):
            clauses.append(key + "=?")
            values.append(filters[key])
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    return [
        _entry(row)
        for row in db.execute(
            "SELECT * FROM audit_entries" + where + " ORDER BY seq DESC", values
        )
    ]


def verify_entries(entries, anchor):
    """Verify an exported full chain against a separately retained final anchor."""
    errors = []
    previous = ZERO_HASH
    for expected, raw in enumerate(entries, 1):
        entry = dict(raw)
        entry.pop("id", None)
        claimed = entry.pop("hash", None)
        if entry.get("seq") != expected:
            errors.append({"seq": expected, "code": "sequence_gap"})
        if entry.get("ledger_id") != anchor["ledger_id"]:
            errors.append({"seq": expected, "code": "ledger_mismatch"})
        if entry.get("prev_hash") != previous:
            errors.append({"seq": expected, "code": "previous_hash_mismatch"})
        if _sha(entry) != claimed:
            errors.append({"seq": expected, "code": "entry_hash_mismatch"})
        for key in ("before", "after"):
            expected_hash = None if entry.get(key) is None else _sha(entry[key])
            if entry.get(key + "_sha256") != expected_hash:
                errors.append({"seq": expected, "code": key + "_hash_mismatch"})
        previous = claimed
    if anchor["seq"] != len(entries) or anchor["hash"] != previous:
        errors.append({"seq": anchor["seq"], "code": "anchor_mismatch"})
    return dict(
        valid=not errors,
        status="verified" if not errors else "failed",
        checked_entries=len(entries),
        anchor=anchor,
        errors=errors,
    )


def verify(db):
    try:
        entries = [
            _entry(row)
            for row in db.execute("SELECT * FROM audit_entries ORDER BY seq")
        ]
        result = verify_entries(entries, _head(db))
        by_id = {entry["seq"]: entry for entry in entries}
        for row in db.execute("SELECT * FROM audit_resource_revisions"):
            snapshot = json.loads(row["payload"])
            entry = by_id.get(row["audit_seq"])
            if (
                _sha(snapshot) != row["sha256"]
                or not entry
                or entry.get("after_sha256") != row["sha256"]
            ):
                result["errors"].append(
                    {"seq": row["audit_seq"], "code": "resource_history_mismatch"}
                )
        result["errors"].extend(_verify_live_resources(db))
        result["errors"].extend(_verify_release_snapshots(db, entries))
        result["errors"].extend(_verify_job_results(db, entries))
        result["errors"].extend(_verify_attempt_accounting(db, entries))
        result["valid"] = not result["errors"]
        result["status"] = "verified" if result["valid"] else "failed"
        return result
    except (ValueError, TypeError, KeyError) as error:
        return dict(
            valid=False,
            status="failed",
            checked_entries=0,
            anchor=_head(db),
            errors=[
                {"seq": None, "code": "invalid_entry", "detail": type(error).__name__}
            ],
        )


def _verify_attempt_accounting(db, entries):
    if not _exists(db, "attempts"):
        return []
    columns = {row["name"] for row in db.execute("PRAGMA table_info(attempts)")}
    if not {"pricing_snapshot", "usage"} <= columns:
        return []
    errors, checked = [], set()
    for entry in reversed(entries):
        if entry["action"] not in {"run.dispatched", "run.attempt_finished"}:
            continue
        evidence = entry.get("details", {}).get("evidence", {})
        attempt_id = evidence.get("attempt_id")
        for field in ("pricing_snapshot", "usage"):
            # Old attempts may only gain a usage record on completion. Their
            # tariff remains explicitly recoverable from the frozen catalog.
            if field not in evidence or field == "pricing_snapshot" and entry["action"] != "run.dispatched":
                continue
            key = attempt_id, field
            if not attempt_id or key in checked:
                continue
            checked.add(key)
            row = db.execute("SELECT pricing_snapshot,usage FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            current = json.loads(row[field]) if row and row[field] is not None else None
            if row is None or sanitize(current) != evidence[field]:
                errors.append({"seq": entry["seq"], "code": "attempt_accounting_mismatch", "attempt_id": attempt_id, "field": field})
    for row in db.execute("SELECT attempt_id,pricing_snapshot,usage FROM attempts"):
        for field in ("pricing_snapshot", "usage"):
            if row[field] is not None and (row["attempt_id"], field) not in checked:
                errors.append({"seq": None, "code": "attempt_accounting_unproven", "attempt_id": row["attempt_id"], "field": field})
    return errors


def _verify_live_resources(db):
    errors = []
    checked = set()
    for row in db.execute(
        "SELECT * FROM audit_resource_revisions ORDER BY revision DESC"
    ):
        key = row["kind"], row["resource_id"]
        if key in checked:
            continue
        checked.add(key)
        saved = json.loads(row["payload"])
        kind, identifier = key
        if kind == "endpoints" and _exists(db, "endpoint_revisions"):
            live = db.execute(
                "SELECT revision,data FROM endpoint_revisions WHERE endpoint_id=? ORDER BY revision DESC LIMIT 1",
                (identifier,),
            ).fetchone()
            current = sanitize(json.loads(live["data"])) if live else None
            saved = {
                key: value for key, value in saved.items() if key != "_gateway_binding"
            }
            if current is not None:
                current.pop("_gateway_binding", None)
        elif kind == "members" and _exists(db, "platform_members"):
            live = db.execute(
                "SELECT *,created AS created_at FROM platform_members WHERE id=?",
                (identifier,),
            ).fetchone()
            current = (
                {key: live[key] for key in saved if key in live.keys()}
                if live
                else None
            )
            if current is not None:
                for field in ("workspace_ids", "project_ids"):
                    if field in current and isinstance(current[field], str):
                        current[field] = json.loads(current[field])
        elif kind in (
            "projects",
            "roles",
            "templates",
            "requirements",
            "skills",
            "workspaces",
        ) and _exists(db, "platform_resources"):
            live = db.execute(
                "SELECT revision,payload FROM platform_resources WHERE kind=? AND id=?",
                key,
            ).fetchone()
            current = sanitize(json.loads(live["payload"])) if live else None
        else:
            continue
        if live is None or live["revision"] != row["revision"] or current != saved:
            errors.append(
                {
                    "seq": row["audit_seq"],
                    "code": "live_resource_mismatch",
                    "kind": kind,
                    "resource_id": identifier,
                }
            )
    return errors


def _verify_job_results(db, entries):
    if not _exists(db, "api_jobs"):
        return []
    errors, checked = [], set()
    actions = {
        "delivery_job_finished",
        "delivery_job_failed",
        "delivery_job_stale",
        "delivery_job_interrupted",
    }
    for entry in reversed(entries):
        proof = entry.get("after")
        legacy = (
            entry["action"] == "legacy.snapshot"
            and entry.get("details", {}).get("source") == "api_jobs"
        )
        if (entry["action"] not in actions and not legacy) or not isinstance(
            proof, dict
        ):
            continue
        job_id = proof.get("id")
        if not job_id or job_id in checked:
            continue
        checked.add(job_id)
        live = db.execute(
            "SELECT result FROM api_jobs WHERE id=?", (job_id,)
        ).fetchone()
        expected = (
            _sha(sanitize(proof.get("result")))
            if legacy
            else proof.get("result_sha256")
        )
        current = (
            _sha(sanitize(json.loads(live[0])))
            if live and live[0] is not None
            else _sha(None)
            if legacy
            else None
        )
        if live is None or current != expected:
            errors.append(
                {"seq": entry["seq"], "code": "job_result_mismatch", "job_id": job_id}
            )
    return errors


def _verify_release_snapshots(db, entries):
    if not _exists(db, "platform_releases"):
        return []
    errors, checked = [], set()
    for entry in reversed(entries):
        proof = entry.get("after")
        if not isinstance(proof, dict):
            continue
        if entry["action"] == "template.published":
            key, expected = (proof.get("id"), proof.get("revision")), proof
        elif (
            entry["action"] == "legacy.snapshot"
            and entry.get("details", {}).get("source") == "platform_releases"
        ):
            key, expected = (
                (proof.get("template_id"), proof.get("revision")),
                proof.get("snapshot"),
            )
        else:
            continue
        if key in checked:
            continue
        checked.add(key)
        row = db.execute(
            "SELECT snapshot FROM platform_releases WHERE template_id=? AND revision=?",
            key,
        ).fetchone()
        if row is None or sanitize(json.loads(row[0])) != expected:
            errors.append(
                {
                    "seq": entry["seq"],
                    "code": "published_snapshot_mismatch",
                    "template_id": key[0],
                    "revision": key[1],
                }
            )
    return errors


def export_bundle(db, filters=None):
    """Include the complete proof chain, plus the sequence IDs selected by filters."""
    entries = list(reversed(list_entries(db)))
    anchor = _head(db)
    return dict(
        format=FORMAT,
        exported_at=time.time(),
        anchor=anchor,
        entries=entries,
        selected_entry_ids=[row["seq"] for row in list_entries(db, filters)],
        scope=sanitize(filters or {}),
        verification=verify(db),
    )


def save_revision(
    db,
    kind,
    resource_id,
    revision,
    payload,
    actor=None,
    before=None,
    source="live",
    project_id=None,
    correlation_id=None,
):
    """Preserve each resource version and link it to the committed audit event."""
    current = db.execute(
        "SELECT sha256 FROM audit_resource_revisions WHERE kind=? AND resource_id=? AND revision=?",
        (kind, resource_id, revision),
    ).fetchone()
    safe = sanitize(payload)
    if current:
        if current[0] != _sha(safe):
            raise RuntimeError("已有资源版本的审计快照不一致")
        return
    entry = record(
        db,
        actor,
        "resource.snapshot" if source == "legacy" else "resource.saved",
        kind + "/" + resource_id,
        outcome="imported" if source == "legacy" else "succeeded",
        details={
            "kind": kind,
            "revision": revision,
            "source": source,
            "history_complete": source != "legacy",
        },
        before=before,
        after=payload,
        project_id=project_id,
        correlation_id=correlation_id,
    )
    db.execute(
        "INSERT INTO audit_resource_revisions VALUES(?,?,?,?,?,?,?,?)",
        (
            kind,
            resource_id,
            revision,
            entry["at"],
            _canonical(safe),
            _sha(safe),
            source,
            entry["seq"],
        ),
    )


def history(db, kind, resource_id):
    return [
        dict(
            kind=row["kind"],
            resource_id=row["resource_id"],
            revision=row["revision"],
            at=row["at"],
            payload=json.loads(row["payload"]),
            sha256=row["sha256"],
            source=row["source"],
            audit_seq=row["audit_seq"],
        )
        for row in db.execute(
            "SELECT * FROM audit_resource_revisions WHERE kind=? AND resource_id=? ORDER BY revision DESC",
            (kind, resource_id),
        )
    ]


def migrate_legacy(db):
    """Import available historical evidence once without inventing missing history."""
    sources = (
        "platform_audit",
        "events",
        "endpoint_revisions",
        "platform_resources",
        "platform_members",
        "platform_releases",
        "runs",
        "attempts",
        "api_context",
        "api_jobs",
        "mw_spec",
        "mw_review",
        "mw_finding",
        "mw_resolution",
        "mw_signed_decision",
    )
    for source in sources:
        if (
            not _exists(db, source)
            or db.execute(
                "SELECT 1 FROM audit_migrations WHERE source=?", (source,)
            ).fetchone()
        ):
            continue
        if source == "platform_audit":
            for row in db.execute("SELECT * FROM platform_audit ORDER BY id"):
                record(
                    db,
                    {"id": row["actor_id"], "display_name": row["actor"]},
                    row["action"],
                    row["resource"],
                    outcome="imported",
                    details={
                        "source": source,
                        "source_id": row["id"],
                        "original_at": row["at"],
                        "evidence": json.loads(row["details"]),
                    },
                )
        elif source == "events":
            for row in db.execute("SELECT * FROM events ORDER BY at,run_id,seq"):
                record(
                    db,
                    LEGACY,
                    "run." + row["kind"],
                    "runs/" + row["run_id"],
                    outcome="imported",
                    run_id=row["run_id"],
                    details={
                        "source": source,
                        "original_at": row["at"],
                        "event_seq": row["seq"],
                        "evidence": json.loads(row["data"]),
                    },
                )
        elif source == "endpoint_revisions":
            for row in db.execute(
                "SELECT * FROM endpoint_revisions ORDER BY endpoint_id,revision"
            ):
                save_revision(
                    db,
                    "endpoints",
                    row["endpoint_id"],
                    row["revision"],
                    json.loads(row["data"]),
                    LEGACY,
                    source="legacy",
                )
        elif source == "platform_resources":
            for row in db.execute("SELECT * FROM platform_resources ORDER BY kind,id"):
                value = json.loads(row["payload"])
                save_revision(
                    db,
                    row["kind"],
                    row["id"],
                    row["revision"],
                    value,
                    LEGACY,
                    source="legacy",
                    project_id=value.get("project_id"),
                )
        elif source == "platform_members":
            for row in db.execute("SELECT * FROM platform_members ORDER BY id"):
                value = {key: row[key] for key in ("id", "username", "display_name", "role", "active", "revision", "workspace_ids", "project_ids") if key in row.keys()}
                for field in ("workspace_ids", "project_ids"):
                    if field in value and isinstance(value[field], str):
                        value[field] = json.loads(value[field])
                save_revision(
                    db,
                    "members",
                    row["id"],
                    row["revision"],
                    value,
                    LEGACY,
                    source="legacy",
                )
        else:
            for row in db.execute(
                "SELECT rowid AS legacy_rowid,* FROM " + source + " ORDER BY rowid"
            ):
                value = dict(row)
                for field in (
                    "body",
                    "snapshot",
                    "decision",
                    "config",
                    "input",
                    "result",
                ):
                    if isinstance(value.get(field), str):
                        try:
                            value[field] = json.loads(value[field])
                        except ValueError:
                            pass
                run_id = value.get("run_id")
                resource = (
                    "runs/" + run_id
                    if run_id
                    else source + "/" + str(value["legacy_rowid"])
                )
                record(
                    db,
                    LEGACY,
                    "legacy.snapshot",
                    resource,
                    outcome="imported",
                    after=value,
                    run_id=run_id,
                    details={
                        "source": source,
                        "source_rowid": value["legacy_rowid"],
                        "history_complete": False,
                    },
                )
        db.execute("INSERT INTO audit_migrations VALUES(?,?)", (source, time.time()))
