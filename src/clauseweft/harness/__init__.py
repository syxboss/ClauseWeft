"""Frozen request evidence and explicitly authorised checkpoint-derived runs."""

from copy import deepcopy
import hashlib
import json
import time
from uuid import uuid4

from clauseweft.audit import sanitize
from clauseweft.storage import canonical
from clauseweft.api.http_contracts import Problem


class Harness:
    def __init__(self, app):
        self.app = app

    def init_schema(self, db):
        db.execute("""CREATE TABLE IF NOT EXISTS harness_checkpoints (
            id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id),
            label TEXT NOT NULL, reason TEXT NOT NULL, created REAL NOT NULL,
            actor TEXT NOT NULL, run_revision INTEGER NOT NULL, snapshot_ref TEXT NOT NULL)""")

    def contexts(self, db, run):
        rows = list(
            db.execute(
                "SELECT * FROM attempts WHERE run_id=? AND endpoint_id IS NOT NULL ORDER BY started,attempt_id",
                (run["run_id"],),
            )
        )
        inherited = {}
        for node in run["nodes"]:
            source = node.get("inherited_from")
            if not source:
                continue
            if not self.valid_inheritance(db, run, node, source):
                raise Problem(409, "INVALID_CONTEXT_PROVENANCE", "继承上下文来源不完整，不能读取该证据")
            inherited[source["attempt_id"]] = source
        for identifier, source in inherited.items():
            row = db.execute(
                "SELECT * FROM attempts WHERE attempt_id=?", (identifier,)
            ).fetchone()
            if row is not None:
                link = db.execute("SELECT project_id FROM platform_run_links WHERE run_id=?", (row["run_id"],)).fetchone()
                if link is None or link["project_id"] != run.get("project_id"):
                    raise Problem(409, "INVALID_CONTEXT_PROVENANCE", "继承上下文不属于当前项目")
                rows.append(row)
        items = []
        for row in rows:
            item = {
                key: row[key]
                for key in (
                    "attempt_id",
                    "node_id",
                    "generation",
                    "endpoint_id",
                    "model_identity",
                )
            }
            item["context_sha256"] = row["context_ref"]
            item["inherited_from"] = inherited.get(row["attempt_id"])
            if row["context_ref"]:
                value = json.loads(self.app.store.read_blob(row["context_ref"]))
                view = sanitize(value)
                item.update(
                    messages=view["messages"],
                    data_scope=view["data_scope"],
                    skills=[
                        {key: skill[key] for key in ("id", "revision", "name")}
                        for skill in view.get("skills", [])
                    ],
                    evidence=view,
                    availability="recorded",
                    redacted=view != value,
                    view_sha256=hashlib.sha256(canonical(view).encode()).hexdigest(),
                )
            else:
                item.update(
                    messages=[],
                    skills=[],
                    availability="legacy_missing",
                    redacted=False,
                )
            items.append(item)
        return {"run_id": run["run_id"], "revision": run["revision"], "items": items}

    def valid_inheritance(self, db, run, node, source):
        """Inherited evidence must come from this fork's persisted checkpoint."""
        if not isinstance(source, dict) or not source.get("attempt_id"):
            return False
        if (source.get("run_id") != run.get("source_run_id")
                or source.get("checkpoint_id") != run.get("checkpoint_id")):
            return False
        checkpoint = db.execute(
            "SELECT snapshot_ref FROM harness_checkpoints WHERE id=? AND run_id=?",
            (source.get("checkpoint_id"), source.get("run_id")),
        ).fetchone()
        link = db.execute("SELECT project_id FROM platform_run_links WHERE run_id=?", (source.get("run_id"),)).fetchone()
        if checkpoint is None or link is None or link["project_id"] != run.get("project_id"):
            return False
        try:
            snapshot = json.loads(self.app.store.read_blob(checkpoint["snapshot_ref"]))
            original = next(item for item in snapshot["nodes"] if item["node_id"] == node["node_id"])
            return (original["attempt_id"] == source["attempt_id"]
                    and original["artifact"] == source.get("artifact") == node["artifact"]
                    and original["generation"] == source.get("generation")
                    and original["state"] == "succeeded")
        except (OSError, ValueError, KeyError, TypeError, StopIteration, RuntimeError):
            return False

    def checkpoints(self, db, run_id):
        result = []
        for row in db.execute(
            "SELECT * FROM harness_checkpoints WHERE run_id=? ORDER BY created DESC,id",
            (run_id,),
        ):
            value = json.loads(self.app.store.read_blob(row["snapshot_ref"]))
            result.append(
                dict(
                    id=row["id"],
                    source_run_id=run_id,
                    label=row["label"],
                    reason=row["reason"],
                    created_at=row["created"],
                    created_by=row["actor"],
                    run_revision=row["run_revision"],
                    snapshot_sha256=row["snapshot_ref"],
                    node_states=[
                        {
                            key: node[key]
                            for key in ("node_id", "state", "generation", "artifact")
                        }
                        for node in value["nodes"]
                    ],
                )
            )
        return result

    def checkpoint(self, db, run, label, reason, actor):
        for name, value in (("label", label), ("reason", reason)):
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > (160 if name == "label" else 4000)
            ):
                raise ValueError("检查点名称或保存依据无效")
        if any(
            node["state"] in ("leased", "dispatched", "uncertain")
            for node in run["nodes"]
        ):
            raise Problem(
                409, "CHECKPOINT_BUSY", "请先等待在途调用完成或对账后恢复，再保存检查点"
            )
        if db.execute(
            "SELECT 1 FROM api_jobs WHERE run_id=? AND state IN ('queued','running')",
            (run["run_id"],),
        ).fetchone():
            raise Problem(409, "CHECKPOINT_BUSY", "请等待整合或测试任务完成")
        body = json.loads(
            db.execute(
                "SELECT body FROM runs WHERE run_id=?", (run["run_id"],)
            ).fetchone()[0]
        )
        nodes = [
            dict(row, definition=json.loads(row["definition"]))
            for row in db.execute(
                "SELECT * FROM nodes WHERE run_id=? ORDER BY rowid", (run["run_id"],)
            )
        ]
        payload = {
            "schema": "clauseweft-checkpoint-v1",
            "source_run_id": run["run_id"],
            "run_revision": run["revision"],
            "body": body,
            "nodes": nodes,
        }
        reference = self.app.store.stage_blob(canonical(payload))
        identifier = uuid4().hex
        db.execute(
            "INSERT INTO harness_checkpoints VALUES(?,?,?,?,?,?,?,?)",
            (
                identifier,
                run["run_id"],
                label.strip(),
                reason.strip(),
                time.time(),
                actor["id"],
                run["revision"],
                reference,
            ),
        )
        self.app.store.event(
            db,
            run["run_id"],
            "checkpoint_saved",
            {
                "checkpoint_id": identifier,
                "snapshot_sha256": reference,
                "actor": actor["id"],
                "reason": reason,
            },
        )
        return next(
            item
            for item in self.checkpoints(db, run["run_id"])
            if item["id"] == identifier
        )

    def fork(self, db, run, identifier, title, reason, budget, actor):
        if run["mode"] == "complex":
            raise Problem(409, "COMPLEX_FORK_UNSUPPORTED", "复杂任务可保存检查点；重新规划请创建新任务")
        if run["mode"] == "workflow":
            raise Problem(
                409,
                "DELIVERY_FORK_UNSUPPORTED",
                "仓库交付请新建研发运行，以重新绑定基线、候选和实测证据",
            )
        if not isinstance(title, str) or not title.strip() or len(title) > 160:
            raise ValueError("派生任务名称无效")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 4000:
            raise ValueError("请填写派生运行的依据")
        if not isinstance(budget, dict) or set(budget) != {"currency", "limit_micros"}:
            raise ValueError("派生运行须显式提供独立预算与币种")
        row = db.execute(
            "SELECT * FROM harness_checkpoints WHERE id=? AND run_id=?",
            (identifier, run["run_id"]),
        ).fetchone()
        if row is None:
            raise Problem(404, "CHECKPOINT_NOT_FOUND", "该运行中没有此检查点")
        snapshot = json.loads(self.app.store.read_blob(row["snapshot_ref"]))
        body = deepcopy(snapshot["body"])
        body.pop("personal_preferences", None)
        if body["mode"] not in ("single", "sop"):
            raise Problem(409, "DELIVERY_FORK_UNSUPPORTED", "此检查点不能派生通用运行")
        body.update(
            title=title.strip(),
            budget=budget,
            source_run_id=run["run_id"],
            checkpoint_id=identifier,
            created_by=actor["id"],
        )
        if body["mode"] == "sop":
            body["template_snapshot"]["nodes"] = [
                item["definition"] for item in snapshot["nodes"]
            ]
        created = self.app.start(db, body, {})
        inherited = set()
        for node in snapshot["nodes"]:
            definition = deepcopy(node["definition"])
            keep = (
                definition["kind"] == "model"
                and node["state"] == "succeeded"
                and node["artifact"]
                and set(definition["dependencies"]) <= inherited
            )
            if keep:
                self.app.store.read_blob(node["artifact"])
                definition["inherited_from"] = {
                    "run_id": run["run_id"],
                    "checkpoint_id": identifier,
                    "node_id": node["node_id"],
                    "generation": node["generation"],
                    "attempt_id": node["attempt_id"],
                    "artifact": node["artifact"],
                }
                db.execute(
                    """UPDATE nodes SET state='succeeded',artifact=?,attempt_id=?,input_digest=?,definition=?
                           WHERE run_id=? AND node_id=?""",
                    (
                        node["artifact"],
                        node["attempt_id"],
                        node["input_digest"],
                        canonical(definition),
                        created["run_id"],
                        node["node_id"],
                    ),
                )
                inherited.add(node["node_id"])
            else:
                definition.pop("inherited_from", None)
                db.execute(
                    "UPDATE nodes SET definition=? WHERE run_id=? AND node_id=?",
                    (canonical(definition), created["run_id"], node["node_id"]),
                )
        self.app.store.event(
            db,
            created["run_id"],
            "fork_created",
            {
                "source_run_id": run["run_id"],
                "checkpoint_id": identifier,
                "snapshot_sha256": row["snapshot_ref"],
                "inherited_nodes": sorted(inherited),
                "actor": actor["id"],
                "reason": reason,
                "budget": budget,
            },
        )
        self.app.runs._refresh(db, created["run_id"])
        return self.app.run(created["run_id"])
