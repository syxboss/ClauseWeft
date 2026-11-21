"""Project-scoped model usage from dispatched prices and provider token receipts."""

import json

from clauseweft.api.http_contracts import Problem


def pricing_snapshot(model):
    """Freeze the actual versioned tariff before making a request."""
    return {
        "endpoint_id": model["endpoint_id"],
        "endpoint_revision": model["revision"],
        "model_identity": model["model_identity"],
        "currency": model["currency"],
        "input_micros_per_million": model.get("input_micros_per_million"),
        "output_micros_per_million": model.get("output_micros_per_million"),
    }


def normalized_usage(value):
    """Keep an explicit unknown state instead of turning missing receipts into zero."""
    if value is None:
        return {"status": "missing"}
    if not isinstance(value, dict):
        return {"status": "invalid"}
    fields = ("prompt_tokens", "completion_tokens", "total_tokens")
    counts = [value.get(key) for key in fields]
    if (
        not all(type(count) is int and 0 <= count <= 2**31 for count in counts)
        or counts[0] + counts[1] != counts[2]
    ):
        return {"status": "invalid"}
    result = dict(status="reported", **dict(zip(fields, counts)))
    cached = value.get("cached_tokens")
    if type(cached) is int and 0 <= cached <= counts[0]:
        result["cached_tokens"] = cached
    reasoning = value.get("reasoning_tokens")
    if type(reasoning) is int and 0 <= reasoning <= counts[1]:
        result["reasoning_tokens"] = reasoning
    return result


def counters():
    return dict(
        calls=0,
        input_tokens=0,
        output_tokens=0,
        total_tokens=0,
        cached_tokens=0,
        reasoning_tokens=0,
        known_input_tokens=0,
        known_output_tokens=0,
        known_total_tokens=0,
        usage_complete=True,
        missing_usage_calls=0,
        settled_micros=0,
        held_micros=0,
        pending_calls=0,
        failed_calls=0,
    )


def add_attempt(target, attempt):
    target["calls"] += 1
    target["settled_micros"] += attempt["settled"]
    target["held_micros"] += attempt["hold"]
    target["pending_calls"] += attempt["accounting"] != "settled"
    target["failed_calls"] += bool(attempt["error"])
    usage = json.loads(attempt["usage"]) if attempt["usage"] else None
    if usage and usage.get("status") == "reported":
        for source, name in (
            ("prompt_tokens", "input_tokens"),
            ("completion_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            target["known_" + name] += usage[source]
            if target[name] is not None:
                target[name] += usage[source]
        if type(usage.get("cached_tokens")) is int:
            target["cached_tokens"] += usage["cached_tokens"]
        if type(usage.get("reasoning_tokens")) is int:
            target["reasoning_tokens"] += usage["reasoning_tokens"]
    else:
        target["missing_usage_calls"] += 1
        target["usage_complete"] = False
        for name in ("input_tokens", "output_tokens", "total_tokens"):
            target[name] = None


class Usage:
    def __init__(self, app):
        self.app = app

    def _prices(self, db, attempt):
        if attempt["pricing_snapshot"]:
            return json.loads(attempt["pricing_snapshot"]), "dispatch_snapshot"
        # Old attempts already freeze their exact endpoint revision. Never use
        # the latest configuration to reprice historical traffic.
        row = db.execute(
            "SELECT data FROM endpoint_revisions WHERE endpoint_id=? AND revision=?",
            (attempt["endpoint_id"], attempt["endpoint_revision"]),
        ).fetchone()
        if row:
            model = dict(json.loads(row["data"]), revision=attempt["endpoint_revision"])
            return pricing_snapshot(model), "historical_catalog"
        if attempt["snapshot"]:
            snapshot = json.loads(attempt["snapshot"])
            for model in snapshot.get("models", []):
                if (
                    model.get("endpoint_id") == attempt["endpoint_id"]
                    and model.get("revision") == attempt["endpoint_revision"]
                ):
                    return pricing_snapshot(model), "historical_snapshot"
        return None, "unavailable"

    def report(self, db, actor, query):
        platform = self.app.platform
        actor = platform.public_actor(db, actor)
        if "usage" not in actor["capabilities"]:
            raise Problem(403, "USAGE_FORBIDDEN", "当前账号不能查看模型用量统计")
        workspace_id = query.get("workspace_id") or "default"
        workspace = platform.authorize_workspace(db, workspace_id, actor)
        project_ids = platform.allowed_project_ids(db, actor, workspace_id)
        project_id = query.get("project_id") or None
        if project_id:
            project = platform.authorize_project(db, project_id, actor)
            if project["workspace_id"] != workspace_id:
                raise Problem(403, "WORKSPACE_FORBIDDEN", "项目不属于所选工作空间")
            project_ids &= {project_id}
        models = {}
        current = {
            model["endpoint_id"]: model
            for model in self.app.catalog.list()
            if model["endpoint_id"] in workspace["endpoint_ids"]
        }

        def row_for(endpoint_id, revision, currency, model_identity, prices, source):
            key = endpoint_id, revision, currency
            if key not in models:
                endpoint = current.get(endpoint_id)
                configured = bool(endpoint and endpoint["revision"] == revision)
                models[key] = dict(
                    endpoint_id=endpoint_id,
                    endpoint_revision=revision,
                    model_identity=model_identity,
                    currency=currency,
                    configured=configured,
                    enabled=bool(
                        configured and endpoint["enabled"] and endpoint["allowed"]
                    ),
                    pricing_snapshot=prices,
                    pricing_source=source,
                    **counters(),
                )
            elif source == "dispatch_snapshot":
                models[key]["pricing_source"] = source
                models[key]["pricing_snapshot"] = prices
            return models[key]

        totals = {}
        if project_ids:
            placeholders = ",".join("?" for _ in project_ids)
            attempts = db.execute(
                f"""SELECT a.*,r.currency FROM attempts a JOIN runs r USING(run_id)
                    JOIN platform_run_links l USING(run_id)
                    WHERE l.project_id IN ({placeholders}) AND a.endpoint_id IS NOT NULL
                    ORDER BY a.started,a.attempt_id""",
                sorted(project_ids),
            )
            for attempt in attempts:
                prices, source = self._prices(db, attempt)
                model = row_for(
                    attempt["endpoint_id"],
                    attempt["endpoint_revision"],
                    attempt["currency"],
                    attempt["model_identity"],
                    prices,
                    source,
                )
                add_attempt(model, attempt)
                total = totals.setdefault(
                    attempt["currency"],
                    dict(currency=attempt["currency"], **counters()),
                )
                add_attempt(total, attempt)
        # Configured models with no traffic remain visible with actual zero calls.
        # Their price is configuration, not an invented charge or subscription.
        for endpoint in current.values():
            row_for(
                endpoint["endpoint_id"],
                endpoint["revision"],
                endpoint["currency"],
                endpoint["model_identity"],
                pricing_snapshot(endpoint),
                "current_configuration",
            )
        return {
            "workspace_id": workspace_id,
            "project_id": project_id,
            "basis": "dispatch_pricing_and_reported_usage",
            "models": [models[key] for key in sorted(models)],
            "totals": [totals[key] for key in sorted(totals)],
        }
