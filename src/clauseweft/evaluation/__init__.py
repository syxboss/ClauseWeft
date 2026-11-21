"""Frozen routing replays, paired execution references and blind outcome accounting."""

from copy import deepcopy
from secrets import token_hex
from statistics import median

from clauseweft.reviews._records import digest, string, strings
from clauseweft.routing.preview import RULESET, preview


def _dataset(dataset):
    if not isinstance(dataset, dict) or not isinstance(dataset.get("items"), list):
        raise ValueError("dataset 必须包含 version 与 items")
    string(dataset.get("version"), "version")
    items = dataset["items"]
    if any(not isinstance(item, dict) for item in items):
        raise ValueError("题目必须为对象")
    strings([item.get("id") for item in items], "item_ids", allow_empty=True)
    return deepcopy(items)


def freeze_dataset(dataset):
    """Validate the envelope and own a private copy before binding trusted execution inputs."""
    items = _dataset(dataset)
    return dict(version=dataset["version"], items=items)


def _sample(item):
    string(item.get("acceptance"), "acceptance")
    strings(item.get("allowed_endpoints"), "allowed_endpoints", allow_empty=True)
    snapshot = item.get("snapshot")
    if not isinstance(snapshot, dict):
        raise ValueError("缺少题目路由快照")
    return snapshot


def replay(dataset, policy=None):
    """Assess decisions alone. Missing labels or inputs remain in the denominator."""
    rows = []
    for item in _dataset(dataset):
        try:
            snapshot = _sample(item)
            canonical = preview(snapshot)
            decision = canonical if policy is None else policy(deepcopy(snapshot))
            selected = decision["selected_endpoint"]
            allowed = {c["endpoint_id"] for c in canonical.get("candidates", []) if not c["reasons"]}
            violation = selected is not None and (selected not in allowed or selected not in item["allowed_endpoints"])
            rows.append(dict(id=item["id"], decision=decision, violation=violation, missing=False))
        except (ValueError, KeyError) as error:
            rows.append(dict(id=item["id"], missing=True, reason=str(error)))
    return dict(mode="replay", version=dataset["version"], ruleset=RULESET,
                dataset_digest=digest("dataset", dataset), total=len(rows),
                violations=sum(bool(r.get("violation")) for r in rows),
                missing=sum(r["missing"] for r in rows), items=rows)


def paired_run(dataset, fixed_endpoint, start_run):
    """Allocate both arms from the same frozen inputs. start_run uses the normal runs service.

    The callback owns authorization and budget enforcement. Domain rejection can raise
    ValueError to record that arm as unstarted. Infrastructure errors propagate so the
    caller can roll back all local run registrations in its creation transaction.
    """
    items = _dataset(dataset)
    string(fixed_endpoint, "fixed_endpoint")
    result = dict(mode="paired", experiment_id=token_hex(16), dataset_digest=digest("dataset", dataset),
                  version=dataset["version"], ruleset=RULESET, total=len(items),
                  fixed_endpoint=fixed_endpoint, dataset=deepcopy(dataset), items=[])
    for item in items:
        arms = {}
        for name, pin in (("fixed", fixed_endpoint), ("automatic", None)):
            record = dict(run_id=None, blind_id=token_hex(16))
            try:
                snapshot = deepcopy(_sample(item))
                # Both policies use the same frozen allowed set and budget.
                task = snapshot["task"]
                if not isinstance(task, dict) or not isinstance(snapshot.get("models"), list):
                    raise ValueError("题目快照无效")
                task.pop("pinned_endpoint", None)
                if pin:
                    task["pinned_endpoint"] = pin
                for model in snapshot["models"]:
                    if not isinstance(model, dict):
                        raise ValueError("题目模型记录无效")
                    if model.get("endpoint_id") not in item["allowed_endpoints"]:
                        model["allowed"] = False
                decision = preview(snapshot)
                if decision["status"] != "selected":
                    raise ValueError(f"冻结策略无法派发：{decision['status']}")
                entry = dict(deepcopy(item), snapshot=snapshot)
                record["run_id"] = string(start_run(entry, pin), "run_id")
            except (ValueError, KeyError) as error:
                record["reason"] = str(error)
            arms[name] = record
        result["items"].append(dict(id=item["id"], acceptance=item.get("acceptance"), arms=arms))
    return result


def blind_items(experiment, observations):
    """Only the anonymized artifact and frozen grading criteria leave the evaluation service."""
    entries = []
    for item in experiment["items"]:
        for record in item["arms"].values():
            result = observations.get(record["run_id"], {})
            if result.get("state") == "succeeded" and result.get("artifact") is not None:
                entries.append(dict(blind_id=record["blind_id"], item_id=item["id"],
                                    acceptance=item["acceptance"], artifact=result["artifact"]))
    return sorted(entries, key=lambda entry: entry["blind_id"])


def _uint(value, name):
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} 必须为非负整数")
    return value


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else None


def _arm(experiment, name, observations, grades):
    counts = dict(passed=0, failed=0, awaiting_grade=0, pending_execution=0, unexecuted=0)
    settled = unknown = retries = timeouts = 0
    durations, judgments, reassigned = [], {}, []
    for item in experiment["items"]:
        arm = item["arms"][name]
        observed = observations.get(arm["run_id"], {})
        executed = observed.get("executed", False)
        if not isinstance(executed, bool):
            raise ValueError("executed 必须为布尔值")
        state = observed.get("state")
        attempts = observed.get("attempts", [])
        if not isinstance(attempts, list):
            raise ValueError("attempts 必须为列表")
        retries += max(0, len(attempts) - 1)
        unknown += int(executed and not attempts)
        for attempt in attempts:
            cost = attempt.get("settled_micros")
            unknown += int(cost is None or attempt.get("pending") is True)
            if cost is not None:
                settled += _uint(cost, "settled_micros")
        if not executed:
            counts["unexecuted"] += 1
            continue
        timeouts += int(state == "timed_out")
        elapsed = observed.get("elapsed_ms")
        if elapsed is not None and state in ("succeeded", "failed", "timed_out", "cancelled"):
            durations.append(_uint(elapsed, "elapsed_ms"))
        reassigned.extend(dict(item_id=item["id"], reason=reason)
                          for reason in observed.get("reassignments", []))
        if state in ("failed", "timed_out", "cancelled"):
            grade = False
        elif state == "succeeded" and observed.get("artifact") is not None:
            grade = grades.get(arm["blind_id"])
        else:
            counts["pending_execution"] += 1
            continue
        if grade is None:
            counts["awaiting_grade"] += 1
        else:
            if type(grade) is not bool:
                raise ValueError("判分必须为布尔值")
            judgments[item["id"]] = grade
            counts["passed" if grade else "failed"] += 1
    total = experiment["total"]
    passed = counts["passed"]
    return dict(**counts, total=total, executed=total-counts["unexecuted"],
                pass_rate=_ratio(passed, total), known_cost_micros=settled, pending_cost_items=unknown,
                cost_per_success_micros=None if unknown else _ratio(settled, passed),
                median_elapsed_ms=median(durations) if durations else None,
                elapsed_sample_count=len(durations), timeouts=timeouts, retries=retries,
                reassignments=reassigned), judgments


def summarize(experiment, observations, grades):
    ids = {r["blind_id"] for item in experiment["items"] for r in item["arms"].values()}
    if not isinstance(grades, dict) or any(k not in ids or type(v) is not bool for k, v in grades.items()):
        raise ValueError("判分必须关联本试验中的匿名 ID")
    fixed, fixed_grades = _arm(experiment, "fixed", observations, grades)
    automatic, automatic_grades = _arm(experiment, "automatic", observations, grades)
    paired = fixed_grades.keys() & automatic_grades.keys()
    return dict(mode="paired", total=experiment["total"], fixed=fixed, automatic=automatic,
                paired_count=len(paired), paired_coverage=_ratio(len(paired), experiment["total"]),
                automatic_wins=sum(automatic_grades[k] and not fixed_grades[k] for k in paired),
                fixed_wins=sum(fixed_grades[k] and not automatic_grades[k] for k in paired),
                ties=sum(fixed_grades[k] == automatic_grades[k] for k in paired))
