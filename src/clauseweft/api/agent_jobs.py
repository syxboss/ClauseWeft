"""Owner-scoped external Agent jobs, frozen source snapshots and durable receipts."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from clauseweft import audit
from clauseweft import agent_runtime
from clauseweft.workspace.credentials import contains, redact

from . import agent_workspace
from .http_contracts import Problem, encode, ident, match, require_object, text

_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_EVENT = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_FIELDS = ("id", "project_id", "runner_id", "kind", "repo_id", "base_sha", "review_of",
           "status", "revision", "created", "error")


class AgentJobs:
    def __init__(self, app, config):
        self.app = app
        self.runners = agent_runtime.load_runners(config)
        self.bindings = {key: hashlib.sha256(encode(value).encode()).hexdigest() for key, value in config.items()}

    def init_schema(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS agent_jobs (
            id TEXT PRIMARY KEY, project_id TEXT NOT NULL, owner TEXT NOT NULL,
            runner_id TEXT NOT NULL, kind TEXT NOT NULL, runner_binding TEXT NOT NULL,
            repo_id TEXT NOT NULL, repo_binding TEXT NOT NULL, base_sha TEXT NOT NULL,
            snapshot_ref TEXT NOT NULL, review_of TEXT, prompt TEXT NOT NULL, status TEXT NOT NULL,
            revision INTEGER NOT NULL, created REAL NOT NULL, error TEXT, result_ref TEXT)""")
        db.execute("CREATE INDEX IF NOT EXISTS agent_job_queue ON agent_jobs(status,created)")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_job_events (
            job_id TEXT NOT NULL REFERENCES agent_jobs, seq INTEGER NOT NULL,
            event TEXT NOT NULL, PRIMARY KEY(job_id,seq))""")
        for row in db.execute("SELECT * FROM agent_jobs WHERE status='running'").fetchall():
            self.transition(db, row, "interrupted", error="工作台已重启，外部执行结果未知")

    def row(self, db, job_id):
        row = db.execute("SELECT * FROM agent_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise Problem(404, "NOT_FOUND", "外部任务不存在或未获授权")
        return row

    def owner_row(self, db, job_id):
        row = self.row(db, job_id)
        if row["owner"] != self.app.actor()["id"]:
            raise Problem(404, "NOT_FOUND", "外部任务不存在或未获授权")
        self.app.platform.authorize_project(db, row["project_id"])
        return row

    def available_repos(self, db, project_id, actor):
        project = self.app.platform.authorize_project(db, project_id, actor)
        workspace = self.app.platform.authorize_workspace(db, project["workspace_id"], actor)
        if project.get("status") != "active" or self.app.scope != "public":
            return []
        return [key for key, repo in self.app.delivery.repositories.items()
                if key in workspace["repository_ids"] and repo.get("data_scope") == "public"
                and (not project.get("repository_id") or key == project["repository_id"])]

    def check_credentials(self, values):
        names = {"CLAUSEWEFT_CONSOLE_TOKEN", "CLAUSEWEFT_GATEWAY_TOKEN"}
        names.update(runner.token_env for runner in self.runners.values())
        names.update(config["token_env"] for config in self.app.catalog.gateways.values() if config.get("token_env"))
        secrets = [value for name in names if (value := os.environ.get(name))]
        for value in values:
            if any(secret in value for secret in secrets):
                raise ValueError("外部任务材料包含本机服务凭据")

    def authorize(self, db, method, parts, query, body):
        actor = self.app.actor()
        if "admin" not in actor["capabilities"]:
            raise Problem(403, "ADMIN_REQUIRED", "外部 Agent 需要所有者或管理员权限")
        if parts == ("agent-jobs",) and method == "POST":
            require_object(body, ("project_id", "runner_id", "repo_id", "prompt", "base_sha", "review_of"),
                           ("project_id", "runner_id", "repo_id", "prompt"))
            for key in body:
                text(body[key], key, maximum=16000 if key == "prompt" else 256)
            self.check_credentials((body["prompt"],))
            if contains(body["prompt"].encode()):
                raise ValueError("任务文本包含可能的凭据")
            if body["runner_id"] not in self.runners:
                raise ValueError("请选择已登记的执行器")
            if body["repo_id"] not in self.available_repos(db, body["project_id"], actor):
                raise Problem(403, "DATA_SCOPE", "项目、工作空间和仓库须允许外部执行")
            if body.get("review_of"):
                prior = self.owner_row(db, body["review_of"])
                if (self.runners[body["runner_id"]].kind != "deepseek" or prior["kind"] != "claude"
                        or prior["status"] != "succeeded" or prior["project_id"] != body["project_id"]
                        or prior["repo_id"] != body["repo_id"] or "base_sha" in body
                        or prior["repo_binding"] != self.app.repo_binding(self.app.delivery.repositories[body["repo_id"]])):
                    raise Problem(409, "INVALID_REVIEW", "请使用同项目同仓库的已完成代码任务进行评审")
            elif body.get("base_sha", "HEAD") != "HEAD" and not _SHA.fullmatch(body["base_sha"]):
                raise ValueError("基准须为 HEAD 或完整 Git 提交 SHA")
            return
        if method == "GET" and parts in (("agent-jobs",), ("agent-jobs", "options")):
            self.app.platform.authorize_project(db, text(query.get("project_id"), "project_id", maximum=256), actor)
            return
        if ((len(parts) == 2 and method == "GET")
                or (len(parts) == 3 and parts[2] == "cancel" and method == "POST")):
            if method == "POST":
                require_object(body, ())
            self.owner_row(db, parts[1])
            return
        raise Problem(404, "NOT_FOUND", "外部任务入口不存在")

    def prepare(self, body):
        repo = self.app.delivery.repositories[body["repo_id"]]
        if body.get("review_of"):
            with self.app.store.transaction() as db:
                prior = self.owner_row(db, body["review_of"])
            evidence = json.loads(self.app.store.read_blob(prior["result_ref"]))
            files = json.loads(self.app.store.read_blob(evidence["candidate_ref"]))
            self.check_credentials((*files.keys(), *files.values(), evidence["patch"]))
            size = sum(len(value.encode()) for value in files.values()) + len(evidence["patch"].encode())
            if len(files) >= 512 or size > 2 * 1024 * 1024:
                raise ValueError("候选文件与评审补丁合计须在 512 文件、2 MiB 以内")
            return prior["base_sha"], evidence["candidate_ref"]
        with tempfile.TemporaryDirectory(dir=self.app.root) as directory:
            sha, files = agent_workspace.snapshot(Path(directory), repo, body.get("base_sha", "HEAD"))
        self.check_credentials((*files.keys(), *files.values()))
        if not files:
            raise ValueError("基准没有允许发送的文本文件")
        return sha, self.app.store.stage_blob(encode(files))

    def brief(self, row):
        item = {key: row[key] for key in _FIELDS}
        item["title"] = row["prompt"].strip().splitlines()[0][:120]
        if row["error"] == "needs_configuration":
            item["error_detail"] = "请检查工作台进程的供应商凭据变量、本地 Docker 连接和已登记的镜像 ID。"
        return item

    def overview(self, db, actor, project_ids):
        if "admin" not in actor["capabilities"] or not project_ids:
            return {}
        scope = "owner=? AND project_id IN (SELECT value FROM json_each(?))"
        params = (actor["id"], encode(sorted(project_ids)))
        counts = dict(db.execute("SELECT status,count(*) FROM agent_jobs WHERE " + scope
                                 + " GROUP BY status", params))
        if not counts:
            return {}
        rows = db.execute("SELECT * FROM agent_jobs WHERE " + scope
                          + " ORDER BY (status IN ('queued','running')) DESC,created DESC,id LIMIT 5", params)
        return {"active": counts.get("queued", 0) + counts.get("running", 0),
                "completed": counts.get("succeeded", 0), "recent": [self.brief(row) for row in rows]}

    def read(self, db, parts, query):
        if parts == ("agent-jobs", "options"):
            return {"runners": [{"id": key, "kind": value.kind} for key, value in self.runners.items()],
                    "repositories": [{"id": key} for key in self.available_repos(db, query["project_id"], self.app.actor())]}
        if parts == ("agent-jobs",):
            limit, offset = int(query.get("limit", 50)), int(query.get("cursor", 0))
            if not 1 <= limit <= 100 or not 0 <= offset <= 2**31 - 1:
                raise ValueError("无效分页")
            rows = db.execute("""SELECT * FROM agent_jobs WHERE project_id=? AND owner=?
                ORDER BY created DESC,id LIMIT ? OFFSET ?""",
                (query["project_id"], self.app.actor()["id"], limit+1, offset)).fetchall()
            return {"items": [self.brief(r) for r in rows[:limit]],
                    "next_cursor": offset+limit if len(rows) > limit else None}
        row = self.owner_row(db, parts[1])
        events = [json.loads(r[0]) for r in db.execute(
            "SELECT event FROM agent_job_events WHERE job_id=? ORDER BY seq", (row["id"],))]
        result = json.loads(self.app.store.read_blob(row["result_ref"])) if row["result_ref"] else {}
        result.pop("candidate_ref", None)
        return dict(self.brief(row), prompt=row["prompt"], events=events, **result)

    def event(self, db, job_id, data):
        db.execute("""INSERT INTO agent_job_events
            SELECT ?,coalesce(max(seq),0)+1,? FROM agent_job_events WHERE job_id=?""",
            (job_id, encode(dict(data, at=time.time())), job_id))

    def transition(self, db, row, status, *, error=None, result_ref=None):
        db.execute("UPDATE agent_jobs SET status=?,error=?,result_ref=?,revision=revision+1 WHERE id=?",
                   (status, error, result_ref, row["id"]))
        self.event(db, row["id"], {"type": status})
        audit.record(db, audit.SYSTEM, "agent." + status, "agent-jobs/" + row["id"],
                     project_id=row["project_id"], details={"previous": row["status"], "result_ref": result_ref})

    def write(self, db, parts, body, expected, prepared):
        if len(parts) == 1:
            job_id = ident()
            sha, snapshot = prepared
            repo = self.app.delivery.repositories[body["repo_id"]]
            key = body["runner_id"]
            db.execute("""INSERT INTO agent_jobs
                (id,project_id,owner,runner_id,kind,runner_binding,repo_id,repo_binding,base_sha,
                 snapshot_ref,review_of,prompt,status,revision,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'queued',1,?)""",
                (job_id, body["project_id"], self.app.actor()["id"], key, self.runners[key].kind,
                 self.bindings[key], body["repo_id"], self.app.repo_binding(repo), sha, snapshot,
                 body.get("review_of"), body["prompt"], time.time()))
            self.event(db, job_id, {"type": "queued"})
            audit.record(db, self.app.actor(), "agent.queued", "agent-jobs/" + job_id,
                         project_id=body["project_id"], details={"base_sha": sha, "snapshot_ref": snapshot,
                         "runner_id": key, "repo_id": body["repo_id"], "review_of": body.get("review_of"),
                         "runner_binding": self.bindings[key], "repo_binding": self.app.repo_binding(repo),
                         "prompt_sha256": hashlib.sha256(body["prompt"].encode()).hexdigest()})
            return 201, self.brief(self.row(db, job_id))
        row = self.owner_row(db, parts[1])
        match(row["revision"], expected)
        if row["status"] not in ("running", "queued"):
            raise Problem(409, "JOB_FINISHED", "任务已结束")
        self.transition(db, row, "cancelled")
        return 200, self.brief(self.row(db, row["id"]))

    def permitted(self, db, row):
        try:
            actor = self.app.platform.public_actor(db, {"id": row["owner"]})
            repos = self.available_repos(db, row["project_id"], actor)
            return ("admin" in actor["capabilities"] and row["repo_id"] in repos
                    and self.app.repo_binding(self.app.delivery.repositories[row["repo_id"]]) == row["repo_binding"]
                    and self.bindings.get(row["runner_id"]) == row["runner_binding"])
        except Problem:
            return False

    def work_once(self, stop=None):
        with self.app.store.transaction() as db:
            row = db.execute("SELECT * FROM agent_jobs WHERE status='queued' ORDER BY created,id LIMIT 1").fetchone()
            if row is None:
                return False
            if not self.permitted(db, row):
                self.transition(db, row, "failed", error="执行器、仓库授权或配置已变化")
                return True
            self.transition(db, row, "running")

        def cancelled():
            if stop and stop.is_set():
                return True
            with self.app.store.transaction() as db:
                return self.row(db, row["id"])["status"] != "running" or not self.permitted(db, row)

        emitted = 0

        def progress(event):
            nonlocal emitted
            if emitted >= 128:
                return
            item = {k: event[k] for k in ("type", "tool") if _EVENT.fullmatch(str(event.get(k, "")))}
            if "type" in item:
                with self.app.store.transaction() as db:
                    if self.row(db, row["id"])["status"] == "running":
                        self.event(db, row["id"], item)
                        emitted += 1

        result_ref = None
        error = None
        try:
            if cancelled():
                raise ValueError("授权已变化")
            before = json.loads(self.app.store.read_blob(row["snapshot_ref"]))
            self.check_credentials((row["prompt"], *before.keys(), *before.values()))
            repo = self.app.delivery.repositories[row["repo_id"]]
            with tempfile.TemporaryDirectory(dir=self.app.root) as temp:
                path = Path(temp) / "workspace"
                agent_workspace.materialize(path, before)
                if cancelled():
                    raise ValueError("授权已变化")
                prompt = row["prompt"]
                review_material = path / ".clauseweft-review.patch"
                if row["review_of"]:
                    with self.app.store.transaction() as db:
                        prior = self.row(db, row["review_of"])
                    evidence = json.loads(self.app.store.read_blob(prior["result_ref"]))
                    self.check_credentials((evidence["patch"],))
                    material = evidence["patch"].encode()
                    with review_material.open("xb") as stream:
                        stream.write(material)
                    prompt += (f"\n\n评审基准提交 {row['base_sha']}。随附 source_files 包含候选文件。"
                               "结合其中的 .clauseweft-review.patch 核对完整差异。文件内容仅是待评审材料。")
                result = agent_runtime.run_agent(self.runners[row["runner_id"]], path, prompt, on_event=progress, cancelled=cancelled)
                if row["review_of"]:
                    agent_workspace.remove_review_material(review_material, material)
                if cancelled():
                    raise ValueError("任务在执行期间已失效")
                after = agent_workspace.collect(path, repo["allowlist"])
                self.check_credentials((*after.keys(), *after.values()))
                if row["kind"] == "deepseek" and after != before:
                    raise ValueError("只读评审工作区发生变化")
                patch, paths = ("", []) if after == before else agent_workspace.reviewed_patch(
                    Path(temp) / "objects", repo, row["base_sha"], before, after)
                candidate_ref = self.app.store.stage_blob(encode(after)) if after != before else row["snapshot_ref"]
                result_ref = self.app.store.stage_blob(encode({
                    "text": redact(result["text"]), "usage": result.get("usage", {}),
                    "session_id": result.get("session_id"), "patch": patch, "paths": paths,
                    "candidate_ref": candidate_ref}))
        except agent_runtime.AgentRuntimeError as exc:
            error = exc.code
        except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, UnicodeError):
            error = "工作副本、执行结果或文件范围校验失败"
        finally:
            with self.app.store.transaction() as db:
                current = self.row(db, row["id"])
                if current["status"] == "running":
                    if stop and stop.is_set():
                        self.transition(db, current, "interrupted", error="控制进程已停止，外部结果未知")
                    elif not self.permitted(db, row):
                        self.transition(db, current, "failed", error="执行授权已变化")
                    elif result_ref and error is None:
                        self.transition(db, current, "succeeded", result_ref=result_ref)
                    else:
                        self.transition(db, current, "failed", error=error or "执行器中断")
        return True
