"""Collect the deployment snapshot and evidence partitions for audited exports."""

import hashlib
import json
from importlib.resources import files
from pathlib import Path

from clauseweft import audit, testing
from .http_contracts import encode


def _workbench_sources():
    web = files("clauseweft.api.web")
    sources = {"clauseweft/api/web/page.html": web.joinpath("page.html").read_text(encoding="utf-8")}
    for folder, extension in (("scripts", ".js"), ("styles", ".css")):
        for resource in web.joinpath(folder).iterdir():
            if resource.name.endswith(extension) and resource.is_file():
                sources[f"clauseweft/api/web/{folder}/{resource.name}"] = resource.read_text(encoding="utf-8")
    return sources


def record_deployment(store, db, config):
    from clauseweft import __version__
    from clauseweft.routing.preview import RULESET

    package = Path(__file__).resolve().parents[1]
    sources = {
        "clauseweft/" + str(path.relative_to(package)): path.read_text(encoding="utf-8")
        for path in sorted(package.rglob("*.py"))
    }
    sources.update(_workbench_sources())
    project = package.parent.parent
    if package.is_dir() and (project / "pyproject.toml").is_file():
        paths = [
            project / name
            for name in (
                "pyproject.toml",
                "README.md",
                "AGENTS.md",
                "CONTRIBUTING.md",
                "SECURITY.md",
                "LICENSE",
                "Dockerfile",
                "MANIFEST.in",
            )
        ]
        for folder, pattern in (
            ("docs", "*.md"),
            ("tests", "*.py"),
            ("scripts", "*.py"),
            ("examples", "*.json"),
            (".github/workflows", "*.yml"),
        ):
            paths.extend(sorted((project / folder).glob(pattern)))
        for path in paths:
            if path.is_file():
                sources[str(path.relative_to(project))] = path.read_text()
    files = {
        name: hashlib.sha256(content.encode()).hexdigest()
        for name, content in sources.items()
    }
    source_reference = (
        store.stage_blob(
            encode({"schema": "clauseweft-source-snapshot-v1", "files": sources})
        )
        if sources
        else None
    )
    current = dict(
        version=__version__,
        ruleset=RULESET,
        test_adapter=testing.ADAPTER,
        config=audit.sanitize(config),
        manifest=files,
        source_snapshot_ref=source_reference,
    )
    previous = (
        db.execute(
            "SELECT after_snapshot FROM deployment_snapshot WHERE id=1"
        ).fetchone()
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='deployment_snapshot'"
        ).fetchone()
        else None
    )
    db.execute(
        "CREATE TABLE IF NOT EXISTS deployment_snapshot(id INTEGER PRIMARY KEY,after_snapshot TEXT NOT NULL)"
    )
    before = json.loads(previous[0]) if previous else None
    audit.record(
        db,
        audit.SYSTEM,
        "deployment.started",
        "deployment",
        before=before,
        after=current,
        details={"changed": before != current, "manifest_available": bool(files)},
    )
    db.execute(
        "INSERT OR REPLACE INTO deployment_snapshot VALUES(1,?)", (encode(current),)
    )


def _evidence_references(db, entries):
    references = set()
    for row in db.execute("SELECT context_ref,artifact FROM attempts"):
        references.update(value for value in row if value)
    references.update(
        row[0]
        for row in db.execute(
            "SELECT artifact FROM nodes WHERE artifact IS NOT NULL"
        )
    )
    references.update(
        row[0]
        for row in db.execute("SELECT snapshot_ref FROM harness_checkpoints")
    )
    references.update(
        entry["after"]["source_snapshot_ref"]
        for entry in entries
        if entry["action"] == "deployment.started"
        and isinstance(entry.get("after"), dict)
        and entry["after"].get("source_snapshot_ref")
    )
    references.update(
        entry["details"]["evidence"]["response_sha256"]
        for entry in entries
        if entry["action"] in ("run.attempt_finished", "run.receipt_ignored")
        and entry["details"].get("evidence", {}).get("response_sha256")
    )
    return references


def _evidence_views(store, references):
    evidence = []
    for reference in sorted(references):
        try:
            raw = store.read_blob(reference)
            content = raw.decode()
            try:
                content = json.loads(content)
            except ValueError:
                pass
            view = audit.sanitize(content)
            evidence.append(
                {
                    "sha256": reference,
                    "size": len(raw),
                    "view": view,
                    "view_sha256": hashlib.sha256(
                        encode(view).encode()
                    ).hexdigest(),
                    "redacted": content != view,
                    "availability": "recorded",
                }
            )
        except (OSError, ValueError, UnicodeError, RuntimeError):
            evidence.append(
                {"sha256": reference, "availability": "missing_or_invalid"}
            )
    return evidence


def export(app, db, query):
    bundle = audit.export_bundle(db, query)
    evidence = _evidence_views(app.store, _evidence_references(db, bundle["entries"]))
    bundle["evidence"] = evidence
    bundle["delivery_jobs"] = [
        audit.sanitize(
            dict(
                row, result=json.loads(row["result"]) if row["result"] else None
            )
        )
        for row in db.execute(
            "SELECT id,run_id,kind,state,reason,result FROM api_jobs ORDER BY rowid"
        )
    ]
    bundle["business_records"] = {}
    for table in (
        "mw_spec",
        "mw_decision_head",
        "mw_review_target",
        "mw_review",
        "mw_finding",
        "mw_resolution",
        "mw_signed_decision",
        "api_evaluations",
        "attempts",
    ):
        if db.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone():
            bundle["business_records"][table] = [
                audit.sanitize(dict(row))
                for row in db.execute("SELECT * FROM " + table)
            ]
    bundle["evidence_status"] = (
        "complete"
        if all(item["availability"] == "recorded" for item in evidence)
        else "incomplete"
    )
    manifest = {
        key + "_sha256": hashlib.sha256(
            encode(bundle[key]).encode()
        ).hexdigest()
        for key in ("evidence", "delivery_jobs", "business_records")
    }
    manifest["database_verification"] = audit.verify(db)
    manifest_entry = audit.record(
        db,
        app.actor(),
        "audit.export_prepared",
        "audit",
        after=manifest,
        details={"scope": audit.sanitize(query)},
        correlation_id=getattr(app.request_context, "correlation_id", None),
    )
    proof = audit.export_bundle(db, query)
    for key in (
        "evidence",
        "evidence_status",
        "delivery_jobs",
        "business_records",
    ):
        proof[key] = bundle[key]
    proof["manifest_seq"] = manifest_entry["seq"]
    return proof
