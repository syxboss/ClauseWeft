"""Owner-scoped external Agent jobs, frozen source snapshots, tests and durable receipts."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import tempfile
import time

from clauseweft import agent_runtime, audit, testing
from clauseweft.runs import state as run_state
from clauseweft.workspace.credentials import contains, redact

from . import agent_workspace
from .delivery_jobs import format_test_feedback
from .http_contracts import Problem, encode, ident, match, require_object, text

_SHA = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})")
_EVENT = re.compile(r"[A-Za-z0-9_.:-]{1,64}")
_FIELDS = ("id", "project_id", "runner_id", "kind", "repo_id", "base_sha", "review_of",
           "continue_from", "suite_id", "max_fix_rounds", "status", "revision", "created", "error")


def _repo_suites(repo, configured_suites):
    suites = repo.get("suite_ids")
    if isinstance(suites, list):
        return [s for s in suites if s in configured_suites]
    default = repo.get("suite_id")
    return [default] if default and default in configured_suites else []


def _merge_usage(total, latest):
    if not isinstance(latest, dict):
        return total
    merged = dict(total)
    for key, value in latest.items():
        if type(value) is int and value >= 0:
            merged[key] = merged.get(key, 0) + value
    return merged


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
            snapshot_ref TEXT NOT NULL, base_snapshot_ref TEXT, review_of TEXT,
            continue_from TEXT, suite_id TEXT, max_fix_rounds INTEGER NOT NULL DEFAULT 0,
            prompt TEXT NOT NULL, status TEXT NOT NULL,
            revision INTEGER NOT NULL, created REAL NOT NULL, error TEXT, result_ref TEXT)""")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(agent_jobs)")}
        for col, ddl in (
            ("base_snapshot_ref", "TEXT"),
            ("continue_from", "TEXT"),
            ("suite_id", "TEXT"),
            ("max_fix_rounds", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if col not in columns:
                db.execute(f"ALTER TABLE agent_jobs ADD COLUMN {col} {ddl}")
        db.execute("CREATE INDEX IF NOT EXISTS agent_job_queue ON agent_jobs(status,created)")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_job_events (
            job_id TEXT NOT NULL REFERENCES agent_jobs, seq INTEGER NOT NULL,
            event TEXT NOT NULL, PRIMARY KEY(job_id,seq))""")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_job_tests (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES agent_jobs,
            suite_id TEXT NOT NULL, status TEXT NOT NULL, verified INTEGER NOT NULL DEFAULT 0,
            created REAL NOT NULL, report_ref TEXT NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS agent_job_tests_by_job ON agent_job_tests(job_id,created)")
        db.execute("""CREATE TABLE IF NOT EXISTS agent_job_comments (
            id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES agent_jobs,
            candidate_ref TEXT NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL,
            side TEXT NOT NULL, body TEXT NOT NULL, actor TEXT NOT NULL, created REAL NOT NULL)""")
        db.execute("CREATE INDEX IF NOT EXISTS agent_job_comments_by_job ON agent_job_comments(job_id,created)")
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
            require_object(
                body,
                ("project_id", "runner_id", "repo_id", "prompt", "base_sha", "review_of",
                 "continue_from", "comment_ids", "include_comments", "suite_id", "max_fix_rounds"),
                ("project_id", "runner_id", "repo_id", "prompt"),
            )
            for key in body:
                if key in ("max_fix_rounds", "comment_ids", "include_comments"):
                    continue
                text(body[key], key, maximum=16000 if key == "prompt" else 256)
            if "max_fix_rounds" in body:
                rounds = body["max_fix_rounds"]
                if type(rounds) is not int or not 0 <= rounds <= 3:
                    raise ValueError("自动修复轮次上限须为 0 至 3 的整数")
            if "include_comments" in body and type(body["include_comments"]) is not bool:
                raise ValueError("include_comments 须为布尔值")
            if "comment_ids" in body:
                cids = body["comment_ids"]
                if (not isinstance(cids, list) or not 1 <= len(cids) <= 32
                        or any(not isinstance(cid, str) or not cid.strip() for cid in cids)
                        or len(set(cids)) != len(cids)):
                    raise ValueError("comment_ids 须为 1 至 32 项不重复的评论 ID")
            if (body.get("comment_ids") or body.get("include_comments")) and not body.get("continue_from"):
                raise ValueError("comment_ids 与 include_comments 仅用于 continue_from 续接修改")
            self.check_credentials((body["prompt"],))
            if contains(body["prompt"].encode()):
                raise ValueError("任务文本包含可能的凭据")
            if body["runner_id"] not in self.runners:
                raise ValueError("请选择已登记的执行器")
            runner = self.runners[body["runner_id"]]
            if body["repo_id"] not in self.available_repos(db, body["project_id"], actor):
                raise Problem(403, "DATA_SCOPE", "项目、工作空间和仓库须允许外部执行")
            repo = self.app.delivery.repositories[body["repo_id"]]
            if body.get("review_of") and body.get("continue_from"):
                raise ValueError("review_of 与 continue_from 不能同时指定")
            if "suite_id" in body:
                if not agent_runtime.is_editable_kind(runner.kind):
                    raise ValueError("仅代码修改执行器支持绑定验收测试套件")
                self.app.delivery.check_suite(repo, body["suite_id"])
            if body.get("max_fix_rounds", 0) > 0 and not body.get("suite_id"):
                raise ValueError("开启自动修复循环须同时指定 suite_id")
            if body.get("review_of"):
                prior = self.owner_row(db, body["review_of"])
                if (runner.kind != "deepseek" or not agent_runtime.is_editable_kind(prior["kind"])
                        or prior["status"] != "succeeded" or prior["project_id"] != body["project_id"]
                        or prior["repo_id"] != body["repo_id"] or "base_sha" in body
                        or prior["repo_binding"] != self.app.repo_binding(repo)):
                    raise Problem(409, "INVALID_REVIEW", "请使用同项目同仓库的已完成代码任务进行评审")
            elif body.get("continue_from"):
                prior = self.owner_row(db, body["continue_from"])
                if (not agent_runtime.is_editable_kind(runner.kind)
                        or not agent_runtime.is_editable_kind(prior["kind"])
                        or prior["status"] != "succeeded" or prior["project_id"] != body["project_id"]
                        or prior["repo_id"] != body["repo_id"] or "base_sha" in body
                        or not prior["result_ref"]
                        or prior["repo_binding"] != self.app.repo_binding(repo)):
                    raise Problem(409, "INVALID_CONTINUATION", "请使用同项目同仓库的已完成代码任务继续修改")
                if body.get("comment_ids"):
                    existing = {r["id"] for r in self._comments(db, prior["id"])}
                    if any(cid not in existing for cid in body["comment_ids"]):
                        raise ValueError("所选评论不属于来源任务")
            elif body.get("base_sha", "HEAD") != "HEAD" and not _SHA.fullmatch(body["base_sha"]):
                raise ValueError("基准须为 HEAD 或完整 Git 提交 SHA")
            return
        if method == "GET" and parts in (("agent-jobs",), ("agent-jobs", "options")):
            self.app.platform.authorize_project(db, text(query.get("project_id"), "project_id", maximum=256), actor)
            return
        if len(parts) == 2 and method == "GET":
            self.owner_row(db, parts[1])
            return
        if len(parts) == 3 and parts[2] == "cancel" and method == "POST":
            require_object(body, ())
            self.owner_row(db, parts[1])
            return
        if len(parts) == 3 and parts[2] == "comments" and method == "POST":
            require_object(body, ("path", "line", "body", "side"), ("path", "line", "body"))
            path_val = text(body["path"], "path", maximum=512)
            body_val = text(body["body"], "body", maximum=2000)
            line_val = body["line"]
            side_val = body.get("side", "new")
            if type(line_val) is not int or not 1 <= line_val <= 1_000_000:
                raise ValueError("行号须为 1 至 1000000 之间的整数")
            if side_val not in ("new", "old"):
                raise ValueError("side 须为 new 或 old")
            self.check_credentials((body_val,))
            if contains(body_val.encode()):
                raise ValueError("评论内容包含可能的凭据")
            row = self.owner_row(db, parts[1])
            if not agent_runtime.is_editable_kind(row["kind"]) or row["status"] != "succeeded" or not row["result_ref"]:
                raise Problem(409, "INVALID_COMMENT_TARGET", "仅已完成的代码任务可添加行级评论")
            evidence = json.loads(self.app.store.read_blob(row["result_ref"]))
            if path_val not in evidence.get("paths", []):
                raise ValueError("仅可对候选补丁涉及的文件添加行级评论")
            return
        if len(parts) == 3 and parts[2] == "tests" and method == "POST":
            require_object(body, ("suite_id",))
            if "suite_id" in body:
                text(body["suite_id"], "suite_id", maximum=256)
            row = self.owner_row(db, parts[1])
            if not agent_runtime.is_editable_kind(row["kind"]) or row["status"] != "succeeded" or not row["result_ref"]:
                raise Problem(409, "INVALID_TEST_TARGET", "仅已完成的代码任务可运行验收测试")
            repo = self.app.delivery.repositories.get(row["repo_id"])
            if not repo or self.app.repo_binding(repo) != row["repo_binding"]:
                raise Problem(409, "REPO_CHANGED", "仓库配置已变更")
            suite_id = body.get("suite_id") or repo.get("suite_id")
            self.app.delivery.check_suite(repo, suite_id)
            return
        raise Problem(404, "NOT_FOUND", "外部任务入口不存在")

    def prepare(self, body):
        repo = self.app.delivery.repositories[body["repo_id"]]
        source_id = body.get("review_of") or body.get("continue_from")
        if source_id:
            with self.app.store.transaction() as db:
                prior = self.owner_row(db, source_id)
            evidence = json.loads(self.app.store.read_blob(prior["result_ref"]))
            files = json.loads(self.app.store.read_blob(evidence["candidate_ref"]))
            self.check_credentials((*files.keys(), *files.values(), evidence["patch"]))
            size = sum(len(value.encode()) for value in files.values()) + len(evidence["patch"].encode())
            if len(files) >= 512 or size > 2 * 1024 * 1024:
                raise ValueError("候选文件与评审补丁合计须在 512 文件、2 MiB 以内")
            base_snapshot = prior["base_snapshot_ref"] or prior["snapshot_ref"]
            return prior["base_sha"], evidence["candidate_ref"], base_snapshot
        with tempfile.TemporaryDirectory(dir=self.app.root) as directory:
            sha, files = agent_workspace.snapshot(Path(directory), repo, body.get("base_sha", "HEAD"))
        self.check_credentials((*files.keys(), *files.values()))
        if not files:
            raise ValueError("基准没有允许发送的文本文件")
        snap = self.app.store.stage_blob(encode(files))
        return sha, snap, snap

    def _run_candidate_suite(self, repo_id, suite_id, files, candidate_ref):
        repo = self.app.delivery.repositories[repo_id]
        self.app.delivery.check_suite(repo, suite_id)
        frozen = testing.freeze_suite(suite_id, self.app.delivery.suites[suite_id])
        candidate = {"candidate_sha": candidate_ref, "tree_hash": candidate_ref}
        file_bytes = {path: content.encode("utf-8") for path, content in files.items()}
        report = testing.start(candidate, frozen, file_bytes)
        verified = testing.verify(report, candidate, frozen)
        report_ref = self.app.store.stage_blob(encode(report))
        return suite_id, report, verified, report_ref

    def prepare_test(self, job_id, body):
        with self.app.store.transaction() as db:
            row = self.owner_row(db, job_id)
            repo = self.app.delivery.repositories[row["repo_id"]]
            suite_id = body.get("suite_id") or repo.get("suite_id")
            self.app.delivery.check_suite(repo, suite_id)
            evidence = json.loads(self.app.store.read_blob(row["result_ref"]))
            candidate_ref = evidence["candidate_ref"]
            files = json.loads(self.app.store.read_blob(candidate_ref))
        return self._run_candidate_suite(row["repo_id"], suite_id, files, candidate_ref)

    def _record_test(self, db, row, suite_id, report, verified, report_ref, actor):
        test_id = ident()
        created = time.time()
        status = "passed" if verified else report.get("status", "failed")
        db.execute(
            """INSERT INTO agent_job_tests(id,job_id,suite_id,status,verified,created,report_ref)
               VALUES(?,?,?,?,?,?,?)""",
            (test_id, row["id"], suite_id, status, 1 if verified else 0, created, report_ref),
        )
        self.event(db, row["id"], {"type": "test_passed" if verified else "test_failed", "tool": suite_id})
        audit.record(
            db, actor, "agent.tested", f"agent-jobs/{row['id']}/tests/{test_id}",
            outcome="succeeded" if verified else "failed",
            project_id=row["project_id"],
            details={"job_id": row["id"], "suite_id": suite_id, "status": status,
                     "verified": verified, "report_ref": report_ref},
        )
        return {
            "id": test_id, "job_id": row["id"], "suite_id": suite_id,
            "status": status, "verified": verified, "created": created, "report": report,
        }

    def _comments(self, db, job_id):
        rows = db.execute(
            "SELECT * FROM agent_job_comments WHERE job_id=? ORDER BY created ASC, id ASC",
            (job_id,),
        ).fetchall()
        return [
            {
                "id": r["id"], "job_id": r["job_id"], "candidate_ref": r["candidate_ref"],
                "path": r["path"], "line": r["line"], "side": r["side"],
                "body": r["body"], "actor": r["actor"], "created": r["created"],
            }
            for r in rows
        ]

    def _test_reports(self, db, job_id):
        rows = db.execute(
            "SELECT * FROM agent_job_tests WHERE job_id=? ORDER BY created DESC, id DESC",
            (job_id,),
        ).fetchall()
        return [
            {
                "id": r["id"], "job_id": r["job_id"], "suite_id": r["suite_id"],
                "status": r["status"], "verified": bool(r["verified"]), "created": r["created"],
                "report": json.loads(self.app.store.read_blob(r["report_ref"])),
            }
            for r in rows
        ]

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
            repos = []
            for key in self.available_repos(db, query["project_id"], self.app.actor()):
                repo = self.app.delivery.repositories[key]
                entry = {"id": key}
                suites = _repo_suites(repo, self.app.delivery.suites)
                if suites:
                    entry["suites"] = suites
                repos.append(entry)
            capabilities = {
                runner.kind: agent_runtime.capabilities_for(runner.kind)
                for runner in self.runners.values()
            }
            return {
                "runners": [{"id": key, "kind": value.kind} for key, value in self.runners.items()],
                "repositories": repos,
                "capabilities": capabilities,
            }
        if parts == ("agent-jobs",):
            limit, offset = int(query.get("limit", 50)), int(query.get("cursor", 0))
            if not 1 <= limit <= 100 or not 0 <= offset <= 2**31 - 1:
                raise ValueError("无效分页")
            rows = db.execute("""SELECT * FROM agent_jobs WHERE project_id=? AND owner=?
                ORDER BY created DESC,id LIMIT ? OFFSET ?""",
                (query["project_id"], self.app.actor()["id"], limit + 1, offset)).fetchall()
            return {"items": [self.brief(r) for r in rows[:limit]],
                    "next_cursor": offset + limit if len(rows) > limit else None}
        row = self.owner_row(db, parts[1])
        events = [json.loads(r[0]) for r in db.execute(
            "SELECT event FROM agent_job_events WHERE job_id=? ORDER BY seq", (row["id"],))]
        result = json.loads(self.app.store.read_blob(row["result_ref"])) if row["result_ref"] else {}
        result.pop("candidate_ref", None)
        return dict(self.brief(row), prompt=row["prompt"], events=events,
                    test_reports=self._test_reports(db, row["id"]),
                    comments=self._comments(db, row["id"]), **result)

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
            sha, snapshot, base_snapshot = prepared
            repo = self.app.delivery.repositories[body["repo_id"]]
            key = body["runner_id"]
            prompt_text = body["prompt"]
            selected_comment_ids = []
            if body.get("continue_from") and (body.get("comment_ids") or body.get("include_comments")):
                all_comments = self._comments(db, body["continue_from"])
                if body.get("comment_ids"):
                    by_id = {c["id"]: c for c in all_comments}
                    chosen = [by_id[cid] for cid in body["comment_ids"]]
                else:
                    chosen = all_comments
                if chosen:
                    selected_comment_ids = [c["id"] for c in chosen]
                    lines = [
                        f"- {c['path']}:{c['line']} ({c['side']}): {c['body']}"
                        for c in chosen
                    ]
                    prompt_text = prompt_text + "\n\n待处理的行级评审意见：\n" + "\n".join(lines)
            db.execute("""INSERT INTO agent_jobs
                (id,project_id,owner,runner_id,kind,runner_binding,repo_id,repo_binding,base_sha,
                 snapshot_ref,base_snapshot_ref,review_of,continue_from,suite_id,max_fix_rounds,
                 prompt,status,revision,created)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'queued',1,?)""",
                (job_id, body["project_id"], self.app.actor()["id"], key, self.runners[key].kind,
                 self.bindings[key], body["repo_id"], self.app.repo_binding(repo), sha, snapshot,
                 base_snapshot, body.get("review_of"), body.get("continue_from"), body.get("suite_id"),
                 body.get("max_fix_rounds", 0), prompt_text, time.time()))
            self.event(db, job_id, {"type": "queued"})
            audit.record(db, self.app.actor(), "agent.queued", "agent-jobs/" + job_id,
                         project_id=body["project_id"], details={"base_sha": sha, "snapshot_ref": snapshot,
                         "runner_id": key, "repo_id": body["repo_id"], "review_of": body.get("review_of"),
                         "continue_from": body.get("continue_from"), "source_comment_ids": selected_comment_ids,
                         "suite_id": body.get("suite_id"), "max_fix_rounds": body.get("max_fix_rounds", 0),
                         "runner_binding": self.bindings[key], "repo_binding": self.app.repo_binding(repo),
                         "prompt_sha256": hashlib.sha256(prompt_text.encode()).hexdigest()})
            return 201, self.brief(self.row(db, job_id))
        if len(parts) == 3 and parts[2] == "comments":
            row = self.owner_row(db, parts[1])
            evidence = json.loads(self.app.store.read_blob(row["result_ref"]))
            cid = ident()
            created = time.time()
            side = body.get("side", "new")
            db.execute(
                """INSERT INTO agent_job_comments(id,job_id,candidate_ref,path,line,side,body,actor,created)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (cid, row["id"], evidence["candidate_ref"], body["path"].strip(), body["line"],
                 side, body["body"].strip(), self.app.actor()["id"], created),
            )
            audit.record(
                db, self.app.actor(), "agent.commented", f"agent-jobs/{row['id']}/comments/{cid}",
                project_id=row["project_id"],
                details={"job_id": row["id"], "candidate_ref": evidence["candidate_ref"],
                         "path": body["path"].strip(), "line": body["line"], "side": side},
            )
            return 201, {
                "id": cid, "job_id": row["id"], "candidate_ref": evidence["candidate_ref"],
                "path": body["path"].strip(), "line": body["line"], "side": side,
                "body": body["body"].strip(), "actor": self.app.actor()["id"], "created": created,
            }
        if len(parts) == 3 and parts[2] == "tests":
            row = self.owner_row(db, parts[1])
            suite_id, report, verified, report_ref = prepared
            record = self._record_test(db, row, suite_id, report, verified, report_ref, self.app.actor())
            return 201, record
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
            base_files = json.loads(self.app.store.read_blob(row["base_snapshot_ref"] or row["snapshot_ref"]))
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
                        prior_tests = self._test_reports(db, prior["id"])
                    evidence = json.loads(self.app.store.read_blob(prior["result_ref"]))
                    self.check_credentials((evidence["patch"],))
                    material = evidence["patch"].encode()
                    with review_material.open("xb") as stream:
                        stream.write(material)
                    prompt += (f"\n\n评审基准提交 {row['base_sha']}。随附 source_files 包含候选文件。"
                               "结合其中的 .clauseweft-review.patch 核对完整差异。文件内容仅是待评审材料。")
                    if prior_tests:
                        latest = prior_tests[0]
                        counts = latest["report"].get("counts", {})
                        prompt += (
                            f"\n\n最近一次隔离容器验收测试（套件 {latest['suite_id']}）："
                            f"status={latest['status']}，verified={latest['verified']}，"
                            f"counts={json.dumps(counts, ensure_ascii=False)}。"
                        )
                usage_total = {}
                fix_rounds_used = 0
                latest_test_status = None
                test_verified = None
                current_prompt = prompt
                while True:
                    result = agent_runtime.run_agent(
                        self.runners[row["runner_id"]], path, current_prompt,
                        on_event=progress, cancelled=cancelled,
                    )
                    usage_total = _merge_usage(usage_total, result.get("usage", {}))
                    if row["review_of"]:
                        agent_workspace.remove_review_material(review_material, material)
                    if cancelled():
                        raise ValueError("任务在执行期间已失效")
                    after = agent_workspace.collect(path, repo["allowlist"])
                    self.check_credentials((*after.keys(), *after.values()))
                    if row["kind"] == "deepseek" and after != before:
                        raise ValueError("只读评审工作区发生变化")
                    candidate_ref = self.app.store.stage_blob(encode(after)) if after != before else row["snapshot_ref"]
                    if agent_runtime.is_editable_kind(row["kind"]) and row["suite_id"]:
                        progress({"type": "testing", "tool": row["suite_id"]})
                        suite_id, report, verified, report_ref = self._run_candidate_suite(
                            row["repo_id"], row["suite_id"], after, candidate_ref,
                        )
                        with self.app.store.transaction() as db:
                            if self.row(db, row["id"])["status"] == "running":
                                self._record_test(db, row, suite_id, report, verified, report_ref, audit.SYSTEM)
                        latest_test_status = "passed" if verified else report.get("status", "failed")
                        test_verified = verified
                        if (not verified and fix_rounds_used < (row["max_fix_rounds"] or 0)
                                and report.get("status") != "needs_configuration" and not cancelled()):
                            fix_rounds_used += 1
                            progress({"type": "auto_fix", "tool": f"round-{fix_rounds_used}"})
                            current_prompt = (
                                row["prompt"] + "\n\n"
                                + format_test_feedback(report, fix_rounds_used, row["max_fix_rounds"])
                            )
                            continue
                    break
                diff_base = base_files if row["continue_from"] else before
                patch, paths = ("", []) if row["kind"] == "deepseek" or after == diff_base else agent_workspace.reviewed_patch(
                    Path(temp) / "objects", repo, row["base_sha"], diff_base, after)
                evidence_payload = {
                    "text": redact(result["text"]), "usage": usage_total,
                    "session_id": result.get("session_id"), "patch": patch, "paths": paths,
                    "candidate_ref": candidate_ref,
                }
                if row["suite_id"]:
                    evidence_payload["fix_rounds_used"] = fix_rounds_used
                    evidence_payload["latest_test_status"] = latest_test_status
                    evidence_payload["test_verified"] = test_verified
                result_ref = self.app.store.stage_blob(encode(evidence_payload))
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

    def work_sop_agent_once(self, stop=None):
        """Execute one ready SOP node of kind='agent' inside an isolated container."""
        with self.app.store.transaction() as db:
            row = db.execute(
                """SELECT n.*, r.body FROM nodes n JOIN runs r USING(run_id)
                   WHERE n.state='ready' AND r.status IN ('queued','running')
                   AND json_extract(n.definition, '$.kind')='agent'
                   ORDER BY r.created, n.rowid LIMIT 1"""
            ).fetchone()
            if row is None:
                return False
            run_id, node_id, generation = row["run_id"], row["node_id"], row["generation"]
            definition = json.loads(row["definition"])
            body = json.loads(row["body"])
            runner_id = definition["runner_id"]
            repo_id = definition["repo_id"]
            base_ref = definition.get("base_sha", "HEAD")
            runner = self.runners.get(runner_id)
            repo = self.app.delivery.repositories.get(repo_id)
            creator = body.get("created_by")
            try:
                actor = self.app.platform.public_actor(db, {"id": creator} if creator else self.app.actor())
                allowed_repos = self.available_repos(db, body.get("project_id", "default"), actor)
                authorized = (
                    runner is not None and repo is not None
                    and "admin" in actor["capabilities"]
                    and repo_id in allowed_repos
                    and self.app.execution_authorized(body, actor)
                )
            except Problem:
                authorized = False
            if not authorized:
                db.execute(
                    "UPDATE nodes SET state='needs_input', reason='agent_node_unauthorized' WHERE run_id=? AND node_id=?",
                    (run_id, node_id),
                )
                run_state.refresh(db, run_id)
                return True
            nodes = run_state.load_nodes(db, run_id)
            refs = run_state.input_refs(nodes[node_id], nodes)
            upstream = {k: self.app.store.read_blob(sha).decode("utf-8", errors="replace") for k, sha in refs.items()}
            db.execute(
                "UPDATE nodes SET state='dispatched', reason=NULL WHERE run_id=? AND node_id=? AND generation=?",
                (run_id, node_id, generation),
            )
            self.app.store.event(db, run_id, "sop_agent_started", {
                "node_id": node_id, "generation": generation, "runner_id": runner_id, "repo_id": repo_id,
            })
            run_state.refresh(db, run_id)

        def cancelled():
            if stop and stop.is_set():
                return True
            with self.app.store.transaction() as db:
                r = db.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
                n = db.execute("SELECT state, generation FROM nodes WHERE run_id=? AND node_id=?", (run_id, node_id)).fetchone()
                return not r or r["status"] == "cancelled" or not n or n["state"] != "dispatched" or n["generation"] != generation

        artifact = None
        error = None
        try:
            parts = [body["task"]["text"]]
            if definition.get("prompt"):
                parts.insert(0, definition["prompt"])
            if definition.get("feedback"):
                parts.append("返工意见：" + definition["feedback"])
            for dep, content in upstream.items():
                parts.append(f"上游节点 {dep} 产物：\n{content[:16000]}")
            prompt = "\n\n".join(parts)[:60000]
            self.check_credentials((prompt,))
            with tempfile.TemporaryDirectory(dir=self.app.root) as temp:
                sha, before = agent_workspace.snapshot(Path(temp) / "snap", repo, base_ref)
                self.check_credentials((*before.keys(), *before.values()))
                work_dir = Path(temp) / "workspace"
                agent_workspace.materialize(work_dir, before)
                result = agent_runtime.run_agent(runner, work_dir, prompt, cancelled=cancelled)
                after = agent_workspace.collect(work_dir, repo["allowlist"])
                self.check_credentials((*after.keys(), *after.values()))
                editable = agent_runtime.is_editable_kind(runner.kind)
                if not editable and after != before:
                    raise ValueError("只读评审工作区发生变化")
                patch, paths = ("", []) if not editable or after == before else agent_workspace.reviewed_patch(
                    Path(temp) / "objects", repo, sha, before, after
                )
                artifact = self.app.store.stage_blob(encode({
                    "text": redact(result["text"]), "patch": patch, "paths": paths,
                    "usage": result.get("usage", {}), "session_id": result.get("session_id"),
                    "runner_id": runner_id, "repo_id": repo_id, "base_sha": sha,
                }))
        except agent_runtime.AgentRuntimeError as exc:
            error = exc.code
        except (OSError, ValueError, KeyError, RuntimeError, sqlite3.Error, UnicodeError):
            error = "sop_agent_failed"
        finally:
            with self.app.store.transaction() as db:
                n = db.execute("SELECT state, generation FROM nodes WHERE run_id=? AND node_id=?", (run_id, node_id)).fetchone()
                if n and n["state"] == "dispatched" and n["generation"] == generation:
                    new_state = "succeeded" if artifact and error is None else "needs_input"
                    db.execute(
                        "UPDATE nodes SET state=?, artifact=?, reason=? WHERE run_id=? AND node_id=? AND generation=?",
                        (new_state, artifact if new_state == "succeeded" else None, error, run_id, node_id, generation),
                    )
                    self.app.store.event(db, run_id, "sop_agent_finished", {
                        "node_id": node_id, "generation": generation, "state": new_state,
                        "artifact": artifact, "error": error, "runner_id": runner_id, "repo_id": repo_id,
                    })
                    audit.record(
                        db, audit.SYSTEM, "sop.agent_finished", f"runs/{run_id}/nodes/{node_id}",
                        outcome="succeeded" if new_state == "succeeded" else "failed",
                        run_id=run_id, project_id=body.get("project_id"),
                        details={"node_id": node_id, "runner_id": runner_id, "repo_id": repo_id,
                                 "artifact": artifact, "error": error},
                    )
                    run_state.refresh(db, run_id)
        return True
