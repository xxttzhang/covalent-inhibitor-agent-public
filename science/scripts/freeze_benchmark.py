from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TOOL_VERSION = "1.0.0"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object: {path}")
    return value


def project_root_from_spec(spec_path: Path) -> Path:
    resolved = spec_path.resolve()
    if len(resolved.parents) < 3:
        raise ValueError("Spec must be stored below benchmark/specs/")
    return resolved.parents[2]


def audit_library(root: Path, library_spec: dict[str, Any]) -> dict[str, Any]:
    path = root / library_spec["path"]
    with path.open(newline="", encoding="utf-8-sig") as handle:
        rows = list(csv.DictReader(handle))
    identity_columns = list(library_spec["identity_columns"])
    duplicates: dict[str, int] = {}
    for column in identity_columns:
        values = [row.get(column, "") for row in rows]
        duplicates[column] = len(values) - len(set(values))
    label_column = library_spec["label_column"]
    labels: dict[str, int] = {}
    for row in rows:
        label = row.get(label_column, "")
        labels[label] = labels.get(label, 0) + 1
    errors: list[str] = []
    if len(rows) != library_spec["expected_rows"]:
        errors.append(f"library rows: expected {library_spec['expected_rows']}, got {len(rows)}")
    for label, expected in library_spec["expected_labels"].items():
        if labels.get(label, 0) != expected:
            errors.append(f"label {label}: expected {expected}, got {labels.get(label, 0)}")
    for column, count in duplicates.items():
        if count:
            errors.append(f"duplicate {column}: {count}")
    return {
        "path": library_spec["path"],
        "rows": len(rows),
        "labels": labels,
        "duplicate_identity_values": duplicates,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }


def audit_engines(root: Path, spec: dict[str, Any]) -> list[dict[str, Any]]:
    reports: list[dict[str, Any]] = []
    expected_input = spec["library"]["expected_rows"]
    for engine in spec["engines"]:
        metrics = load_json(root / engine["metrics_path"])
        checks = {
            "engine": metrics.get("engine") == engine["id"],
            "n_input": metrics.get("n_input") == expected_input,
            "n_scoring_success": metrics.get("n_scoring_success") == engine["expected_success"],
            "n_failed": metrics.get("n_failed") == engine["expected_failure"],
            "score_direction": metrics.get("score_direction") == engine["score_direction"],
            "conservative_metrics_present": isinstance(metrics.get("conservative"), dict),
        }
        reports.append(
            {
                "engine": engine["id"],
                "metrics_path": engine["metrics_path"],
                "result_path": engine["result_path"],
                "checks": checks,
                "status": "PASS" if all(checks.values()) else "FAIL",
            }
        )
    return reports


def collect_artifacts(root: Path, spec: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    records: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for item in spec["artifacts"]:
        relative = item["path"]
        if relative in seen:
            errors.append(f"duplicate artifact declaration: {relative}")
            continue
        seen.add(relative)
        path = root / relative
        if not path.is_file():
            errors.append(f"missing artifact: {relative}")
            continue
        records.append(
            {
                "path": relative,
                "role": item["role"],
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
        )
    return records, errors


def build_manifest(spec_path: Path) -> tuple[dict[str, Any], list[str]]:
    spec = load_json(spec_path)
    root = project_root_from_spec(spec_path)
    artifacts, errors = collect_artifacts(root, spec)
    library_report = audit_library(root, spec["library"])
    engine_reports = audit_engines(root, spec)
    errors.extend(library_report["errors"])
    errors.extend(
        f"engine semantic check failed: {report['engine']}"
        for report in engine_reports
        if report["status"] != "PASS"
    )
    spec_digest = sha256_file(spec_path)
    freeze_id = f"{spec['benchmark_id']}-v{spec['version']}-{spec_digest[:12]}"
    manifest = {
        "schema_version": "1.0",
        "freeze_id": freeze_id,
        "benchmark_id": spec["benchmark_id"],
        "benchmark_version": spec["version"],
        "frozen": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "tool": {"name": "freeze_benchmark.py", "version": TOOL_VERSION},
        "spec": {
            "path": str(spec_path.resolve().relative_to(root)),
            "sha256": spec_digest,
        },
        "failure_policy": spec["failure_policy"],
        "library_audit": library_report,
        "engine_audits": engine_reports,
        "artifacts": artifacts,
        "status": "PASS" if not errors else "FAIL",
        "errors": errors,
    }
    return manifest, errors


def verify_manifest(manifest_path: Path) -> tuple[dict[str, Any], list[str]]:
    manifest = load_json(manifest_path)
    root = manifest_path.resolve().parents[3]
    errors: list[str] = []
    spec_path = root / manifest["spec"]["path"]
    if not spec_path.is_file():
        errors.append(f"missing spec: {manifest['spec']['path']}")
        spec = None
    elif sha256_file(spec_path) != manifest["spec"]["sha256"]:
        errors.append(f"spec checksum mismatch: {manifest['spec']['path']}")
        spec = None
    else:
        spec = load_json(spec_path)
    checked: list[dict[str, Any]] = []
    for artifact in manifest.get("artifacts", []):
        path = root / artifact["path"]
        exists = path.is_file()
        current_hash = sha256_file(path) if exists else None
        current_size = path.stat().st_size if exists else None
        ok = (
            exists
            and current_hash == artifact["sha256"]
            and current_size == artifact["size_bytes"]
        )
        checked.append({"path": artifact["path"], "status": "PASS" if ok else "FAIL"})
        if not ok:
            errors.append(f"artifact mismatch: {artifact['path']}")
    library_report: dict[str, Any] | None = None
    engine_reports: list[dict[str, Any]] = []
    if spec is not None:
        library_report = audit_library(root, spec["library"])
        engine_reports = audit_engines(root, spec)
        errors.extend(library_report["errors"])
        errors.extend(
            f"engine semantic check failed: {report['engine']}"
            for report in engine_reports
            if report["status"] != "PASS"
        )
    report = {
        "manifest": str(manifest_path),
        "freeze_id": manifest.get("freeze_id"),
        "frozen": manifest.get("frozen") is True,
        "artifact_checks": checked,
        "library_audit": library_report,
        "engine_audits": engine_reports,
        "status": "PASS" if not errors and manifest.get("frozen") is True else "FAIL",
        "errors": errors,
    }
    return report, errors


def write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Freeze or verify an immutable benchmark manifest.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    freeze = subparsers.add_parser("freeze")
    freeze.add_argument("--spec", type=Path, required=True)
    freeze.add_argument("--output", type=Path, required=True)
    verify = subparsers.add_parser("verify")
    verify.add_argument("--manifest", type=Path, required=True)
    verify.add_argument("--output", type=Path)
    args = parser.parse_args()

    if args.command == "freeze":
        if args.output.exists():
            print(f"Refusing to overwrite frozen manifest: {args.output}", file=sys.stderr)
            return 2
        manifest, errors = build_manifest(args.spec)
        if errors:
            print(json.dumps(manifest, indent=2, ensure_ascii=False))
            return 1
        write_json(args.output, manifest)
        print(json.dumps(manifest, indent=2, ensure_ascii=False))
        return 0

    report, errors = verify_manifest(args.manifest)
    if args.output:
        write_json(args.output, report)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0 if not errors and report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
