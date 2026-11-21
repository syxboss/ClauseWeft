"""Claim, renew and expire persisted worker leases."""

import json
import time
from uuid import uuid4

from .state import refresh

LEASE_SECONDS = 90


def expire(store, db):
    expired = db.execute(
        """SELECT * FROM attempts WHERE state IN ('leased','dispatched') AND lease_until<=?""",
        (time.time(),),
    ).fetchall()
    for att in expired:
        sent = att["state"] == "dispatched"
        state = "uncertain" if sent else "ready"
        db.execute(
            """UPDATE attempts SET state=?,accounting=?,error='lease_expired',finished=? WHERE attempt_id=?""",
            (state if sent else "expired", "uncertain" if sent else None, time.time(), att["attempt_id"]),
        )
        db.execute(
            """UPDATE nodes SET state=?,reason='lease_expired' WHERE attempt_id=? AND state IN ('leased','dispatched')""",
            (state, att["attempt_id"]),
        )
        store.event(
            db, att["run_id"], "lease_expired", {"attempt_id": att["attempt_id"], "dispatched": sent}
        )
        refresh(db, att["run_id"])


def claim(store):
    with store.transaction() as db:
        expire(store, db)
        for row in db.execute(
            """SELECT n.*,r.body FROM nodes n JOIN runs r USING(run_id)
                                  WHERE n.state='ready' AND ready_at<=? AND r.status IN ('queued','running')
                                  ORDER BY ready_at,r.created,n.rowid""",
            (time.time(),),
        ):
            body = json.loads(row["body"])
            count = db.execute(
                """SELECT count(*) FROM nodes WHERE run_id=? AND state IN ('leased','dispatched')""",
                (row["run_id"],),
            ).fetchone()[0]
            if count >= body.get("max_concurrency", 4):
                continue
            att = uuid4().hex
            db.execute(
                """INSERT INTO attempts(attempt_id,run_id,node_id,generation,state,lease_until,started)
                          VALUES(?,?,?,?,'leased',?,?)""",
                (
                    att,
                    row["run_id"],
                    row["node_id"],
                    row["generation"],
                    time.time() + LEASE_SECONDS,
                    time.time(),
                ),
            )
            db.execute(
                "UPDATE nodes SET state='leased',attempt_id=?,reason=NULL WHERE run_id=? AND node_id=?",
                (att, row["run_id"], row["node_id"]),
            )
            store.event(db, row["run_id"], "claimed", {"node_id": row["node_id"], "attempt_id": att})
            refresh(db, row["run_id"])
            return att
        return None


def renew(store, attempt_id):
    with store.transaction() as db:
        now = time.time()
        return bool(
            db.execute(
                """UPDATE attempts SET lease_until=? WHERE attempt_id=?
                                   AND state IN ('leased','dispatched') AND lease_until>?
                                   AND EXISTS(SELECT 1 FROM nodes WHERE nodes.attempt_id=attempts.attempt_id
                                              AND nodes.state=attempts.state)""",
                (now + LEASE_SECONDS, attempt_id, now),
            ).rowcount
        )
