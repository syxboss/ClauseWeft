"""Manage team membership, credential rotation, login throttling and sessions."""

import hashlib
import hmac
import json
import re
import secrets
import time
from uuid import uuid4

from clauseweft import audit
from clauseweft.storage import canonical
from .http_contracts import Problem, identifier_list, match, text


BOOTSTRAP = {
    "id": "bootstrap-admin",
    "username": "admin",
    "display_name": "平台管理员",
    "role": "admin",
}


class Accounts:
    def __init__(self, store, request_context, get_resource, record_audit):
        self.store = store
        self.request_context = request_context
        self.get_resource = get_resource
        self.record_audit = record_audit

    @staticmethod
    def capabilities(actor):
        result = ["read"]
        if actor.get("role") in ("admin", "operator"):
            result += ["write", "approve", "usage"]
        if actor.get("role") == "admin":
            result.append("admin")
        return result

    def public_actor(self, db, actor):
        """Refresh permissions from authoritative account state on every request."""
        if actor["id"] == BOOTSTRAP["id"]:
            result = dict(BOOTSTRAP)
        else:
            row = db.execute(
                "SELECT * FROM platform_members WHERE id=? AND active=1", (actor["id"],)
            ).fetchone()
            if row is None:
                raise Problem(401, "UNAUTHORIZED", "账号已停用或不存在")
            result = {
                key: row[key] for key in ("id", "username", "display_name", "role")
            }
            result["workspace_ids"] = json.loads(row["workspace_ids"])
            result["project_ids"] = json.loads(row["project_ids"])
        if result["role"] == "admin":
            result["workspace_ids"] = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM platform_resources WHERE kind='workspaces' ORDER BY id"
                )
            ]
            result["project_ids"] = [
                row[0]
                for row in db.execute(
                    "SELECT id FROM platform_resources WHERE kind='projects' ORDER BY id"
                )
            ]
        if db.execute(
            "SELECT 1 FROM personal_installation WHERE owner_id=?", (actor["id"],)
        ).fetchone():
            result["personal_owner"] = True
        result["capabilities"] = self.capabilities(result)
        return result

    def members(self, db):
        result = []
        for row in db.execute(
            "SELECT id,username,display_name,role,revision,active,created AS created_at,workspace_ids,project_ids FROM platform_members ORDER BY created"
        ):
            member = dict(row)
            for field in ("workspace_ids", "project_ids"):
                member[field] = json.loads(member[field])
            result.append(member)
        return result

    def validate_grants(self, db, workspace_ids, project_ids):
        identifier_list(workspace_ids, "workspace_ids")
        identifier_list(project_ids, "project_ids")
        for identifier in workspace_ids:
            self.get_resource(db, "workspaces", identifier)
        for identifier in project_ids:
            project = self.get_resource(db, "projects", identifier)
            if project["workspace_id"] not in workspace_ids:
                raise ValueError("项目授权必须同时包含所属工作空间")
        return sorted(workspace_ids), sorted(project_ids)

    def add_member(self, db, data):
        if data.keys() - {
            "username",
            "display_name",
            "role",
            "password",
            "workspace_ids",
            "project_ids",
        }:
            raise ValueError("不支持的成员字段")
        username = text(data.get("username"), "username", maximum=64)
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.@-]{1,63}", username):
            raise ValueError("用户名须为 2 到 64 位字母、数字或 . _ @ -")
        display = text(data.get("display_name", username), "display_name", maximum=160)
        role = data.get("role", "operator")
        if role not in ("admin", "operator", "viewer"):
            raise ValueError("成员角色无效")
        password = data.get("password")
        if not isinstance(password, str) or not 12 <= len(password) <= 512:
            raise ValueError("密码须为 12 到 512 个字符")
        if db.execute(
            "SELECT 1 FROM platform_members WHERE username=?", (username,)
        ).fetchone():
            raise Problem(409, "MEMBER_EXISTS", "用户名已存在")
        workspace_ids, project_ids = self.validate_grants(
            db, data.get("workspace_ids", []), data.get("project_ids", [])
        )
        salt = secrets.token_hex(16)
        hashed = self.password_hash(password, salt)
        mid = uuid4().hex
        db.execute(
            "INSERT INTO platform_members(id,username,display_name,role,password_hash,salt,revision,active,created,workspace_ids,project_ids) VALUES(?,?,?,?,?,?,1,1,?,?,?)",
            (
                mid,
                username,
                display,
                role,
                hashed,
                salt,
                time.time(),
                canonical(workspace_ids),
                canonical(project_ids),
            ),
        )
        member = next(member for member in self.members(db) if member["id"] == mid)
        audit.save_revision(
            db,
            "members",
            mid,
            member["revision"],
            member,
            actor=getattr(self.request_context, "principal", audit.SYSTEM),
            correlation_id=getattr(self.request_context, "correlation_id", None),
        )
        return member

    def update_member(self, db, member_id, data, expected):
        if data.keys() - {
            "display_name",
            "role",
            "password",
            "active",
            "workspace_ids",
            "project_ids",
        }:
            raise ValueError("不支持的成员字段")
        row = db.execute(
            "SELECT * FROM platform_members WHERE id=?", (member_id,)
        ).fetchone()
        if row is None:
            raise Problem(404, "NOT_FOUND", "成员不存在")
        match(row["revision"], expected)
        previous = next(
            member for member in self.members(db) if member["id"] == member_id
        )
        display = text(
            data.get("display_name", row["display_name"]), "display_name", maximum=160
        )
        role = data.get("role", row["role"])
        active = data.get("active", bool(row["active"]))
        if role not in ("admin", "operator", "viewer") or type(active) is not bool:
            raise ValueError("成员权限或启用状态无效")
        workspace_ids, project_ids = self.validate_grants(
            db,
            data.get("workspace_ids", json.loads(row["workspace_ids"])),
            data.get("project_ids", json.loads(row["project_ids"])),
        )
        hashed, salt = row["password_hash"], row["salt"]
        if "password" in data:
            password = data["password"]
            if not isinstance(password, str) or not 12 <= len(password) <= 512:
                raise ValueError("密码须为 12 到 512 个字符")
            salt = secrets.token_hex(16)
            hashed = self.password_hash(password, salt)
        db.execute(
            """UPDATE platform_members SET display_name=?,role=?,active=?,password_hash=?,salt=?,workspace_ids=?,project_ids=?,revision=revision+1 WHERE id=?""",
            (
                display,
                role,
                int(active),
                hashed,
                salt,
                canonical(workspace_ids),
                canonical(project_ids),
                member_id,
            ),
        )
        revoked = not active or "password" in data
        if revoked:
            db.execute("DELETE FROM platform_sessions WHERE member_id=?", (member_id,))
        member = next(
            member for member in self.members(db) if member["id"] == member_id
        )
        audit.save_revision(
            db,
            "members",
            member_id,
            member["revision"],
            member,
            actor=getattr(self.request_context, "principal", audit.SYSTEM),
            before=previous,
            correlation_id=getattr(self.request_context, "correlation_id", None),
        )
        audit.record(
            db,
            getattr(self.request_context, "principal", audit.SYSTEM),
            "member.access_changed",
            "members/" + member_id,
            before={
                key: previous[key]
                for key in ("role", "active", "workspace_ids", "project_ids")
            },
            after={
                key: member[key]
                for key in ("role", "active", "workspace_ids", "project_ids")
            },
            details={
                "credentials_rotated": "password" in data,
                "sessions_revoked": revoked,
            },
            correlation_id=getattr(self.request_context, "correlation_id", None),
        )
        return member

    @staticmethod
    def password_hash(password, salt):
        return hashlib.scrypt(
            password.encode(),
            salt=bytes.fromhex(salt),
            n=16384,
            r=8,
            p=1,
            maxmem=64 * 1024 * 1024,
        ).hex()

    def login(self, username, password):
        anonymous = {"id": "anonymous", "display_name": "未登录"}
        if (
            not isinstance(username, str)
            or not isinstance(password, str)
            or len(username) > 64
            or len(password) > 512
        ):
            with self.store.transaction() as db:
                audit.record(
                    db,
                    anonymous,
                    "auth.login",
                    "session",
                    outcome="denied",
                    details={"reason": "invalid_credentials"},
                )
            raise Problem(401, "INVALID_CREDENTIALS", "用户名或密码不正确")
        now = time.time()
        with self.store.transaction() as db:
            failure = db.execute(
                "SELECT attempts,until FROM platform_login_failures WHERE username=?",
                (username,),
            ).fetchone()
            throttled = bool(failure and failure[0] >= 8 and failure[1] > now)
            member = db.execute(
                "SELECT * FROM platform_members WHERE username=? AND active=1",
                (username,),
            ).fetchone()
            salt = member["salt"] if member else "0" * 32
            matched = (
                False
                if throttled
                else hmac.compare_digest(
                    self.password_hash(password, salt),
                    member["password_hash"] if member else "0" * 128,
                )
            )
            if not member or not matched:
                if not throttled:
                    attempts = failure[0] + 1 if failure and failure[1] > now else 1
                    db.execute(
                        "INSERT OR REPLACE INTO platform_login_failures VALUES(?,?,?)",
                        (username, attempts, now + 300),
                    )
                audit.record(
                    db,
                    anonymous,
                    "auth.login",
                    "session",
                    outcome="denied",
                    details={
                        "username": username,
                        "reason": "throttled" if throttled else "invalid_credentials",
                    },
                )
                valid = False
            else:
                db.execute(
                    "DELETE FROM platform_login_failures WHERE username=?", (username,)
                )
                session = self.issue_session(db, dict(member), now)
                valid = True
        if throttled:
            raise Problem(429, "LOGIN_THROTTLED", "尝试过多，请稍后重试")
        if not valid:
            raise Problem(401, "INVALID_CREDENTIALS", "用户名或密码不正确")
        return session

    def issue_session(self, db, member, now):
        token = secrets.token_urlsafe(32)
        expires = now + 43200
        db.execute("DELETE FROM platform_sessions WHERE expires<=?", (now,))
        user = self.public_actor(db, member)
        db.execute(
            "INSERT INTO platform_sessions VALUES(?,?,?,?)",
            (hashlib.sha256(token.encode()).hexdigest(), user["id"], now, expires),
        )
        self.record_audit(db, user, "auth.login", "session", {"expires_at": expires})
        return {"token": token, "user": user, "expires_at": expires}

    def authenticate(self, token):
        with self.store.transaction() as db:
            row = db.execute(
                """SELECT m.id,m.username,m.display_name,m.role FROM platform_sessions s
                JOIN platform_members m ON m.id=s.member_id WHERE s.token_hash=? AND s.expires>? AND m.active=1""",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
            if row is None:
                raise Problem(401, "UNAUTHORIZED", "登录已过期，请重新登录")
            return self.public_actor(db, dict(row))

    def logout(self, token):
        with self.store.transaction() as db:
            row = db.execute(
                """SELECT m.id,m.display_name FROM platform_sessions s
                JOIN platform_members m ON m.id=s.member_id WHERE s.token_hash=?""",
                (hashlib.sha256(token.encode()).hexdigest(),),
            ).fetchone()
            db.execute(
                "DELETE FROM platform_sessions WHERE token_hash=?",
                (hashlib.sha256(token.encode()).hexdigest(),),
            )
            if row:
                audit.record(
                    db,
                    dict(row),
                    "auth.logout",
                    "session",
                    details={"session_revoked": True},
                )
