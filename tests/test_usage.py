"""Actual token receipts, immutable tariffs and authorized model accounting."""

import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from clauseweft import audit
from clauseweft.api.application import Application
from clauseweft.api.platform import BOOTSTRAP
from clauseweft.api.server import Problem
from clauseweft.storage import SCHEMA, Store
from clauseweft.usage import Usage
from platform_fixture import prepare_team


class ModelUsageTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        config = self.root / "config.json"
        config.write_text(
            json.dumps({"gateways": {"g": {"url": "http://127.0.0.1:8765"}}})
        )
        self.app = Application(self.root / "state", config)
        self.usage = Usage(self.app)
        self.a = self.model("A", "USD", 1_000_000, 2_000_000)
        self.b = self.model("B", "CNY", 3_000_000, 4_000_000)
        self.c = self.model("C", "USD", 6_000_000, 8_000_000)
        prepare_team(self.app)

    def model(self, name, currency, in_price, out_price):
        return self.app.catalog.save(
            dict(
                endpoint_id=name,
                model_identity="provider-" + name,
                identity_verified=True,
                gateway_id="g",
                gateway_etag='"fixture"',
                enabled=True,
                allowed=True,
                location="local",
                context_tokens=10000,
                capabilities=[],
                fit={"review": 5},
                priority=1,
                currency=currency,
                input_micros_per_million=in_price,
                output_micros_per_million=out_price,
            )
        )

    def call(self, method, path, body=None, revision=None, actor=None, query=None):
        return self.app.handle(
            method,
            tuple(path.split("/")),
            query or {},
            body or {},
            uuid4().hex,
            revision,
            actor,
        )[1]

    def post(self, path, body, revision=None, actor=None):
        return self.call("POST", path, body, revision, actor)

    def report(self, actor=None, **query):
        with self.app.store.transaction() as db:
            return self.usage.report(db, actor or BOOTSTRAP, query)

    def dispatch(self, endpoint="A", project_id="default", *, pin=True):
        model = self.app.catalog.get(endpoint)
        task = {"text": "评审项目内容", "stage": "review", "max_output_tokens": 30}
        if pin:
            task["pinned_endpoint"] = endpoint
        run = self.post(
            "runs",
            {
                "project_id": project_id,
                "task": task,
                "budget": {"currency": model["currency"], "limit_micros": 100000},
            },
        )
        attempt = self.app.runs.claim()
        self.assertIsNotNone(attempt)
        self.assertIsNotNone(self.app.runs.dispatch(attempt))
        return run, attempt

    def finish(self, attempt, *, usage=None, receipt=None):
        if usage is None:
            usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        self.app.runs.finish(
            attempt, text="已完成", usage=usage, receipt=receipt or uuid4().hex
        )

    @staticmethod
    def row(report, endpoint="A", revision=1):
        return next(
            row
            for row in report["models"]
            if row["endpoint_id"] == endpoint and row["endpoint_revision"] == revision
        )

    def test_configured_models_show_zero_calls_without_invented_spend(self):
        report = self.report()
        self.assertEqual(report["totals"], [])
        self.assertEqual(len(report["models"]), 3)
        for row in report["models"]:
            self.assertTrue(row["configured"])
            self.assertEqual(row["calls"], 0)
            self.assertEqual(row["total_tokens"], 0)
            self.assertEqual((row["settled_micros"], row["held_micros"]), (0, 0))
            self.assertEqual(row["pricing_source"], "current_configuration")

    def test_actual_usage_and_cost_are_separated_by_model_and_currency(self):
        for endpoint in ("A", "B"):
            _, attempt = self.dispatch(endpoint)
            self.finish(attempt)
        report = self.report()
        a, b = self.row(report), self.row(report, "B")
        self.assertEqual(
            (a["input_tokens"], a["output_tokens"], a["total_tokens"]), (10, 5, 15)
        )
        self.assertEqual((a["settled_micros"], b["settled_micros"]), (20, 50))
        self.assertEqual(
            {row["currency"]: row["settled_micros"] for row in report["totals"]},
            {"USD": 20, "CNY": 50},
        )
        self.assertEqual(self.row(report, "C")["calls"], 0)
        self.assertTrue(all(row["usage_complete"] for row in report["totals"]))

    def test_dispatch_tariff_survives_price_edits_and_is_audited(self):
        run, attempt = self.dispatch()
        changed = self.app.catalog.save(
            dict(
                self.a,
                input_micros_per_million=90_000_000,
                output_micros_per_million=99_000_000,
            )
        )
        self.finish(attempt)
        report = self.report()
        old = self.row(report)
        self.assertEqual(old["settled_micros"], 20)
        self.assertFalse(old["configured"])
        self.assertEqual(old["pricing_source"], "dispatch_snapshot")
        self.assertEqual(old["pricing_snapshot"]["input_micros_per_million"], 1_000_000)
        self.assertEqual(self.row(report, revision=changed["revision"])["calls"], 0)
        with self.app.store.transaction() as db:
            row = db.execute(
                "SELECT pricing_snapshot,usage FROM attempts WHERE attempt_id=?",
                (attempt,),
            ).fetchone()
            self.assertEqual(json.loads(row["usage"])["total_tokens"], 15)
            entries = audit.list_entries(db, {"run_id": run["run_id"]})
            dispatched = next(
                item for item in entries if item["action"] == "run.dispatched"
            )
            finished = next(
                item for item in entries if item["action"] == "run.attempt_finished"
            )
            self.assertEqual(
                dispatched["details"]["evidence"]["pricing_snapshot"],
                json.loads(row["pricing_snapshot"]),
            )
            self.assertEqual(
                finished["details"]["evidence"]["usage"], json.loads(row["usage"])
            )
            self.assertTrue(audit.verify(db)["valid"])

    def test_missing_and_invalid_usage_remain_unknown_and_keep_holds(self):
        _, known = self.dispatch()
        self.finish(known)
        _, missing = self.dispatch()
        self.app.runs.finish(missing, text="无用量回执", receipt="missing")
        _, invalid = self.dispatch()
        self.finish(
            invalid,
            usage={"prompt_tokens": 10, "completion_tokens": -1, "total_tokens": 9},
        )
        row = self.row(self.report())
        self.assertEqual(row["calls"], 3)
        self.assertIsNone(row["input_tokens"])
        self.assertIsNone(row["output_tokens"])
        self.assertEqual(
            (row["known_input_tokens"], row["known_output_tokens"]), (10, 5)
        )
        self.assertFalse(row["usage_complete"])
        self.assertEqual((row["pending_calls"], row["missing_usage_calls"]), (2, 2))
        self.assertGreater(row["held_micros"], 0)
        self.assertEqual(row["settled_micros"], 20)

    def test_explicit_free_model_and_zero_usage_are_recorded_as_known(self):
        self.model("free", "CNY", 0, 0)
        prepare_team(self.app)
        _, attempt = self.dispatch("free")
        self.finish(
            attempt,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        )
        row = self.row(self.report(), "free")
        self.assertTrue(row["usage_complete"])
        self.assertEqual(
            (row["total_tokens"], row["settled_micros"], row["held_micros"]), (0, 0, 0)
        )
        self.assertEqual(row["pending_calls"], 0)

    def test_retries_and_unknown_failures_are_included_in_model_statistics(self):
        run, first = self.dispatch(pin=False)
        self.app.runs.finish(first, error="upstream_rate_limited")
        with self.app.store.transaction() as db:
            db.execute("UPDATE nodes SET ready_at=0 WHERE run_id=?", (run["run_id"],))
        second = self.app.runs.claim()
        self.assertEqual(self.app.runs.dispatch(second)[0]["endpoint_id"], "C")
        self.finish(second)
        report = self.report()
        self.assertEqual(self.row(report)["pending_calls"], 1)
        self.assertEqual(self.row(report)["failed_calls"], 1)
        self.assertIsNone(self.row(report)["total_tokens"])
        self.assertEqual(self.row(report, "C")["calls"], 1)
        total = report["totals"][0]
        self.assertEqual(
            (total["calls"], total["pending_calls"], total["missing_usage_calls"]),
            (2, 1, 1),
        )

    def test_no_charge_rejection_and_duplicate_receipt_do_not_fabricate_tokens(self):
        _, first = self.dispatch()
        self.finish(first, receipt="one-provider-receipt")
        run, duplicate = self.dispatch()
        self.finish(duplicate, receipt="one-provider-receipt")
        row = self.row(self.report())
        self.assertEqual(row["settled_micros"], 20)
        self.assertEqual(
            (row["calls"], row["known_total_tokens"], row["missing_usage_calls"]),
            (2, 15, 1),
        )
        self.app.runs.reconcile(
            run["run_id"], duplicate, 13, "owner", "账单金额 13 微单位"
        )
        row = self.row(self.report())
        self.assertEqual(
            (row["settled_micros"], row["pending_calls"], row["held_micros"]),
            (33, 0, 0),
        )
        self.assertIsNone(row["total_tokens"])
        _, rejected = self.dispatch("C")
        self.app.runs.finish(rejected, error="model_not_found")
        row = self.row(self.report(), "C")
        self.assertEqual((row["settled_micros"], row["pending_calls"]), (0, 0))
        self.assertIsNone(row["total_tokens"])

    def test_legacy_price_uses_exact_revision_and_missing_tokens_are_explicit(self):
        _, attempt = self.dispatch()
        self.finish(attempt)
        self.app.catalog.save(dict(self.a, input_micros_per_million=90_000_000))
        with self.app.store.transaction() as db:
            db.execute(
                "UPDATE attempts SET pricing_snapshot=NULL,usage=NULL WHERE attempt_id=?",
                (attempt,),
            )
        row = self.row(self.report())
        self.assertEqual(row["pricing_source"], "historical_catalog")
        self.assertEqual(row["pricing_snapshot"]["input_micros_per_million"], 1_000_000)
        self.assertEqual(row["settled_micros"], 20)
        self.assertIsNone(row["total_tokens"])
        self.assertEqual(row["missing_usage_calls"], 1)

    def test_statistics_obey_workspace_project_and_current_role_grants(self):
        other = self.post("projects", {"name": "其他项目"})
        for project_id in ("default", other["id"]):
            _, attempt = self.dispatch(project_id=project_id)
            self.finish(attempt)
        operator = self.post(
            "members",
            {
                "username": "usage-operator",
                "password": "usage-test-password",
                "role": "operator",
                "workspace_ids": ["default"],
                "project_ids": ["default"],
            },
        )
        viewer = self.post(
            "members",
            {
                "username": "usage-viewer",
                "password": "usage-test-password",
                "role": "viewer",
                "workspace_ids": ["default"],
                "project_ids": ["default"],
            },
        )
        self.assertEqual(self.row(self.report(actor=operator))["calls"], 1)
        with self.assertRaises(Problem):
            self.report(actor=operator, project_id=other["id"])
        with self.assertRaises(Problem):
            self.report(actor=operator, workspace_id="example")
        with self.assertRaises(Problem) as readonly:
            self.report(actor=viewer)
        self.assertEqual(readonly.exception.status, 403)
        self.call(
            "PATCH",
            "members/" + operator["id"],
            {"project_ids": []},
            revision=operator["revision"],
        )
        self.assertEqual(self.report(actor=operator)["totals"], [])
        self.assertTrue(
            all(row["calls"] == 0 for row in self.report(actor=operator)["models"])
        )

    def test_removed_model_remains_only_as_authorized_historical_usage(self):
        _, attempt = self.dispatch()
        self.finish(attempt)
        with self.app.store.transaction() as db:
            workspace = self.app.platform.get(db, "workspaces", "default")
        self.call(
            "PATCH",
            "workspaces/default",
            {"endpoint_ids": []},
            revision=workspace["revision"],
        )
        report = self.report()
        self.assertEqual(len(report["models"]), 1)
        self.assertFalse(report["models"][0]["configured"])
        self.assertEqual(report["models"][0]["calls"], 1)

    def test_usage_api_applies_permissions_and_current_account_state(self):
        viewer = self.post(
            "members",
            {
                "username": "api-viewer",
                "password": "usage-test-password",
                "role": "viewer",
            },
        )
        self.assertEqual(
            self.call("GET", "usage")["basis"], "dispatch_pricing_and_reported_usage"
        )
        with self.assertRaises(Problem) as denied:
            self.call("GET", "usage", actor=viewer)
        self.assertEqual(denied.exception.status, 403)

    def test_accounting_columns_are_bound_to_audited_dispatch_and_receipt(self):
        _, attempt = self.dispatch()
        self.finish(attempt)
        with self.app.store.transaction() as db:
            self.assertTrue(audit.verify(db)["valid"])
            for field, changed in (
                (
                    "pricing_snapshot",
                    {"currency": "CNY", "input_micros_per_million": 0},
                ),
                (
                    "usage",
                    {
                        "status": "reported",
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "total_tokens": 0,
                    },
                ),
            ):
                with self.subTest(field=field):
                    db.execute("SAVEPOINT accounting_tamper")
                    db.execute(
                        f"UPDATE attempts SET {field}=? WHERE attempt_id=?",
                        (json.dumps(changed), attempt),
                    )
                    self.assertFalse(audit.verify(db)["valid"])
                    db.execute("ROLLBACK TO accounting_tamper")
                    db.execute("RELEASE accounting_tamper")
                    self.assertTrue(audit.verify(db)["valid"])


class UsageStorageUpgradeTest(unittest.TestCase):
    def test_old_database_gains_nullable_usage_columns_without_invented_history(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "workspace.sqlite3"
            with sqlite3.connect(path) as db:
                for statement in SCHEMA:
                    db.execute(
                        statement.replace(
                            "        pricing_snapshot TEXT, usage TEXT,\n", ""
                        )
                    )
                db.execute("PRAGMA user_version=1")
                db.execute(
                    "INSERT INTO runs(run_id,body,status,created,limit_micros,currency) VALUES('legacy','{}','completed',1,0,'CNY')"
                )
            upgraded = Store(directory)
            with upgraded.transaction() as db:
                fields = {
                    row["name"] for row in db.execute("PRAGMA table_info(attempts)")
                }
                self.assertTrue({"pricing_snapshot", "usage"} <= fields)
                self.assertEqual(
                    db.execute(
                        "SELECT currency FROM runs WHERE run_id='legacy'"
                    ).fetchone()[0],
                    "CNY",
                )
                self.assertEqual(
                    db.execute("SELECT count(*) FROM attempts").fetchone()[0], 0
                )


if __name__ == "__main__":
    unittest.main()
