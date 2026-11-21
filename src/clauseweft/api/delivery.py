"""Bind workflow gates to reviewed versions, immutable Git data and actual tests."""

import json

from clauseweft import audit, requirements, reviews, testing
from clauseweft.workspace import Workspace

from . import delivery_evidence, delivery_jobs
from .delivery_evidence import REVIEW_NODES, SPEC_REVIEWERS, export_record, nodes, output as output
from .http_contracts import Problem, encode, ident, require_object

PATCH_NODES = ("code", "test-design")


class Delivery:
    def __init__(self, app, repositories, suites):
        self.app = app
        self.repositories = repositories
        self.suites = suites

    def record_decision(
        self, db, run, action, *, before=None, after=None, details=None, actor=None
    ):
        return audit.record(
            db,
            actor or self.app.actor(),
            action,
            "runs/" + run["run_id"],
            before=before,
            after=after,
            run_id=run["run_id"],
            project_id=run.get("project_id"),
            details={
                "spec_version_id": run.get("spec_version_id"),
                "candidate_sha": run.get("candidate_sha"),
                **(details or {}),
            },
            correlation_id=getattr(self.app.request_context, "correlation_id", None),
        )

    def job_event(self, db, run_id, event, job_id, kind, payload, ctx, *, result=None, reason=None, state=None):
        return delivery_jobs.job_event(
            self.app, db, run_id, event, job_id, kind, payload, ctx, result=result, reason=reason, state=state
        )

    def init_schema(self, db):
        db.execute(
            """CREATE TABLE IF NOT EXISTS api_jobs (
            id TEXT PRIMARY KEY,run_id TEXT REFERENCES runs, kind TEXT,state TEXT,
            input TEXT,result TEXT,reason TEXT,started REAL)"""
        )
        db.execute(
            """CREATE TABLE IF NOT EXISTS api_imports (
            run_id TEXT REFERENCES runs,node TEXT,generation INTEGER,artifact TEXT,error TEXT,
            PRIMARY KEY(run_id,node,generation,artifact))"""
        )
        audit.migrate_legacy(db)
        # A process restart cannot establish completion of an external test.
        # Expired jobs are explicitly rerunnable and never grant acceptance.
        for job in db.execute(
            "SELECT * FROM api_jobs WHERE state='running'"
        ).fetchall():
            result = {
                "status": "interrupted",
                "reason": "工作台重新启动，外部执行结果未知",
            }
            db.execute(
                "UPDATE api_jobs SET state='interrupted',reason=?,result=? WHERE id=?",
                (result["reason"], encode(result), job["id"]),
            )
            self.job_event(
                db,
                job["run_id"],
                "delivery_job_interrupted",
                job["id"],
                job["kind"],
                json.loads(job["input"]),
                self.app.context(db, job["run_id"]),
                result=result,
                reason=result["reason"],
                state="interrupted",
            )
        # A new process can start with edited suite/image settings. Retain the historical
        # report while revoking its authority to advance the current delivery.
        completed = db.execute(
            """SELECT n.run_id FROM nodes n JOIN runs r USING(run_id)
            WHERE n.node_id='execute-tests' AND n.state='succeeded' AND r.status!='cancelled'"""
        ).fetchall()
        for item in completed:
            rid = item["run_id"]
            run = self.app.run(rid)
            report = self.gate_artifact(run, "execute-tests")
            ctx = self.app.context(db, rid)
            if not self.current_environment(ctx, requirements.current(db, rid), report):
                self.app.runs.invalidate(rid, "execute-tests")
                reviews.invalidate_delivery(db, rid)
                self.record_decision(
                    db,
                    run,
                    "delivery.evidence_invalidated",
                    before=report,
                    after={"valid": False, "reason": "configured_environment_changed"},
                    actor=audit.SYSTEM,
                )

    def check_binding(self, ctx):
        repo = self.repositories.get(ctx["repo_id"])
        if repo is None or ctx.get("repo_binding") != self.app.repo_binding(repo):
            raise RuntimeError("仓库或已授权文件范围已变化，请以当前配置启动新运行")
        return repo

    def workspace(self, ctx):
        repo = self.check_binding(ctx)
        return Workspace(
            self.app.root / "workspaces" / ctx["snapshot_id"],
            repo["path"],
            repo["allowlist"],
        )

    def check_suite(self, repo, suite_id):
        allowed = repo.get("suite_ids")
        if allowed is None:
            allowed = [repo["suite_id"]] if "suite_id" in repo else list(self.suites)
        if (
            not isinstance(suite_id, str)
            or not isinstance(allowed, list)
            or suite_id not in allowed
            or suite_id not in self.suites
        ):
            raise ValueError("请选择为此仓库登记的验收套件")

    def prepare_context(self, plan):
        if plan["mode"] != "workflow":
            if any(plan.get(k) for k in ("repo_id", "base_sha", "suite_id")):
                raise ValueError("仓库交付参数仅用于完整研发流程")
            return {}
        if plan.get("repo_id") not in self.repositories or not isinstance(
            plan.get("base_sha"), str
        ):
            raise ValueError("完整流程需要已登记的仓库及基准提交")
        repo = self.repositories[plan["repo_id"]]
        suite_id = plan.get("suite_id", repo.get("suite_id"))
        self.check_suite(repo, suite_id)
        if (
            repo.get("data_scope", "local_only") == "local_only"
            and plan["task"]["data_scope"] != "local_only"
        ):
            raise Problem(403, "DATA_SCOPE", "此仓库仅允许本地处理")
        ctx = dict(
            repo_id=plan["repo_id"],
            repo_binding=self.app.repo_binding(repo),
            suite_id=suite_id,
            snapshot_id=ident(),
        )
        snap = self.workspace(ctx).snapshot(plan["base_sha"])
        ctx.update(
            base_sha=snap["base_sha"], files=snap["files"], scopes=repo.get("paths", {})
        )
        return ctx

    def scan_results(self, db, run):
        return delivery_evidence.scan_results(self.app.store, db, run)

    def detail(self, db, run):
        return delivery_evidence.detail(self.app, db, run)

    def artifact(self, db, run, aid):
        return delivery_evidence.artifact(self.app.store, db, run, aid)

    def enqueue(self, db, run, kind, payload):
        return delivery_jobs.enqueue(self, db, run, kind, payload)

    def gate_artifact(self, run, key):
        return delivery_evidence.gate_artifact(self.app.store, run, key)

    def write(self, db, run, p, data):
        rid = run["run_id"]
        actor = self.app.actor()["id"]
        if run["mode"] != "workflow":
            raise RuntimeError("单任务没有此流程操作")
        if run["status"] == "cancelled":
            raise RuntimeError("运行已取消")
        spec = requirements.current(db, rid)
        before = {
            "spec": spec,
            "candidate": reviews.candidate(db, rid),
            "delivery": reviews.delivery(db, rid),
        }
        ctx = self.app.context(db, rid)
        self.check_binding(ctx)
        if p == ("specs",):
            require_object(
                data, ("parent", "clauses", "reason"), ("parent", "clauses", "reason")
            )
            result = requirements.revise(
                db,
                rid,
                data["parent"],
                data["clauses"],
                data["reason"],
                actor,
                SPEC_REVIEWERS,
            )
            self.app.runs.replace_input(
                rid,
                "draft",
                {"clauses": result["clauses"], "version": result["version"]},
            )
        elif len(p) == 3 and p[0] == "findings" and p[2] == "resolutions":
            require_object(
                data,
                ("action", "reason", "evidence", "related_ids"),
                ("action", "reason"),
            )
            before["finding"] = next(
                (item for item in reviews.list_findings(db, rid) if item["id"] == p[1]),
                None,
            )
            result = reviews.resolve(db, rid, p[1], actor, **data)
            self.app.store.event(db, rid, "finding_resolved", {"id": p[1]})
        elif p == ("approvals",) and data.get("kind") == "baseline":
            require_object(data, ("kind", "version", "reason"), ("version", "reason"))
            result = requirements.freeze(
                db, rid, data["version"], actor, data["reason"]
            )
            self.app.runs.gate(
                rid,
                "approve-spec",
                {
                    "spec_version_id": result["version"],
                    "clauses": result["clauses"],
                    "files": ctx["files"],
                    "write_scopes": ctx["scopes"],
                },
            )
        elif p == ("integrations",):
            return self._request_integration(db, run, data, spec, before)
        elif p == ("tests",):
            return self._request_tests(db, run, data, spec, ctx, before)
        elif p == ("approvals",) and data.get("kind") == "delivery":
            result = self._accept_delivery(db, run, data, spec, ctx, actor)
        elif p == ("rework",):
            require_object(data, ("node_id", "reason"), ("node_id", "reason"))
            key, reason = data["node_id"], data["reason"]
            if (
                key not in PATCH_NODES
                or not isinstance(reason, str)
                or not reason.strip()
            ):
                raise ValueError("返工需指定编码或测试设计工作包以及原因")
            if (
                not spec
                or not spec["approved"]
                or nodes(run)[key]["state"] != "succeeded"
            ):
                raise RuntimeError("只有当前已批准需求下已产出的工作包可以返工")
            updated = self.app.runs.invalidate(rid, key, feedback=reason)
            reviews.invalidate_candidate(db, rid)
            result = {
                "node_id": key,
                "generation": nodes(updated)[key]["generation"],
                "next_gate": "integrate",
            }
        elif p == ("exports",):
            require_object(data, ("delivery_id",))
            result = reviews.delivery(db, rid)
            if not result:
                raise RuntimeError("交付尚未接纳")
            if data.get("delivery_id", result["decision_id"]) != result["decision_id"]:
                raise RuntimeError("交付版本已变化")
            candidate = self.gate_artifact(run, "integrate")
            report = self.gate_artifact(run, "execute-tests")
            if (
                nodes(run)["accept"]["state"] != "succeeded"
                or not spec
                or not spec["approved"]
                or not testing.verify(report, candidate, self.suite(ctx, spec))
                or result["report"]["execution_id"] != report["execution_id"]
            ):
                raise RuntimeError(
                    "已接纳记录与当前候选、套件或执行环境不符，请重新验证"
                )
            exported = export_record(result)
            self.record_decision(
                db,
                run,
                "delivery.exported",
                after=exported,
                details={"decision_id": result["decision_id"]},
            )
            return 200, exported
        elif p == ("retry-review",):
            key = data.get("node_id")
            if key not in REVIEW_NODES or not self.has_import_error(db, run, key):
                raise RuntimeError("仅能重新运行当前交付物格式无效的评审")
            self.app.runs.invalidate(rid, key)
            result = {"node_id": key}
        else:
            raise Problem(404, "NOT_FOUND", "操作不存在")
        self.record_decision(
            db,
            run,
            "delivery."
            + (data.get("kind", "approval") if p == ("approvals",) else p[0]),
            before=before,
            after={
                "result": result,
                "spec": requirements.current(db, rid),
                "candidate": reviews.candidate(db, rid),
                "delivery": reviews.delivery(db, rid),
            },
            details={"operation": list(p), "request": data},
        )
        return 200, dict(result, revision=self.app.run(rid)["revision"])

    def _request_integration(self, db, run, data, spec, before):
        rid = run["run_id"]
        require_object(
            data, ("selected", "reason", "resolution_patch"), ("selected", "reason")
        )
        selected = data["selected"]
        if (
            not isinstance(selected, list)
            or not selected
            or any(k not in PATCH_NODES for k in selected)
            or len(set(selected)) != len(selected)
            or not isinstance(data["reason"], str)
            or not data["reason"].strip()
        ):
            raise ValueError("需要选择有效工作包并说明取舍")
        if "resolution_patch" in data and not isinstance(
            data["resolution_patch"], str
        ):
            raise ValueError("冲突解决补丁须为文本")
        if (
            not spec
            or not spec["approved"]
            or any(nodes(run)[k]["state"] != "succeeded" for k in PATCH_NODES)
        ):
            raise RuntimeError("需要已批准基线及两个已完成的工作包")
        if nodes(run)["integrate"]["state"] == "succeeded":
            self.app.runs.invalidate(rid, "integrate")
            reviews.invalidate_candidate(db, rid)
            run = self.app.run(rid)
        payload = dict(
            data,
            version=spec["version"],
            gen=nodes(run)["integrate"]["generation"],
            inputs={k: nodes(run)[k]["artifact"] for k in selected},
        )
        queued = self.enqueue(db, run, "integrate", payload)
        self.record_decision(
            db,
            run,
            "delivery.integration_requested",
            before=before,
            after={"job": queued[1], "input": payload},
            details={"reason": data["reason"]},
        )
        return queued

    def _request_tests(self, db, run, data, spec, ctx, before):
        rid = run["run_id"]
        candidate = self.gate_artifact(run, "integrate")
        require_object(data, ("candidate_sha", "suite_id"), ("candidate_sha",))
        if data["candidate_sha"] != candidate["candidate_sha"]:
            raise RuntimeError("候选已变更")
        suite_id = data.get("suite_id", ctx["suite_id"])
        self.check_suite(self.repositories[ctx["repo_id"]], suite_id)
        switched = suite_id != ctx["suite_id"]
        if switched:
            ctx["suite_id"] = suite_id
            db.execute(
                "UPDATE api_context SET config=? WHERE run_id=?", (encode(ctx), rid)
            )
        suite = self.suite(ctx, spec)
        if switched or nodes(run)["execute-tests"]["state"] == "succeeded":
            self.app.runs.invalidate(rid, "execute-tests")
            reviews.invalidate_delivery(db, rid)
            run = self.app.run(rid)
        queued = self.enqueue(
            db,
            run,
            "execute-tests",
            dict(
                version=spec["version"],
                suite=suite,
                candidate_sha=candidate["candidate_sha"],
                gen=nodes(run)["execute-tests"]["generation"],
            ),
        )
        self.record_decision(
            db,
            run,
            "delivery.tests_requested",
            before=before,
            after={"job": queued[1], "suite": suite, "candidate": candidate},
        )
        return queued

    def _accept_delivery(self, db, run, data, spec, ctx, actor):
        rid = run["run_id"]
        require_object(
            data,
            ("kind", "version", "candidate_sha", "reason"),
            ("version", "candidate_sha", "reason"),
        )
        candidate = self.gate_artifact(run, "integrate")
        report = self.gate_artifact(run, "execute-tests")
        suite = self.suite(ctx, spec)
        if not testing.verify(report, candidate, suite):
            raise RuntimeError("当前候选与环境缺少有效的必测通过证据")
        review_scope = reviews.candidate(db, rid)
        if (
            not review_scope
            or review_scope["candidate_sha"] != candidate["candidate_sha"]
        ):
            raise RuntimeError("缺少当前候选的有效代码评审")
        findings = reviews.list_findings(
            db, rid, spec["version"]
        ) + reviews.list_findings(db, rid, review_scope["target"])
        baseline = next(
            s
            for s in requirements.list_specs(db, rid)
            if s["version"] == spec["version"]
        )
        manifest = export_record(
            dict(
                candidate=candidate,
                spec=baseline,
                findings=findings,
                reviews=[
                    reviews.status(db, rid, key)["submissions"]
                    for key in (spec["version"], review_scope["target"])
                ],
                report=report,
                trace=[
                    {
                        "clause_id": case["clause_id"],
                        "test_id": case["id"],
                        "patch_ids": candidate["patch_ids"],
                    }
                    for case in suite["cases"]
                ],
            )
        )
        result = reviews.accept(
            db,
            rid,
            data["version"],
            data["candidate_sha"],
            actor,
            export_record(data["reason"]),
            report=export_record(report),
            manifest=manifest,
        )
        self.app.runs.gate(rid, "accept", {"delivery": result["decision_id"]})
        return result

    def has_import_error(self, db, run, key):
        return delivery_evidence.has_import_error(db, run, key)

    def suite(self, ctx, spec):
        suite = testing.freeze_suite(spec["version"], self.suites[ctx["suite_id"]])
        if {c["id"] for c in spec["clauses"]} != {
            c["clause_id"] for c in suite["cases"]
        }:
            raise RuntimeError("每条当前需求都须映射到已登记的必测用例")
        return suite

    def current_environment(self, ctx, spec, snapshot):
        try:
            self.check_suite(self.check_binding(ctx), ctx["suite_id"])
            current = self.suite(ctx, spec)
        except (ValueError, RuntimeError, KeyError, TypeError):
            return False
        return all(
            snapshot.get(k) == current[k]
            for k in ("suite_digest", "env_digest", "spec_version_id")
        )

    def work_once(self):
        return delivery_jobs.work_once(self)
