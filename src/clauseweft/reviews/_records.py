"""Shared records for a requirement and its version-bound reviews, in the caller's transaction."""

import hashlib
import json
from datetime import datetime, timezone


class Conflict(ValueError):
    """A well-formed decision conflicts with the current resource."""


def string(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} 必须为非空文本")
    return value


def strings(values, name, *, allow_empty=False):
    if not isinstance(values, (tuple, list)) or (not values and not allow_empty):
        raise ValueError(f"{name} 必须是列表")
    result = [string(value, name) for value in values]
    if len(set(result)) != len(result):
        raise ValueError(f"{name} 中存在重复值")
    return result


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(kind, value):
    return hashlib.sha256(encode([1, kind, value]).encode()).hexdigest()


def now():
    return datetime.now(timezone.utc).isoformat()


def one(conn, query, params):
    row = conn.execute(query, params).fetchone()
    return json.loads(row[0]) if row else None


def many(conn, query, params):
    return [json.loads(row[0]) for row in conn.execute(query, params)]


def init_schema(conn):
    # Execute individually: executescript implicitly commits an existing transaction.
    statements = (
        """CREATE TABLE IF NOT EXISTS mw_spec (
               run_id TEXT NOT NULL, version TEXT NOT NULL, body TEXT NOT NULL,
               PRIMARY KEY(run_id, version))""",
        """CREATE TABLE IF NOT EXISTS mw_decision_head (
               run_id TEXT PRIMARY KEY, spec TEXT NOT NULL, approved TEXT,
               candidate TEXT, delivery TEXT,
               FOREIGN KEY(run_id,spec) REFERENCES mw_spec(run_id,version))""",
        """CREATE TABLE IF NOT EXISTS mw_review_target (
               run_id TEXT NOT NULL, target TEXT NOT NULL, body TEXT NOT NULL,
               PRIMARY KEY(run_id,target))""",
        """CREATE TABLE IF NOT EXISTS mw_review (
               run_id TEXT NOT NULL, target TEXT NOT NULL, branch TEXT NOT NULL, body TEXT NOT NULL,
               PRIMARY KEY(run_id,target,branch),
               FOREIGN KEY(run_id,target) REFERENCES mw_review_target(run_id,target))""",
        """CREATE TABLE IF NOT EXISTS mw_finding (
               run_id TEXT NOT NULL, id TEXT NOT NULL, target TEXT NOT NULL, branch TEXT NOT NULL,
               severity TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id,id),
               FOREIGN KEY(run_id,target,branch) REFERENCES mw_review(run_id,target,branch))""",
        """CREATE TABLE IF NOT EXISTS mw_resolution (
               run_id TEXT NOT NULL, id TEXT NOT NULL, body TEXT NOT NULL, PRIMARY KEY(run_id,id),
               FOREIGN KEY(run_id,id) REFERENCES mw_finding(run_id,id))""",
        """CREATE TABLE IF NOT EXISTS mw_signed_decision (
               run_id TEXT NOT NULL, id TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
               body TEXT NOT NULL, PRIMARY KEY(run_id,id),
               FOREIGN KEY(run_id,target) REFERENCES mw_review_target(run_id,target))""",
    )
    for statement in statements:
        conn.execute(statement)
