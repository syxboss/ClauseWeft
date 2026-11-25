"""Application transactions and resource ownership for one local workspace."""

import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading

from clauseweft import audit, project_tools, requirements, reviews, testing, workflow
from clauseweft.harness import Harness
from clauseweft.usage import Usage
from clauseweft.catalog import Catalog, GatewayFailure
from clauseweft.runs import Runs
from clauseweft.storage import Store
from . import access_policy, audit_evidence, evaluation_routes, personal_preferences, project_memory, run_planning, run_routes
from .agent_jobs import AgentJobs
from .delivery import Delivery
from .http_contracts import Problem, encode, ident, match, page, require_object
from .platform import BOOTSTRAP, KINDS, Platform


class Application:
    def __init__(self, state, config=None):
        self.root = Path(state).resolve()
        cfg = json.loads(Path(config).read_text(encoding="utf-8")) if config else {}
        require_object(
            cfg, ("workspace_id", "gateways", "repositories", "suites", "data_scope",
                  "project_references", "agent_runners", "mcp_servers")
        )
        for section in ("gateways", "repositories", "suites", "project_references", "mcp_servers"):
            entries = cfg.get(section, {})
            if not isinstance(entries, dict) or any(
                not isinstance(key, str) or not key or not isinstance(v, dict)
                for key, v in entries.items()
            ):
                raise ValueError(f"{section} 必须为资源 ID 到配置的映射")
        self.references = project_tools.load_references(cfg.get("project_references", {}))
        self.mcp_servers = project_tools.load_mcp_servers(cfg.get("mcp_servers", {}))
        for gateway in cfg.get("gateways", {}).values():
            require_object(gateway, ("url", "token_env"), ("url",))
            if not isinstance(gateway["url"], str):
                raise ValueError("网关 URL 无效")
        for repo in cfg.get("repositories", {}).values():
            require_object(
                repo,
                ("path", "allowlist", "paths", "data_scope", "suite_id", "suite_ids"),
                ("path", "allowlist"),
            )
            if (
                not isinstance(repo["path"], str)
                or not Path(repo["path"]).is_absolute()
            ):
                raise ValueError("可信仓库必须配置绝对路径")
            if repo.get("data_scope", "local_only") not in ("local_only", "public"):
                raise ValueError("仓库数据策略无效")
            if not isinstance(repo.get("paths", {}), dict):
                raise ValueError("工作包 paths 必须为映射")
        for suite in cfg.get("suites", {}).values():
            testing.freeze_suite("config-check", suite)
        self.workspace_id = cfg.get("workspace_id", "local")
        if not isinstance(self.workspace_id, str) or not self.workspace_id.strip():
            raise ValueError("workspace_id 须为非空文本")
        self.scope = cfg.get("data_scope", "local_only")
        if self.scope not in ("local_only", "public"):
            raise ValueError("工作区数据策略无效")
        self.store = Store(self.root)
        self.catalog = Catalog(self.store, cfg.get("gateways", {}))
        self.runs = Runs(
            self.store, self.catalog, scope_for=self.current_scope,
            models_for=self.execution_models, after_finish=self._after_attempt_finish,
        )
        self.delivery = Delivery(
            self, cfg.get("repositories", {}), cfg.get("suites", {})
        )
        self.agent_jobs = AgentJobs(self, cfg.get("agent_runners", {}))
        self.request_context = threading.local()
        self.platform = Platform(self)
        self.harness = Harness(self)
        self.usage = Usage(self)
        with self.store.transaction() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS api_workspace (slot INTEGER PRIMARY KEY CHECK(slot=1), id TEXT NOT NULL)"
            )
            db.execute(
                "INSERT OR IGNORE INTO api_workspace VALUES(1,?)", (self.workspace_id,)
            )
            if (
                db.execute("SELECT id FROM api_workspace WHERE slot=1").fetchone()[0]
                != self.workspace_id
            ):
                raise ValueError("状态目录属于另一工作区")
            db.execute(
                """CREATE TABLE IF NOT EXISTS api_probes (run_id TEXT PRIMARY KEY REFERENCES runs,
                       endpoint_id TEXT, revision INTEGER, capability TEXT)"""
            )
            requirements.init_schema(db)
            db.execute(
                "CREATE TABLE IF NOT EXISTS api_requests (action TEXT, key TEXT, digest TEXT NOT NULL, status INTEGER, response TEXT, PRIMARY KEY(action,key))"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS api_context (run_id TEXT PRIMARY KEY, config TEXT NOT NULL)"
            )
            db.execute(
                "CREATE TABLE IF NOT EXISTS api_evaluations (id TEXT PRIMARY KEY, kind TEXT, payload TEXT, grades TEXT, revision INTEGER)"
            )
            self.platform.init_schema(db)
            personal_preferences.init_schema(db)
            project_memory.init_schema(db)
            self.harness.init_schema(db)
            self.delivery.init_schema(db)
            self.agent_jobs.init_schema(db)
            self.record_deployment(db, cfg)

    def record_deployment(self, db, config):
        return audit_evidence.record_deployment(self.store, db, config)

    def repo_binding(self, repo):
        return hashlib.sha256(
            encode([repo["path"], repo["allowlist"], repo.get("paths", {})]).encode()
        ).hexdigest()

    def current_scope(self, body):
        return access_policy.current_scope(self, body)

    def execution_authorized(self, body, actor=None):
        return access_policy.execution_authorized(self, body, actor)

    def execution_models(self, body):
        return access_policy.execution_models(self, body)

    def run(self, rid):
        try:
            with self.store.transaction() as db:
                return self.platform.enrich_run(db, self.runs.get(rid))
        except KeyError:
            raise Problem(404, "RUN_NOT_FOUND", "运行不存在") from None

    def actor(self):
        return getattr(self.request_context, "principal", BOOTSTRAP)

    def handle(
        self,
        method,
        parts,
        query,
        body,
        key=None,
        expected=None,
        principal=None,
        correlation_id=None,
    ):
        actor = principal or BOOTSTRAP
        self.request_context.principal = actor
        correlation_id = correlation_id or ident()
        self.request_context.correlation_id = correlation_id
        self.store.local.audit_actor = actor
        self.store.local.audit_correlation_id = correlation_id
        try:
            with self.store.transaction() as db:
                actor = self.platform.public_actor(db, actor)
                self.request_context.principal = actor
                self.store.local.audit_actor = actor
                self.authorize(db, method, parts, query, body)
            result = self._handle(method, parts, query, body, key, expected)
        except Exception as error:
            status = (
                error.status
                if isinstance(error, Problem)
                else 422
                if isinstance(error, (ValueError, KeyError))
                else 500
            )
            self.record_request(
                method,
                parts,
                query,
                body,
                actor,
                correlation_id,
                status,
                "denied" if status in (401, 403) else "failed",
            )
            raise
        else:
            self.record_request(
                method,
                parts,
                query,
                body,
                actor,
                correlation_id,
                result[0],
                "read" if method == "GET" else "succeeded",
                hashlib.sha256(encode(result[1]).encode()).hexdigest(),
            )
            return result
        finally:
            del self.request_context.principal
            del self.request_context.correlation_id
            self.store.local.audit_actor = None
            self.store.local.audit_correlation_id = None

    def record_request(
        self,
        method,
        parts,
        query,
        body,
        actor,
        correlation_id,
        status,
        outcome,
        response_hash=None,
    ):
        resource = "/".join(parts)
        if parts[:2] == ("me", "preferences"):
            body, query = {}, {}
        elif parts[:1] == ("agent-jobs",):
            body = {key: value for key, value in body.items() if key != "prompt"}
        run_id = parts[1] if len(parts) > 1 and parts[0] == "runs" else None
        project_id = body.get("project_id") or query.get("project_id")
        if not isinstance(project_id, str):
            project_id = None
        try:
            request_hash = hashlib.sha256(
                encode(audit.sanitize(body)).encode()
            ).hexdigest()
        except (ValueError, TypeError, RecursionError):
            request_hash = None
        with self.store.transaction() as db:
            if run_id:
                row = db.execute(
                    "SELECT project_id FROM platform_run_links WHERE run_id=?",
                    (run_id,),
                ).fetchone()
                project_id = row[0] if row else None
            audit.record(
                db,
                actor,
                "api." + method.lower(),
                resource,
                outcome=outcome,
                run_id=run_id,
                project_id=project_id,
                correlation_id=correlation_id,
                details={
                    "status": status,
                    "query": audit.sanitize(query),
                    "request": audit.sanitize(body),
                    "request_sha256": request_hash,
                    "request_hash_scope": "redacted",
                    "response_sha256": response_hash,
                },
            )

    def authorize(self, db, method, parts, query, body):
        if parts[:1] == ("agent-jobs",):
            actor = self.platform.public_actor(db, self.actor())
            self.request_context.principal = actor
            self.store.local.audit_actor = actor
            return self.agent_jobs.authorize(db, method, parts, query, body)
        return access_policy.authorize(self, db, method, parts, query, body)

    def _handle(self, method, parts, query, body, key=None, expected=None):
        data = dict(body)
        accepts_workspace = (
            parts and parts[0] in KINDS and len(parts) <= 2
            or parts in (("runs",), ("routing", "previews"), ("evaluations",))
            or len(parts) == 3 and parts[0] == "endpoints" and parts[2] == "probes"
        )
        if not accepts_workspace:
            data.pop("workspace_id", None)
        try:
            if method == "GET":
                with self.store.transaction() as db:
                    self.authorize(db, method, parts, query, body)
                    return 200, self.read(db, parts, query)
            if method == "POST" and parts == ("routing", "previews"):
                plan = self.plan(data)
                if plan["mode"] == "workflow":
                    repo = self.delivery.repositories.get(plan.get("repo_id"))
                    if repo is None:
                        raise ValueError("完整流程需要已登记的仓库")
                    if (
                        repo.get("data_scope", "local_only") == "local_only"
                        and plan["task"]["data_scope"] != "local_only"
                    ):
                        raise Problem(403, "DATA_SCOPE", "此仓库仅允许本地处理")
                    plan["repo_binding"] = self.repo_binding(repo)
                result = self.runs.preview(plan)
                result["budget"] = dict(plan["budget"], source=plan.get("budget_source", "explicit"))
                if plan["mode"] in ("workflow", "sop", "complex", "tool"):
                    result["nodes"] = workflow.compile(plan)
                    result["preview_node"] = result["nodes"][0]["node_id"]
                    result["note"] = "下游使用前序产物，实际派发时重新计算端点及预算"
                    if plan["mode"] == "complex":
                        result["note"] = "此处预览规划节点。规划完成后生成 2–8 个并行分工，全部成功后执行汇总。"
                return 200, result
            action = encode([method, parts])
            if self.actor()["id"] != BOOTSTRAP["id"]:
                action = encode([method, parts, self.actor()["id"]])
            digest = hashlib.sha256(encode([body, expected]).encode()).hexdigest()

            def cached(db):
                row = db.execute(
                    "SELECT * FROM api_requests WHERE action=? AND key=?", (action, key)
                ).fetchone()
                if row:
                    if row["digest"] != digest:
                        raise Problem(409, "KEY_CONFLICT", "同一幂等键已用于不同请求")
                    response = json.loads(row["response"])
                    if parts and parts[0] in KINDS and response.get("id"):
                        self.platform.authorize_resource(db, parts[0], response["id"])
                    if response.get("run_id"):
                        link = db.execute("SELECT project_id FROM platform_run_links WHERE run_id=?", (response["run_id"],)).fetchone()
                        if link is None:
                            raise Problem(404, "NOT_FOUND", "运行不存在或未授权")
                        self.platform.authorize_project(db, link[0])
                    return row["status"], response

            # Registration checks can use Git or the network. Recheck the revision
            # and idempotency inside the committing transaction after preparation.
            prepared = None
            if method == "POST" and (
                parts in (("runs",), ("agent-jobs",))
                or (len(parts) == 3 and parts[2] == "probes")
                or (len(parts) == 3 and parts[0] == "agent-jobs" and parts[2] == "tests")
            ):
                with self.store.transaction() as db:
                    self.authorize(db, method, parts, query, body)
                    result = cached(db)
                    if result:
                        return result
                if parts == ("runs",):
                    plan = self.plan(data)
                    prepared = {
                        "plan": plan,
                        "context": self.delivery.prepare_context(plan),
                    }
                elif parts == ("agent-jobs",):
                    prepared = self.agent_jobs.prepare(data)
                elif parts[0] == "agent-jobs" and parts[2] == "tests":
                    prepared = self.agent_jobs.prepare_test(parts[1], data)
                elif parts[0] == "endpoints":
                    require_object(data, ("kind", "budget", "project_id", "workspace_id"))
                    kind = data.get("kind", "connectivity")
                    if kind not in ("connectivity", "structured_output"):
                        raise ValueError("未知探针类型")
                    match(self.catalog.get(parts[1])["revision"], expected)
                    if kind == "connectivity":
                        prepared = self.catalog.probe(parts[1])
            with self.store.transaction() as db:
                self.authorize(db, method, parts, query, body)
                result = cached(db)
                if result:
                    return result
                status, result = self.write(db, method, parts, data, expected, prepared)
                db.execute(
                    "INSERT INTO api_requests VALUES (?,?,?,?,?)",
                    (action, key, digest, status, encode(result)),
                )
                target = "/".join(parts)
                resource_id = (
                    result.get("id")
                    or result.get("run_id")
                    or result.get("endpoint_id")
                )
                self.platform.audit(
                    db,
                    self.actor(),
                    method.lower() + "." + parts[0],
                    target,
                    {"resource_id": resource_id, "reason": data.get("reason", "")},
                )
                return status, result
        except GatewayFailure as exc:
            raise Problem(502, "GATEWAY_FAILURE", exc.code) from exc
        except KeyError as exc:
            raise Problem(404, "NOT_FOUND", "资源或必需引用不存在") from exc
        except (RuntimeError, reviews.Conflict) as exc:
            raise Problem(409, "CONFLICT", str(exc)) from exc

    def plan(self, data, *, inherited=None):
        return run_planning.plan(self, data, inherited=inherited)

    def task_budget(self, data, task, mode):
        return run_planning.task_budget(self, data, task, mode)

    def context(self, db, rid):
        row = db.execute(
            "SELECT config FROM api_context WHERE run_id=?", (rid,)
        ).fetchone()
        return json.loads(row[0]) if row else {}

    def start(self, db, plan, context):
        return run_planning.start(self, db, plan, context)

    def read(self, db, p, q):
        if p[:1] == ("agent-jobs",):
            return self.agent_jobs.read(db, p, q)
        if p[:2] == ("me", "preferences"):
            return personal_preferences.read(db, self.actor(), p[2])
        if p == ("auth", "me"):
            return self.platform.public_actor(db, self.actor())
        if p == ("overview",):
            return self.platform.overview(db, q)
        if p == ("usage",):
            return self.usage.report(db, self.actor(), q)
        if p == ("members",):
            return page(self.platform.members(db), q)
        if p == ("audit",):
            filters = dict(q)
            if filters.get("actor"):
                filters["actor_id"] = filters.pop("actor")
            return page(audit.list_entries(db, filters), q)
        if p == ("audit", "verify"):
            return audit.verify(db)
        if p in (("audit", "exports"), ("audit", "export")):
            return audit_evidence.export(self, db, q)
        if p == ("settings",):
            return {
                "workspace_id": self.workspace_id,
                "data_scope": self.scope,
                "repositories": [
                    {
                        "id": key,
                        "allowlist": value["allowlist"],
                        "data_scope": value.get("data_scope", "local_only"),
                        "suite_id": value.get("suite_id"),
                    }
                    for key, value in self.delivery.repositories.items()
                ],
                "suites": list(self.delivery.suites),
                "gateways": list(self.catalog.gateways),
                "session_hours": 12,
                "access_model": "trusted-team-rbac",
            }
        if p and p[0] in KINDS:
            if len(p) == 1:
                items = self.platform.list(db, p[0], q)
                if p[0] == "workspaces" and "admin" not in self.actor()["capabilities"]:
                    items = [{key: value for key, value in item.items() if key not in {"repository_ids", "endpoint_ids"}} for item in items]
                return page(items, q)
            if len(p) == 2:
                item = self.platform.get(db, p[0], p[1])
                if p[0] == "workspaces" and "admin" not in self.actor()["capabilities"]:
                    item = {key: value for key, value in item.items() if key not in {"repository_ids", "endpoint_ids"}}
                return item
            if len(p) == 3 and p[0] == "templates" and p[2] == "versions":
                return page(self.platform.versions(db, p[1]), q)
            if len(p) == 3 and p[0] == "projects" and p[2] == "memories":
                self.platform.get(db, "projects", p[1])
                return page(project_memory.list_memories(db, p[1], q), q)
            if len(p) == 4 and p[0] == "projects" and p[2] == "memories":
                self.platform.get(db, "projects", p[1])
                return project_memory.get(db, p[1], p[3])
            if len(p) == 3 and p[2] == "history":
                self.platform.get(db, p[0], p[1])
                return page(audit.history(db, p[0], p[1]), q)
        if p == ("workspace",):
            workspace = self.platform.authorize_workspace(db, q.get("workspace_id") or "default")
            can_write = "write" in self.actor()["capabilities"]
            repositories = [key for key in workspace["repository_ids"] if key in self.delivery.repositories] if can_write else []
            suites = sorted({suite for key in repositories for suite in self.delivery.repositories[key].get("suite_ids", [self.delivery.repositories[key].get("suite_id")]) if suite in self.delivery.suites})
            fields = {"endpoint_id", "enabled", "allowed", "capabilities", "fit", "location", "currency", "input_micros_per_million", "output_micros_per_million", "context_tokens", "max_output_tokens", "identity_verified"}
            return {
                "workspace_id": workspace["id"],
                "name": workspace["name"],
                "is_example": workspace.get("is_example", False),
                "project_references": {
                    key: self.references[key]["name"]
                    for key in self.platform.allowed_project_ids(db, self.actor(), workspace["id"])
                    if can_write and key in self.references
                },
                "has_personal_preferences": self.actor()["id"] != BOOTSTRAP["id"] and
                    bool(personal_preferences.read(db, self.actor(), workspace["id"])["instructions"]),
                "data_scope": self.scope,
                "repositories": repositories,
                "suites": suites,
                "model_options": [
                    {key: value for key, value in model.items() if key in fields}
                    for model in self.catalog.list()
                    if can_write and model["endpoint_id"] in workspace["endpoint_ids"]
                ],
            }
        if p == ("endpoints",):
            return page(self.catalog.list(), q)
        if len(p) == 2 and p[0] == "endpoints":
            return self.catalog.get(p[1])
        if len(p) == 3 and p[0] == "endpoints" and p[2] == "probes":
            model = self.catalog.get(p[1])
            records = []
            for row in db.execute(
                "SELECT * FROM api_probes WHERE endpoint_id=? ORDER BY rowid DESC",
                (p[1],),
            ):
                run = self.run(row["run_id"])
                node = run["nodes"][0]
                verified = False
                if node["state"] == "succeeded" and node["artifact"]:
                    try:
                        verified = json.loads(
                            self.store.read_blob(node["artifact"])
                        ) == {"check": "ok"}
                    except (ValueError, RecursionError):
                        pass
                records.append(
                    dict(
                        row,
                        created=run["created"],
                        current=row["revision"] == model["revision"],
                        verified=verified,
                        status=run["status"],
                    )
                )
            return page(records, q)
        if p == ("runs",):
            return run_routes.list_runs(self, db, q)
        if len(p) >= 2 and p[0] == "runs":
            return run_routes.read(self, db, p[1], p[2:], q)
        if len(p) >= 2 and p[0] == "evaluations":
            return self.assessment(db, p, q)
        raise Problem(404, "NOT_FOUND", "资源不存在")

    def detail(self, db, run):
        return run_routes.detail(self, db, run)

    def write(self, db, method, p, data, expected, prepared):
        if p[:1] == ("agent-jobs",):
            return self.agent_jobs.write(db, p, data, expected, prepared)
        if p[:2] == ("me", "preferences") and method == "PATCH":
            return 200, personal_preferences.save(db, self.actor(), p[2], data, expected)
        if method == "POST" and p == ("auth", "logout"):
            return 200, {"signed_out": True}
        if method == "POST" and p == ("members",):
            return 201, self.platform.add_member(db, data)
        if method == "PATCH" and len(p) == 2 and p[0] == "members":
            return 200, self.platform.update_member(db, p[1], data, expected)
        if p and p[0] in KINDS:
            if method == "POST" and len(p) == 1:
                resource = self.platform.save(db, p[0], data)
                if p[0] == "projects":
                    self.platform.grant_created_project(db, self.actor(), resource)
                return 201, resource
            if method == "PATCH" and len(p) == 2:
                self.platform.get(db, p[0], p[1])
                return 200, self.platform.save(db, p[0], data, p[1], expected)
            if (
                method == "POST"
                and len(p) == 3
                and p[0] == "templates"
                and p[2] == "publish"
            ):
                require_object(data, ())
                return 200, self.platform.publish(db, p[1], expected, self.actor())
            if method == "POST" and len(p) == 3 and p[0] == "projects" and p[2] == "memories":
                project = self.platform.get(db, "projects", p[1])
                return 201, project_memory.create(db, project, data, self.actor())
            if method == "PATCH" and len(p) == 4 and p[0] == "projects" and p[2] == "memories":
                project = self.platform.get(db, "projects", p[1])
                return 200, project_memory.update(db, project, p[3], data, expected, self.actor())
        if method == "POST" and p == ("endpoints",):
            eid = data.get("endpoint_id")
            if any(m["endpoint_id"] == eid for m in self.catalog.list()):
                raise Problem(409, "ENDPOINT_EXISTS", "端点已存在")
            return 201, self.catalog.save(data)
        if p and p[0] == "endpoints" and len(p) >= 2:
            old = self.catalog.get(p[1])
            match(old["revision"], expected)
            if method == "PATCH" and len(p) == 2:
                if "endpoint_id" in data and data["endpoint_id"] != p[1]:
                    raise ValueError("端点 ID 不可修改")
                model = dict(old, **data)
                model.pop("revision", None)
                return 200, self.catalog.save(model)
            if method == "POST" and p[2:] == ("probes",):
                if prepared is not None:
                    return 200, prepared
                scope = self.admin_run_scope(db, data)
                if p[1] not in self.platform.get(db, "workspaces", scope["workspace_id"])["endpoint_ids"]:
                    raise Problem(403, "MODEL_FORBIDDEN", "所选模型未授权给项目工作空间")
                if not old["allowed"] or not old["enabled"]:
                    raise Problem(409, "MODEL_UNAVAILABLE", "所选模型未允许调用或已停用")
                probe = self.plan(
                    {
                        **scope,
                        "task": {
                            "text": '只返回 JSON {"check":"ok"}',
                            "stage": "general",
                            "pinned_endpoint": p[1],
                            "max_output_tokens": 32,
                        },
                        "budget": data.get(
                            "budget", {"currency": old["currency"], "limit_micros": 0}
                        ),
                    }
                )
                probe["endpoint_revisions"] = {p[1]: old["revision"]}
                result = self.start(db, probe, {})
                db.execute(
                    "INSERT INTO api_probes VALUES (?,?,?,?)",
                    (result["run_id"], p[1], old["revision"], "structured_output"),
                )
                return 202, result
        if method == "POST" and p == ("runs",):
            return 201, self.start(db, prepared["plan"], prepared["context"])
        if method == "POST" and len(p) >= 3 and p[0] == "runs":
            return run_routes.write_action(self, db, p[1], p[2:], data, expected)
        if method == "POST" and p == ("evaluations",):
            return self.begin_evaluation(db, data)
        if (
            method == "POST"
            and len(p) == 3
            and p[0] == "evaluations"
            and p[2] == "grades"
        ):
            return self.grade(db, p[1], expected, data)
        raise Problem(404, "NOT_FOUND", "资源不存在")

    def _after_attempt_finish(self, attempt_id):
        with self.store.transaction() as db:
            row = db.execute("SELECT run_id FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row:
            project_tools.auto_approve(self, row["run_id"])

    def work(self, stop):
        while not stop.is_set():
            try:
                if (
                    self.delivery.work_once()
                    or self.runs.work_once()
                    or self.agent_jobs.work_once(stop)
                    or self.agent_jobs.work_sop_agent_once(stop)
                ):
                    continue
            except (sqlite3.OperationalError, RuntimeError, ValueError, OSError) as exc:
                print(f"工作线程暂缓 {type(exc).__name__}", file=sys.stderr)
            stop.wait(0.25)

    def admin_run_scope(self, db, data):
        return run_planning.admin_run_scope(self, db, data)

    def begin_evaluation(self, db, data):
        return evaluation_routes.begin_evaluation(self, db, data)

    def observations(self, db, experiment):
        return evaluation_routes.observations(self, db, experiment)

    def assessment(self, db, parts, query):
        return evaluation_routes.assessment(self, db, parts, query)

    def grade(self, db, eid, expected, data):
        return evaluation_routes.grade(self, db, eid, expected, data)
