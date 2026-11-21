"""Offline audit verification with no server or model credentials."""

import argparse
import hashlib
import json
from pathlib import Path
import sys

from . import FORMAT, verify_entries
from clauseweft.gateway.server import reject_constant, unique_object
from clauseweft.storage import canonical


def verify_file(path):
    try:
        bundle = json.loads(
            Path(path).read_text(encoding="utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_constant,
        )
        if bundle["format"] != FORMAT:
            raise ValueError("审计导出格式无效")
        result = verify_entries(bundle["entries"], bundle["anchor"])
        if any(
            key in bundle for key in ("evidence", "delivery_jobs", "business_records")
        ):
            manifest = next(
                (
                    entry
                    for entry in bundle["entries"]
                    if entry["seq"] == bundle.get("manifest_seq")
                    and entry["action"] == "audit.export_prepared"
                ),
                None,
            )
            if manifest is None:
                result["errors"].append({"code": "export_manifest_missing"})
            else:
                if not manifest["after"].get("database_verification", {}).get("valid"):
                    result["errors"].append(
                        {"code": "source_database_verification_failed"}
                    )
                for key in ("evidence", "delivery_jobs", "business_records"):
                    digest = hashlib.sha256(canonical(bundle[key]).encode()).hexdigest()
                    if manifest["after"].get(key + "_sha256") != digest:
                        result["errors"].append(
                            {"code": "export_content_mismatch", "section": key}
                        )
        for evidence in bundle.get("evidence", []):
            if evidence.get("availability") != "recorded":
                result["errors"].append(
                    {"code": "evidence_missing", "sha256": evidence.get("sha256")}
                )
            elif (
                hashlib.sha256(canonical(evidence["view"]).encode()).hexdigest()
                != evidence["view_sha256"]
            ):
                result["errors"].append(
                    {"code": "evidence_view_mismatch", "sha256": evidence.get("sha256")}
                )
        result["valid"] = not result["errors"]
        result["status"] = "verified" if result["valid"] else "failed"
        result["anchor_source"] = "exported_bundle"
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as error:
        print("审计校验失败：" + type(error).__name__, file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True)
    raise SystemExit(verify_file(parser.parse_args().input))
