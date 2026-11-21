"""Run lifecycle and public worker operations."""

import json
import sqlite3
import threading
import time
from uuid import uuid4

from clauseweft import workflow
from clauseweft.catalog import GatewayFailure
from clauseweft.routing.preview import preview as route
from clauseweft.storage import canonical, digest

from . import leases, receipts, state
from .dispatch import dispatch_attempt
from .receipts import INCOMPLETE_OUTPUT
from .requests import budget_limit, build_messages, normalize_task, route_snapshot


class Runs:
    """Coordinate run lifecycle, workers and human decisions against one store and catalog."""

    def __init__(self, store, catalog, scope_for=None, models_for=None):
        self.store = store
        self.catalog = catalog
        self.scope_for = scope_for
        self.models_for = models_for

    def _models(self, body):
        return self.models_for(body) if self.models_for is not None else self.catalog.list()

    def _effective_task(self, body):
        task = normalize_task(body.get("task"), body.get("mode", "single"))
        if self.scope_for is not None:
            scope = self.scope_for(body)
            if scope == "blocked":
                task["_workspace_blocked"] = True
            elif scope not in ("local_only", "public"):
                raise ValueError("工作区数据策略无效")
            if scope == "local_only":
                task["data_scope"] = scope
        return task

    def snapshot(self, body):
        task = self._effective_task(body)
        limit, currency = budget_limit(body)
        plan = workflow.compile(dict(body, task=task))
        definition = next((n for n in plan if n["kind"] == "model"), plan[0])
        messages = build_messages(task, definition, {}, body)
        return route_snapshot(
            task, definition, messages, self._models(body), limit, currency, body.get("endpoint_revisions")
        )

    def preview(self, body):
        concurrency = body.get("max_concurrency", 4)
        if type(concurrency) is not int or not 1 <= concurrency <= 256:
            raise ValueError("无效的运行并发数")
        plan = workflow.compile(body)
        if not any(n["kind"] == "model" for n in plan):
            return dict(status="needs_input", selected_endpoint=None, candidates=[],
                        reason="human_gate", nodes=plan,
                        data_scope=self._effective_task(body)["data_scope"])
        return route(self.snapshot(body))

    def start(self, body):
        mode = body.get("mode", "single")
        body = dict(body, mode=mode, task=normalize_task(body.get("task"), mode))
        limit, currency = budget_limit(body)
        if body.get("plan_template_id", "delivery-v1") != "delivery-v1":
            raise ValueError("未知的流程模板")
        if "endpoint_revisions" in body:
            frozen = body["endpoint_revisions"]
            if not isinstance(frozen, dict) or any(
                not isinstance(k, str) or type(v) is not int or v < 1 for k, v in frozen.items()
            ):
                raise ValueError("无效的冻结端点清单")
        self.preview(body)
        plan = workflow.compile(body)
        run_id = uuid4().hex
        with self.store.transaction() as db:
            db.execute(
                """INSERT INTO runs(run_id,revision,body,status,created,limit_micros,currency)
                          VALUES(?,0,?,'queued',?,?,?)""",
                (run_id, canonical(body), time.time(), limit, currency),
            )
            state.insert_nodes(db, run_id, plan)
            self.store.event(db, run_id, "run_created", {"mode": body["mode"]})
            state.refresh(db, run_id)
            return self.get(run_id)

    def get(self, run_id):
        with self.store.transaction() as db:
            return state.describe_run(db, run_id)

    def list(self):
        with self.store.transaction() as db:
            return [
                self.get(r[0]) for r in db.execute("SELECT run_id FROM runs ORDER BY created DESC,run_id")
            ]

    def _refresh(self, db, run_id):
        state.refresh(db, run_id)

    def claim(self):
        return leases.claim(self.store)

    def dispatch(self, attempt_id):
        return dispatch_attempt(self.store, attempt_id, models_for=self._models, task_for=self._effective_task)

    def renew(self, attempt_id):
        return leases.renew(self.store, attempt_id)

    def _heartbeat(self, attempt_id, stop):
        while not stop.wait(leases.LEASE_SECONDS / 3):
            try:
                if not self.renew(attempt_id):
                    return
            except sqlite3.OperationalError:
                # A lock can clear before the next interval. Expiry remains fenced.
                continue

    def work_once(self):
        attempt_id = self.claim()
        if not attempt_id:
            return False
        permit = self.dispatch(attempt_id)
        if permit:
            stop = threading.Event()
            heartbeat = threading.Thread(target=self._heartbeat, args=(attempt_id, stop), daemon=True)
            heartbeat.start()
            try:
                reply = self.catalog.generate(*permit)
            except GatewayFailure as err:
                self.finish(attempt_id, error=err.code, receipt=err.receipt)
            else:
                self.finish(attempt_id, *reply)
            finally:
                stop.set()
                heartbeat.join()
        return True

    def finish(self, attempt_id, text=None, usage=None, receipt=None, finish_reason=None,
               effective_max_tokens=None, *, error=None):
        return receipts.finish_attempt(self.store, self.catalog, attempt_id, text, usage, receipt, finish_reason,
                                       effective_max_tokens, error=error)

    def _active(self, db, run_id):
        row = state.load_run(db, run_id)
        if row["status"] == "cancelled":
            raise RuntimeError("已取消的运行需要新建")
        return row

    def cancel(self, run_id):
        with self.store.transaction() as db:
            row = state.load_run(db, run_id)
            if row["status"] == "completed":
                raise RuntimeError("已完成的运行不能取消")
            if row["status"] != "cancelled":
                db.execute("UPDATE runs SET status='cancelled' WHERE run_id=?", (run_id,))
                db.execute(
                    "UPDATE nodes SET state='cancelled',reason='cancelled' WHERE run_id=? AND state!='succeeded'",
                    (run_id,),
                )
                db.execute(
                    """UPDATE attempts SET state='uncertain',accounting='uncertain'
                              WHERE run_id=? AND state='dispatched'""",
                    (run_id,),
                )
                db.execute(
                    "UPDATE attempts SET state='cancelled',finished=? WHERE run_id=? AND state='leased'",
                    (time.time(), run_id),
                )
                self.store.event(db, run_id, "cancelled", {})
            return self.get(run_id)

    def resume(self, run_id):
        with self.store.transaction() as db:
            self._active(db, run_id)
            if state.load_run(db, run_id)["status"] == "paused":
                db.execute("UPDATE runs SET status='queued' WHERE run_id=?", (run_id,))
                db.execute("UPDATE nodes SET ready_at=0,reason=NULL WHERE run_id=? AND reason='run_paused'",
                           (run_id,))
                self.store.event(db, run_id, "run_resumed", {})
            leases.expire(self.store, db)
            for n in state.load_nodes(db, run_id).values():
                if n["definition"]["kind"] != "model" or n["state"] not in {"needs_input", "uncertain"}:
                    continue
                if n["state"] == "uncertain" or n["reason"] in INCOMPLETE_OUTPUT:
                    att = db.execute(
                        "SELECT accounting FROM attempts WHERE attempt_id=?", (n["attempt_id"],)
                    ).fetchone()
                    if att and att[0] != "settled":
                        continue
                db.execute(
                    "UPDATE nodes SET state='ready',reason=NULL,ready_at=0 WHERE run_id=? AND node_id=?",
                    (run_id, n["node_id"]),
                )
                self.store.event(db, run_id, "node_resumed", {"node_id": n["node_id"]})
            state.refresh(db, run_id)
            return self.get(run_id)

    def pause(self, run_id):
        with self.store.transaction() as db:
            run = self._active(db, run_id)
            if run["status"] == "completed":
                raise RuntimeError("已完成的运行不能暂停")
            if run["status"] != "paused":
                db.execute("UPDATE runs SET status='paused' WHERE run_id=?", (run_id,))
                self.store.event(db, run_id, "run_paused", {})
            return self.get(run_id)

    def reconcile(self, run_id, attempt_id, actual_micros, actor, evidence):
        return receipts.reconcile(self.store, run_id, attempt_id, actual_micros, actor, evidence)

    def gate(self, run_id, node_id, value):
        artifact = self.store.stage_blob(canonical(value))
        with self.store.transaction() as db:
            self._active(db, run_id)
            nodes = state.load_nodes(db, run_id)
            n = nodes[node_id]
            if n["definition"]["kind"] != "gate" or n["state"] != "needs_input":
                raise RuntimeError("前置分支尚未成功，或关卡已处理")
            refs = state.input_refs(n, nodes)
            if any(nodes[k]["state"] != "succeeded" for k in refs):
                raise RuntimeError("上游输入已失效")
            db.execute(
                "UPDATE nodes SET state='succeeded',artifact=?,input_digest=?,reason=NULL WHERE run_id=? AND node_id=?",
                (artifact, digest(refs), run_id, node_id),
            )
            if node_id == "approve-spec" and isinstance(value, dict):
                db.execute(
                    "UPDATE runs SET spec_version_id=? WHERE run_id=?", (value.get("spec_version_id"), run_id)
                )
            if node_id == "integrate" and isinstance(value, dict):
                db.execute(
                    "UPDATE runs SET candidate_sha=? WHERE run_id=?", (value.get("candidate_sha"), run_id)
                )
            self.store.event(db, run_id, "gate_completed", {"node_id": node_id, "artifact": artifact})
            if n["definition"].get("tool_step"):
                body = json.loads(state.load_run(db, run_id)["body"])
                if body["mode"] == "tool" and "allowed_tools" in body:
                    step = n["definition"]["tool_step"] + 1
                    state.insert_nodes(db, run_id, [workflow.tool_decision_node(body, step, value)])
            state.refresh(db, run_id)
            return self.get(run_id)

    def _invalidate(self, db, run_id, node_id):
        run = self._active(db, run_id)
        nodes = state.load_nodes(db, run_id)
        if node_id not in nodes:
            raise KeyError(node_id)
        if node_id == "plan" and "summary" in nodes and json.loads(run["body"])["mode"] == "complex":
            raise RuntimeError("分工已生成，重新规划请新建复杂任务")
        affected = {node_id}
        for key, n in nodes.items():  # Stored template order is topological.
            if affected.intersection(n["definition"]["dependencies"]):
                affected.add(key)
        for key in sorted(affected):
            n = nodes[key]
            db.execute(
                """UPDATE attempts SET state='uncertain',accounting='uncertain' WHERE attempt_id=?
                          AND state='dispatched'""",
                (n["attempt_id"],),
            )
            db.execute(
                "UPDATE attempts SET state='superseded' WHERE attempt_id=? AND state='leased'",
                (n["attempt_id"],),
            )
            definition = n["definition"]
            definition.pop("feedback", None)
            db.execute(
                """UPDATE nodes SET state='waiting',generation=generation+1,input_digest=NULL,artifact=NULL,
                          attempt_id=NULL,reason=NULL,ready_at=0,definition=? WHERE run_id=? AND node_id=?""",
                (canonical(definition), run_id, key),
            )
        if "approve-spec" in affected:
            db.execute("UPDATE runs SET spec_version_id=NULL WHERE run_id=?", (run_id,))
        if "integrate" in affected:
            db.execute("UPDATE runs SET candidate_sha=NULL WHERE run_id=?", (run_id,))
        self.store.event(db, run_id, "invalidated", {"source": node_id, "nodes": sorted(affected)})

    def invalidate(self, run_id, node_id, feedback=None):
        if feedback is not None and (not isinstance(feedback, str) or not feedback.strip()):
            raise ValueError("返工说明须为非空文本")
        with self.store.transaction() as db:
            self._invalidate(db, run_id, node_id)
            if feedback is not None:
                definition = state.load_nodes(db, run_id)[node_id]["definition"]
                definition["feedback"] = feedback
                db.execute(
                    "UPDATE nodes SET definition=? WHERE run_id=? AND node_id=?",
                    (canonical(definition), run_id, node_id),
                )
                self.store.event(db, run_id, "rework_requested", {"node_id": node_id, "feedback": feedback})
            state.refresh(db, run_id)
            return self.get(run_id)

    def replace_input(self, run_id, node_id, value):
        artifact = self.store.stage_blob(canonical(value))
        with self.store.transaction() as db:
            if node_id != "draft":
                raise ValueError("仅支持人工修订需求草稿")
            self._invalidate(db, run_id, node_id)
            db.execute(
                """UPDATE nodes SET state='succeeded',input_digest=?,artifact=? WHERE run_id=? AND node_id=?""",
                (digest(value), artifact, run_id, node_id),
            )
            self.store.event(db, run_id, "input_replaced", {"node_id": node_id, "artifact": artifact})
            state.refresh(db, run_id)
            return self.get(run_id)
