"""Private, opt-in instructions for one member in one workspace."""

from .accounts import BOOTSTRAP
from .http_contracts import Problem, match, text


def init_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS member_preferences (
        member_id TEXT NOT NULL REFERENCES platform_members(id),
        workspace_id TEXT NOT NULL, instructions TEXT NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(member_id, workspace_id))""")


def read(db, actor, workspace_id):
    if actor["id"] == BOOTSTRAP["id"]:
        raise Problem(403, "NAMED_ACCOUNT_REQUIRED", "请使用具名账号管理个人偏好")
    row = db.execute(
        "SELECT revision, instructions FROM member_preferences WHERE member_id=? AND workspace_id=?",
        (actor["id"], workspace_id),
    ).fetchone()
    return dict(row) if row else {"revision": 0, "instructions": ""}


def save(db, actor, workspace_id, data, expected):
    if set(data) != {"instructions"}:
        raise ValueError("偏好仅接受 instructions 字段")
    value = text(data["instructions"], "偏好", empty=True, maximum=4000)
    current = read(db, actor, workspace_id)
    match(current["revision"], expected)
    revision = current["revision"] + 1
    db.execute(
        """INSERT INTO member_preferences VALUES (?,?,?,?)
           ON CONFLICT(member_id,workspace_id) DO UPDATE
           SET instructions=excluded.instructions, revision=excluded.revision""",
        (actor["id"], workspace_id, value, revision),
    )
    # The shared idempotency ledger needs only the acknowledgement.
    return {"revision": revision}
