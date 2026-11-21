"""Manage workspace resources, project permissions, published templates and run ownership."""

from copy import deepcopy
import json
import time
from uuid import uuid4

from clauseweft import audit, workflow
from clauseweft.storage import canonical
from . import platform_setup
from .accounts import Accounts, BOOTSTRAP
from .http_contracts import Problem, identifier_list, match, text
from .resource_validation import KINDS as KINDS, validate_resource


def check_revision(current, expected):
    return match(current, expected)


class Platform:
    def __init__(self, app):
        self.app = app
        self.accounts = Accounts(app.store, app.request_context, self.get, self.audit)

    def init_schema(self, db):
        platform_setup.init_schema(self, db)

    def validate(self, db, kind, data):
        return validate_resource(self, db, kind, data)

    identifier_list = staticmethod(identifier_list)
    capabilities = staticmethod(Accounts.capabilities)
    password_hash = staticmethod(Accounts.password_hash)

    def public_actor(self, db, actor):
        return self.accounts.public_actor(db, actor)

    def members(self, db):
        return self.accounts.members(db)

    def validate_grants(self, db, workspace_ids, project_ids):
        return self.accounts.validate_grants(db, workspace_ids, project_ids)

    def add_member(self, db, data):
        return self.accounts.add_member(db, data)

    def update_member(self, db, member_id, data, expected):
        return self.accounts.update_member(db, member_id, data, expected)

    def login(self, username, password):
        return self.accounts.login(username, password)

    def authenticate(self, token):
        return self.accounts.authenticate(token)

    def logout(self, token):
        return self.accounts.logout(token)

    def visible_workspaces(self, db, actor):
        actor = self.public_actor(db, actor)
        identifiers = set(actor["workspace_ids"])
        return [
            self.get(db, "workspaces", row[0])
            for row in db.execute(
                "SELECT id FROM platform_resources WHERE kind='workspaces' ORDER BY created,id"
            )
            if row[0] in identifiers
        ]

    def allowed_project_ids(self, db, actor, workspace_id=None):
        actor = self.public_actor(db, actor)
        identifiers = set(actor["project_ids"])
        workspaces = set(actor["workspace_ids"])
        return {
            row["id"]
            for row in db.execute(
                "SELECT id,payload FROM platform_resources WHERE kind='projects'"
            )
            if row["id"] in identifiers
            and json.loads(row["payload"])["workspace_id"] in workspaces
            and (
                workspace_id is None
                or json.loads(row["payload"])["workspace_id"] == workspace_id
            )
        }

    def authorize_workspace(self, db, workspace_id, actor=None):
        actor = self.public_actor(db, actor or self.app.actor())
        if workspace_id not in actor["workspace_ids"]:
            raise Problem(404, "NOT_FOUND", "工作空间不存在或未授权")
        return self.get(db, "workspaces", workspace_id)

    def authorize_project(self, db, project_id, actor=None):
        if project_id not in self.allowed_project_ids(db, actor or self.app.actor()):
            raise Problem(404, "NOT_FOUND", "项目不存在或未授权")
        return self.get(db, "projects", project_id)

    def authorize_resource(self, db, kind, resource_id, actor=None):
        if kind == "workspaces":
            return self.authorize_workspace(db, resource_id, actor)
        if kind == "projects":
            return self.authorize_project(db, resource_id, actor)
        resource = self.get(db, kind, resource_id)
        self.authorize_workspace(db, resource["workspace_id"], actor)
        if kind == "requirements":
            self.authorize_project(db, resource["project_id"], actor)
        return resource

    def grant_created_project(self, db, actor, project):
        actor = self.public_actor(db, actor)
        if actor["role"] != "admin":
            row = db.execute(
                "SELECT revision FROM platform_members WHERE id=?", (actor["id"],)
            ).fetchone()
            self.update_member(
                db,
                actor["id"],
                {"project_ids": sorted(set(actor["project_ids"]) | {project["id"]})},
                row["revision"],
            )

    def audit(self, db, actor, action, resource, details=None):
        evidence = audit.sanitize(details or {})
        db.execute(
            "INSERT INTO platform_audit(at,actor_id,actor,action,resource,details) VALUES(?,?,?,?,?,?)",
            (
                time.time(),
                actor["id"],
                actor["display_name"],
                action,
                resource,
                canonical(evidence),
            ),
        )
        return audit.record(
            db,
            actor,
            action,
            resource,
            details=evidence,
            correlation_id=getattr(self.app.request_context, "correlation_id", None),
        )

    def get(self, db, kind, resource_id):
        row = db.execute(
            "SELECT * FROM platform_resources WHERE kind=? AND id=?",
            (kind, resource_id),
        ).fetchone()
        if row is None:
            raise Problem(404, "NOT_FOUND", "平台资源不存在")
        result = dict(
            json.loads(row["payload"]),
            id=row["id"],
            revision=row["revision"],
            created_at=row["created"],
            updated_at=row["updated"],
        )
        if kind == "templates":
            result["published_revision"] = db.execute(
                "SELECT max(revision) FROM platform_releases WHERE template_id=?",
                (resource_id,),
            ).fetchone()[0]
        if kind == "requirements":
            latest = db.execute(
                """SELECT r.run_id,r.status FROM runs r JOIN platform_run_links l USING(run_id)
                WHERE l.requirement_id=? ORDER BY r.created DESC LIMIT 1""",
                (resource_id,),
            ).fetchone()
            result["run_count"] = db.execute(
                "SELECT count(*) FROM platform_run_links WHERE requirement_id=?",
                (resource_id,),
            ).fetchone()[0]
            result["latest_run_id"] = latest["run_id"] if latest else None
            result["run_status"] = latest["status"] if latest else None
        return result

    def list(self, db, kind, query, actor=None):
        actor = actor or self.app.actor()
        if kind == "workspaces":
            return self.visible_workspaces(db, actor)
        workspace_id = query.get("workspace_id", "default")
        self.authorize_workspace(db, workspace_id, actor)
        projects = self.allowed_project_ids(db, actor, workspace_id)
        items = [
            self.get(db, kind, row[0])
            for row in db.execute(
                "SELECT id FROM platform_resources WHERE kind=? ORDER BY created DESC,id",
                (kind,),
            )
        ]
        items = [
            item
            for item in items
            if item.get("workspace_id") == workspace_id
            and (kind != "projects" or item["id"] in projects)
            and (kind != "requirements" or item["project_id"] in projects)
        ]
        if query.get("project_id"):
            self.authorize_project(db, query["project_id"], actor)
            items = [
                item
                for item in items
                if item.get(
                    "project_id", item.get("id") if kind == "projects" else None
                )
                == query["project_id"]
            ]
        if query.get("status"):
            items = [item for item in items if item.get("status") == query["status"]]
        return items

    def freeze_role(self, db, role_id, workspace_id):
        role = self.get(db, "roles", role_id)
        if role["workspace_id"] != workspace_id:
            raise ValueError("角色必须属于当前工作空间")
        snapshot = {key: role[key] for key in ("id", "revision", "name", "stage", "prompt", "endpoint_id")}
        snapshot["skill_ids"] = role.get("skill_ids", [])
        return snapshot

    def freeze_skills(self, db, identifiers, workspace_id=None, *, inherited=()):
        if (
            not isinstance(identifiers, list)
            or len(identifiers) > 16
            or any(not isinstance(item, str) or not item for item in identifiers)
            or len(set(identifiers)) != len(identifiers)
        ):
            raise ValueError("skill_ids 须为最多 16 个不同技能 ID")
        identifiers = list(dict.fromkeys([*inherited, *identifiers]))
        if len(identifiers) > 16:
            raise ValueError("角色和任务合计最多选择 16 个技能")
        skills = []
        for identifier in identifiers:
            skill = self.get(db, "skills", identifier)
            if workspace_id is not None and skill["workspace_id"] != workspace_id:
                raise ValueError("技能必须属于当前工作空间")
            if not skill["enabled"]:
                raise Problem(409, "SKILL_DISABLED", "请先启用所选技能")
            skills.append(
                {
                    key: skill[key]
                    for key in ("id", "revision", "name", "instructions", "data_scope")
                }
            )
        if sum(len(skill["instructions"]) for skill in skills) > 64000:
            raise ValueError("本次技能指令总量超过 64000 字符")
        return skills

    def save(self, db, kind, data, resource_id=None, expected=None):
        revision = 1
        previous = None
        created = now = time.time()
        if resource_id is not None:
            row = db.execute(
                "SELECT * FROM platform_resources WHERE kind=? AND id=?",
                (kind, resource_id),
            ).fetchone()
            if row:
                check_revision(row["revision"], expected)
                previous = json.loads(row["payload"])
                if (
                    "workspace_id" in previous
                    and data.get("workspace_id", previous["workspace_id"])
                    != previous["workspace_id"]
                ):
                    raise Problem(
                        409,
                        "WORKSPACE_OWNERSHIP_LOCKED",
                        "已有资源不能直接移到其他工作空间",
                    )
                if (
                    kind == "requirements"
                    and "project_id" in data
                    and data["project_id"] != previous["project_id"]
                ):
                    raise Problem(
                        409,
                        "REQUIREMENT_PROJECT_LOCKED",
                        "需求的项目归属固定，跨项目请创建新需求",
                    )
                data = dict(previous, **data)
                revision, created = row["revision"] + 1, row["created"]
        resource_id = resource_id or uuid4().hex
        payload = self.validate(db, kind, data)
        db.execute(
            "INSERT OR REPLACE INTO platform_resources VALUES(?,?,?,?,?,?)",
            (kind, resource_id, revision, canonical(payload), created, now),
        )
        audit.save_revision(
            db,
            kind,
            resource_id,
            revision,
            payload,
            actor=getattr(self.app.request_context, "principal", audit.SYSTEM),
            before=previous,
            project_id=payload.get("project_id")
            or (resource_id if kind == "projects" else None),
            correlation_id=getattr(self.app.request_context, "correlation_id", None),
        )
        return self.get(db, kind, resource_id)

    def publish(self, db, template_id, expected, actor):
        template = self.get(db, "templates", template_id)
        check_revision(template["revision"], expected)
        nodes = deepcopy(template["nodes"])
        for node in nodes:
            if node.get("role_id") and node.get("kind", "model") == "model":
                role = self.freeze_role(db, node["role_id"], template["workspace_id"])
                node["prompt"] = role["prompt"]
                node["role_name"], node["role_revision"] = (
                    role["name"],
                    role["revision"],
                )
                node["skill_snapshots"] = self.freeze_skills(
                    db, role.get("skill_ids", []), template["workspace_id"]
                )
                if role["endpoint_id"]:
                    node["pinned_endpoint"] = role["endpoint_id"]
        nodes = workflow.compile_sop(nodes)
        snapshot = dict(
            id=template_id,
            name=template["name"],
            revision=template["revision"],
            execution_mode=template["execution_mode"],
            workspace_id=template["workspace_id"],
            nodes=nodes,
        )
        workflow.compile(
            dict(mode=template["execution_mode"], template_snapshot=snapshot)
        )
        old_release = db.execute(
            "SELECT snapshot FROM platform_releases WHERE template_id=? AND revision=?",
            (template_id, template["revision"]),
        ).fetchone()
        if old_release:
            if canonical(snapshot) != old_release[0]:
                raise Problem(
                    409,
                    "SAVE_TEMPLATE_VERSION",
                    "角色已变化，请保存新的模板版本后再发布",
                )
            return template
        db.execute(
            "INSERT INTO platform_releases VALUES(?,?,?,?,?)",
            (
                template_id,
                template["revision"],
                canonical(snapshot),
                time.time(),
                actor["id"],
            ),
        )
        audit.record(
            db,
            actor,
            "template.published",
            "templates/" + template_id,
            after=snapshot,
            details={"revision": template["revision"]},
            correlation_id=getattr(self.app.request_context, "correlation_id", None),
        )
        return self.get(db, "templates", template_id)

    def release(self, db, template_id):
        row = db.execute(
            "SELECT snapshot FROM platform_releases WHERE template_id=? ORDER BY revision DESC LIMIT 1",
            (template_id,),
        ).fetchone()
        if row is None:
            raise Problem(409, "TEMPLATE_UNPUBLISHED", "请先发布 SOP 模板")
        return json.loads(row[0])

    def versions(self, db, template_id):
        self.get(db, "templates", template_id)
        releases = []
        for row in db.execute(
            "SELECT * FROM platform_releases WHERE template_id=? ORDER BY revision DESC",
            (template_id,),
        ):
            snapshot = json.loads(row["snapshot"])
            member = db.execute(
                "SELECT display_name FROM platform_members WHERE id=?", (row["actor"],)
            ).fetchone()
            actor = (
                member[0]
                if member
                else BOOTSTRAP["display_name"]
                if row["actor"] == BOOTSTRAP["id"]
                else row["actor"]
            )
            releases.append(
                dict(
                    template_id=template_id,
                    revision=row["revision"],
                    name=snapshot["name"],
                    published_at=row["published"],
                    actor=actor,
                    actor_id=row["actor"],
                    node_count=len(snapshot["nodes"]),
                    nodes=snapshot["nodes"],
                    execution_mode=snapshot["execution_mode"],
                )
            )
        return releases

    def link_run(self, db, run_id, plan, actor):
        project_id = plan.get("project_id", "default")
        project = self.get(db, "projects", project_id)
        if project["status"] != "active":
            raise Problem(409, "PROJECT_ARCHIVED", "归档项目不能启动新运行")
        requirement_id = plan.get("requirement_id")
        if requirement_id:
            requirement = self.get(db, "requirements", requirement_id)
            if requirement["project_id"] != project_id:
                raise ValueError("需求与运行所属项目不匹配")
            if not plan.get("conversation"):
                self.save(
                    db,
                    "requirements",
                    {"status": "in_progress"},
                    requirement_id,
                    requirement["revision"],
                )
        title = text(
            plan.get("title", plan["task"]["text"][:160]), "title", maximum=160
        )
        release = plan.get("template_snapshot", {})
        db.execute(
            "INSERT INTO platform_run_links VALUES(?,?,?,?,?,?,?)",
            (
                run_id,
                project_id,
                requirement_id,
                title,
                release.get("id"),
                release.get("revision"),
                actor["id"],
            ),
        )

    def enrich_run(self, db, run):
        row = db.execute(
            "SELECT * FROM platform_run_links WHERE run_id=?", (run["run_id"],)
        ).fetchone()
        if row:
            run = dict(
                run,
                **{
                    key: row[key]
                    for key in (
                        "project_id",
                        "requirement_id",
                        "title",
                        "template_id",
                        "template_revision",
                        "created_by",
                    )
                },
            )
            project = self.get(db, "projects", row["project_id"])
            run["project_name"] = project["name"]
            run["workspace_id"] = project["workspace_id"]
            run["is_example"] = project["is_example"]
        return run

    def overview(self, db, query, actor=None):
        actor = self.public_actor(db, actor or self.app.actor())
        workspace_id = query.get("workspace_id", "default")
        self.authorize_workspace(db, workspace_id, actor)
        project_ids = self.allowed_project_ids(db, actor, workspace_id)
        if query.get("project_id"):
            self.authorize_project(db, query["project_id"], actor)
            project_ids &= {query["project_id"]}
        runs = [
            self.enrich_run(db, self.app.runs.get(row["run_id"]))
            for row in db.execute(
                "SELECT r.run_id,l.project_id FROM runs r JOIN platform_run_links l USING(run_id) ORDER BY r.created DESC,r.run_id"
            )
            if row["project_id"] in project_ids
        ]
        costs, approvals = {}, []
        for run in runs:
            budget = run["budget"]
            amount = costs.setdefault(
                budget["currency"],
                {"currency": budget["currency"], "settled_micros": 0, "held_micros": 0},
            )
            amount["settled_micros"] += budget["settled_micros"]
            amount["held_micros"] += budget["held_micros"]
            for node in run["nodes"]:
                if node["kind"] == "gate" and node["state"] == "needs_input":
                    approvals.append(
                        dict(
                            run_id=run["run_id"],
                            title=run.get("title", run["run_id"]),
                            node_id=node["node_id"],
                            label=node.get("label", node["node_id"]),
                            project_id=run.get("project_id"),
                            project_name=run.get("project_name"),
                            created=run["created"],
                            mode=run["mode"],
                        )
                    )
        projects = [
            self.get(db, "projects", project_id) for project_id in sorted(project_ids)
        ]
        activity = (
            [
                entry
                for entry in audit.list_entries(db)
                if entry.get("project_id") in project_ids
            ][:12]
            if actor["role"] == "admin"
            else []
        )
        workspace = self.get(db, "workspaces", workspace_id)
        return dict(
            workspace_id=workspace_id,
            is_example=workspace["is_example"],
            projects=len(projects),
            agent_jobs=self.app.agent_jobs.overview(db, actor, project_ids),
            project_list=projects,
            runs=len(runs),
            active_runs=sum(run["status"] in ("queued", "running") for run in runs),
            completed_runs=sum(run["status"] == "completed" for run in runs),
            pending_approvals=len(approvals)
            if "approve" in actor["capabilities"]
            else 0,
            endpoints=len(workspace["endpoint_ids"]) if actor["role"] == "admin" else 0,
            members=len(self.members(db)) if actor["role"] == "admin" else 0,
            costs=list(costs.values()) if "usage" in actor["capabilities"] else [],
            recent_runs=runs[:8],
            approvals=approvals if "approve" in actor["capabilities"] else [],
            activity=activity,
            requirements=len(self.list(db, "requirements", query, actor)),
        )

    def audit_rows(self, db):
        return [
            dict(row, details=json.loads(row["details"]))
            for row in db.execute("SELECT * FROM platform_audit ORDER BY id DESC")
        ]
