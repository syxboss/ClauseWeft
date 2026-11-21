"""Single-workspace SQLite transactions and immutable content-addressed objects."""

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import threading
import time


def canonical(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def digest(value):
    return hashlib.sha256(
        canonical({"version": 1, "input": value}).encode()
    ).hexdigest()


SCHEMA = (
    """CREATE TABLE endpoint_revisions (
        endpoint_id TEXT, revision INTEGER, data TEXT NOT NULL,
        PRIMARY KEY(endpoint_id, revision))""",
    """CREATE TABLE runs (
        run_id TEXT PRIMARY KEY, revision INTEGER NOT NULL DEFAULT 1, body TEXT NOT NULL,
        status TEXT NOT NULL, created REAL NOT NULL, limit_micros INTEGER NOT NULL,
        currency TEXT NOT NULL, spec_version_id TEXT, candidate_sha TEXT)""",
    """CREATE TABLE nodes (
        run_id TEXT REFERENCES runs, node_id TEXT, definition TEXT NOT NULL, state TEXT NOT NULL,
        generation INTEGER NOT NULL DEFAULT 1, input_digest TEXT, artifact TEXT,
        attempt_id TEXT, reason TEXT, ready_at REAL NOT NULL DEFAULT 0,
        PRIMARY KEY(run_id,node_id))""",
    "CREATE INDEX queue ON nodes(state,ready_at)",
    """CREATE TABLE attempts (
        attempt_id TEXT PRIMARY KEY, run_id TEXT, node_id TEXT, generation INTEGER NOT NULL,
        state TEXT NOT NULL, lease_until REAL NOT NULL, started REAL NOT NULL, finished REAL,
        endpoint_id TEXT, model_identity TEXT, endpoint_revision INTEGER, snapshot TEXT, decision TEXT,
        hold INTEGER NOT NULL DEFAULT 0, settled INTEGER NOT NULL DEFAULT 0,
        accounting TEXT, receipt TEXT, error TEXT, artifact TEXT, context_ref TEXT,
        pricing_snapshot TEXT, usage TEXT, completion_metadata TEXT,
        FOREIGN KEY(run_id,node_id) REFERENCES nodes)""",
    """CREATE TABLE events (
        run_id TEXT REFERENCES runs, seq INTEGER, at REAL NOT NULL, kind TEXT NOT NULL, data TEXT NOT NULL,
        PRIMARY KEY(run_id,seq))""",
)


class Store:
    def __init__(self, state_dir):
        self.root = Path(state_dir)
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.objects = self.root / "objects"
        self.objects.mkdir(mode=0o700, exist_ok=True)
        self.path = self.root / "workspace.sqlite3"
        self.local = threading.local()
        db = self._connect()
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("BEGIN IMMEDIATE")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version == 0:
                for stmt in SCHEMA:
                    db.execute(stmt)
                db.execute("PRAGMA user_version=1")
            elif version != 1:
                raise RuntimeError(f"不支持的存储版本 {version}")
            attempt_columns = {
                row["name"] for row in db.execute("PRAGMA table_info(attempts)")
            }
            for column in ("context_ref", "pricing_snapshot", "usage", "completion_metadata"):
                if column not in attempt_columns:
                    db.execute(f"ALTER TABLE attempts ADD COLUMN {column} TEXT")
            from clauseweft import audit

            audit.init_schema(db)
            db.commit()
        finally:
            db.close()

    def _connect(self):
        db = sqlite3.connect(self.path, isolation_level=None, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    @contextmanager
    def transaction(self):
        db = getattr(self.local, "db", None)
        if db is not None:
            # Nested application services may catch a business error and keep going.
            db.execute("SAVEPOINT nested_service")
            try:
                yield db
            except BaseException:
                db.execute("ROLLBACK TO nested_service")
                raise
            finally:
                db.execute("RELEASE nested_service")
            return
        db = self._connect()
        try:
            db.execute("BEGIN IMMEDIATE")
            self.local.db = db
            yield db
            db.commit()
        finally:
            self.local.db = None
            db.close()

    def stage_blob(self, data):
        if isinstance(data, str):
            data = data.encode()
        sha = hashlib.sha256(data).hexdigest()
        with tempfile.NamedTemporaryFile(dir=self.objects, delete=False) as staged:
            try:
                staged.write(data)
                staged.flush()
                os.fsync(staged.fileno())
                os.replace(staged.name, self.objects / sha)
                directory = os.open(self.objects, os.O_RDONLY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
            finally:
                Path(staged.name).unlink(missing_ok=True)
        return sha

    def read_blob(self, sha):
        if not isinstance(sha, str) or not re.fullmatch(r"[a-f0-9]{64}", sha):
            raise ValueError("无效的工件引用")
        data = (self.objects / sha).read_bytes()
        if hashlib.sha256(data).hexdigest() != sha:
            raise RuntimeError("工件校验失败")
        return data

    def event(self, db, run_id, kind, data):
        from clauseweft import audit

        now = time.time()
        db.execute(
            """INSERT INTO events SELECT ?,coalesce(max(seq),0)+1,?,?,? FROM events
                      WHERE run_id=?""",
            (run_id, now, kind, canonical(data), run_id),
        )
        db.execute("UPDATE runs SET revision=revision+1 WHERE run_id=?", (run_id,))
        run = db.execute("SELECT body FROM runs WHERE run_id=?", (run_id,)).fetchone()
        body = json.loads(run[0]) if run else {}
        sequence = db.execute(
            "SELECT max(seq) FROM events WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        audit.record(
            db,
            audit.SYSTEM,
            "run." + kind,
            "runs/" + run_id,
            run_id=run_id,
            project_id=body.get("project_id", "default"),
            correlation_id=getattr(self.local, "audit_correlation_id", None),
            details={
                "event_seq": sequence,
                "event_at": now,
                "evidence": data,
                "initiated_by": body.get("created_by"),
            },
        )

    def events_after(self, run_id, after=0, limit=100):
        with self.transaction() as db:
            if not db.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone():
                raise KeyError(run_id)
            return [
                dict(
                    seq=r["seq"], at=r["at"], kind=r["kind"], data=json.loads(r["data"])
                )
                for r in db.execute(
                    "SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq LIMIT ?",
                    (run_id, after, limit),
                )
            ]
