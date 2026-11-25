from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from covscreen.jobs import ArtifactRegistry, sha256_file, utc_now


STAGES = (
    ("af3", "alphafold3-mpae", "stage4_top_candidates.parquet", "stage4_selection_manifest.json"),
    ("covdock", "schrodinger-covdock-lo", "stage5_gated_candidates.parquet", "covdock_lo_registered_manifest.json"),
    ("evaluation", "ligand-first-evaluation", "ligand_first_evaluated_candidates.parquet", "ligand_first_evaluation_manifest.json"),
    ("pareto", "ligand-first-pareto", "stage6_pareto_all_candidates.parquet", "stage6_pareto_manifest.json"),
)


def _identity(frame: pd.DataFrame) -> list[tuple[int, str]]:
    required = {"input_sdf_index", "compound_id"}
    if not required.issubset(frame.columns):
        raise ValueError(f"candidate table is missing identity columns: {sorted(required - set(frame.columns))}")
    index = pd.to_numeric(frame["input_sdf_index"], errors="raise").astype("int64")
    compounds = frame["compound_id"].astype("string")
    if index.duplicated().any() or compounds.isna().any() or compounds.str.strip().eq("").any():
        raise ValueError("candidate identity denominator is empty, duplicated, or missing")
    return sorted(zip(index.tolist(), compounds.astype(str).tolist()))


def _resolve(root: Path, raw: str) -> Path:
    path = Path(raw)
    resolved = path.resolve() if path.is_absolute() else (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"manifest output escapes the registry root: {resolved}") from exc
    return resolved


def _outputs_valid(root: Path, manifest: dict[str, Any]) -> tuple[bool, list[str]]:
    failures: list[str] = []
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or not outputs:
        return False, ["manifest_outputs_missing"]
    for name, record in outputs.items():
        if not isinstance(record, dict) or not record.get("path") or not record.get("sha256"):
            failures.append(f"{name}:incomplete_output_record")
            continue
        try:
            path = _resolve(root, str(record["path"]))
        except ValueError:
            failures.append(f"{name}:path_outside_registry")
            continue
        if not path.is_file():
            failures.append(f"{name}:file_missing")
        elif sha256_file(path) != record["sha256"]:
            failures.append(f"{name}:sha256_mismatch")
    return not failures, failures


def _scientific_upstream(registry: ArtifactRegistry, run_id: str) -> str | None:
    spec = registry.load_spec(run_id)
    explicit = spec.parameters.get("upstream_run_id")
    return str(explicit) if explicit else spec.parent_run_id


def _finite(series: pd.Series) -> pd.Series:
    return pd.to_numeric(series, errors="coerce").map(np.isfinite)


def _status_score_consistent(
    frame: pd.DataFrame, status_column: str, score_columns: tuple[str, ...]
) -> tuple[bool, str]:
    score = next((column for column in score_columns if column in frame.columns), None)
    if status_column not in frame.columns or score is None:
        return False, f"missing {status_column} or one of {score_columns}"
    status = frame[status_column].map(
        lambda value: value is True or str(value).strip().upper() in {"TRUE", "SUCCESS", "1"}
    )
    finite = _finite(frame[score])
    invalid = (status & ~finite) | (~status & finite)
    return not invalid.any(), f"score={score}; inconsistent_rows={int(invalid.sum())}"


def audit_ligand_p0_closure(
    root: Path,
    run_ids: dict[str, str],
    *,
    expected_count: int = 3,
    delivery_table: Path | None = None,
    platform_receipt: Path | None = None,
    browser_verification: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    registry = ArtifactRegistry(root, create=False)
    gates: dict[str, dict[str, Any]] = {}

    def gate(name: str, passed: bool, detail: Any) -> None:
        gates[name] = {"passed": bool(passed), "detail": detail}

    frames: dict[str, pd.DataFrame] = {}
    manifests: dict[str, dict[str, Any]] = {}
    results = {}
    for stage, engine, table_name, manifest_name in STAGES:
        run_id = run_ids.get(stage)
        if not run_id:
            gate(f"{stage}.run_registered", False, "run_id missing")
            continue
        try:
            result = registry.load_result(run_id)
            spec = registry.load_spec(run_id)
        except (FileNotFoundError, ValueError) as exc:
            gate(f"{stage}.run_registered", False, f"{type(exc).__name__}:{exc}")
            continue
        results[stage] = result
        gate(f"{stage}.engine", result.engine == engine == spec.engine, {"result": result.engine, "spec": spec.engine, "expected": engine})
        gate(f"{stage}.completed", result.state == "COMPLETED" and result.return_code == 0, {"state": result.state, "return_code": result.return_code})
        artifact_dir = registry.job_dir(run_id) / "artifacts"
        table_path, manifest_path = artifact_dir / table_name, artifact_dir / manifest_name
        if table_path.is_file():
            frames[stage] = pd.read_parquet(table_path)
        gate(f"{stage}.table", table_path.is_file(), str(table_path.relative_to(root)))
        if manifest_path.is_file():
            manifests[stage] = json.loads(manifest_path.read_text())
            gate(
                f"{stage}.manifest_completed",
                str(manifests[stage].get("status", "")).strip().lower() == "completed",
                manifests[stage].get("status"),
            )
            valid, failures = _outputs_valid(root, manifests[stage])
            gate(f"{stage}.manifest_outputs", valid, failures)
            quality = manifests[stage].get("quality_gates", manifests[stage].get("gate_quality_gates", {}))
            gate(f"{stage}.quality_gates", bool(quality) and all(value is True for value in quality.values()), quality)
            if stage == "pareto":
                named_outputs = set(manifests[stage].get("outputs", {}))
                required_reports = {
                    "stage6_selection_report.md",
                    "ligand_first_traceability_report.json",
                    "ligand_first_traceability_report.md",
                }
                gate(
                    "pareto.reports",
                    required_reports <= named_outputs,
                    {"missing": sorted(required_reports - named_outputs)},
                )
        else:
            gate(f"{stage}.manifest_completed", False, f"missing {manifest_name}")
            gate(f"{stage}.manifest_outputs", False, f"missing {manifest_name}")
            gate(f"{stage}.quality_gates", False, f"missing {manifest_name}")

    if all(stage in frames for stage, *_ in STAGES):
        identities = {stage: _identity(frame) for stage, frame in frames.items()}
        reference = identities["af3"]
        gate("identity.expected_count", len(reference) == expected_count, {"expected": expected_count, "actual": len(reference)})
        gate("identity.preserved", all(value == reference for value in identities.values()), identities)
        required_identity = {
            "target_id", "site_id", "parent_id", "standardized_smiles", "warhead_class",
            "electrophilic_atom", "nucleophilic_atom",
        }
        missing = sorted(required_identity - set(frames["af3"].columns))
        complete = not missing and not frames["af3"][sorted(required_identity)].isna().any().any()
        gate("identity.scientific_fields", complete, {"missing_columns": missing})
        af3_ok, af3_detail = _status_score_consistent(frames["af3"], "af3_success", ("af3_mpae", "mpae"))
        cov_ok, cov_detail = _status_score_consistent(frames["covdock"], "covdock_success", ("covdock_score",))
        gate("scores.af3_missing_not_zero", af3_ok, af3_detail)
        gate("scores.covdock_missing_not_zero", cov_ok, cov_detail)
        evaluation = frames["evaluation"]
        gate("evaluation.independent_gate", "independent_gate" in evaluation and evaluation["independent_gate"].isin(["PASS", "WARN", "FAIL"]).all(), evaluation.get("independent_gate", pd.Series(dtype=str)).value_counts().to_dict())
        pareto = frames["pareto"]
        needed = {"pareto_eligible", "pareto_exclusion_reason", "pareto_front"}
        missing_pareto = sorted(needed - set(pareto.columns))
        pareto_ok = not missing_pareto
        if pareto_ok:
            eligible = pareto["pareto_eligible"].fillna(False).astype(bool)
            pareto_ok = bool(
                _finite(pareto.loc[eligible, "pareto_front"]).all()
                and pareto.loc[~eligible, "pareto_exclusion_reason"].fillna("").astype(str).str.strip().ne("").all()
            )
        gate("pareto.ranked_or_explained", pareto_ok, {"missing_columns": missing_pareto})
        final_path = registry.job_dir(run_ids["pareto"]) / "artifacts" / "stage6_final_candidates.parquet"
        final_ok = final_path.is_file()
        detail: dict[str, Any] = {"path": str(final_path.relative_to(root))}
        if final_ok:
            final = pd.read_parquet(final_path)
            final_identity = set(_identity(final))
            all_identity = set(_identity(pareto))
            ranks = (
                pd.to_numeric(final["final_rank"], errors="coerce")
                if "final_rank" in final
                else pd.Series(np.nan, index=final.index)
            )
            final_ok = bool(
                len(final) > 0
                and final_identity <= all_identity
                and ranks.notna().all()
                and sorted(ranks.astype(int).tolist()) == list(range(1, len(final) + 1))
            )
            detail.update(selected_count=len(final), ranks=ranks.tolist())
        gate("pareto.final_ranking", final_ok, detail)

    lineage = {
        "covdock": run_ids.get("af3"),
        "evaluation": run_ids.get("covdock"),
        "pareto": run_ids.get("evaluation"),
    }
    for stage, expected_parent in lineage.items():
        run_id = run_ids.get(stage)
        if run_id and stage in results:
            observed = _scientific_upstream(registry, run_id)
            gate(f"lineage.{stage}", observed == expected_parent, {"expected": expected_parent, "observed": observed})

    delivery_ready = all(path is not None and path.is_file() for path in (delivery_table, platform_receipt, browser_verification))
    gate("workbench.delivery_evidence_present", delivery_ready, {
        "table": str(delivery_table) if delivery_table else None,
        "receipt": str(platform_receipt) if platform_receipt else None,
        "browser": str(browser_verification) if browser_verification else None,
    })
    if delivery_ready:
        delivery = pd.read_csv(delivery_table, keep_default_na=False)
        receipt = json.loads(platform_receipt.read_text())
        browser = json.loads(browser_verification.read_text())
        delivery_identity = _identity(delivery)
        expected_identity = _identity(frames["af3"]) if "af3" in frames else []
        gate("workbench.identity", delivery_identity == expected_identity, delivery_identity)
        structure_columns = {
            "af3_artifact_id", "af3_artifact_version", "af3_artifact_sha256",
            "covdock_artifact_id", "covdock_artifact_version", "covdock_artifact_sha256",
        }
        structure_ok = structure_columns.issubset(delivery.columns)
        if structure_ok:
            for column in structure_columns:
                values = delivery[column].astype(str).str.strip()
                structure_ok = structure_ok and values.ne("").all()
                if column.endswith("sha256"):
                    structure_ok = structure_ok and values.str.fullmatch(r"[a-f0-9]{64}").all()
        gate("workbench.structure_references", structure_ok, sorted(structure_columns - set(delivery.columns)))
        raw_artifacts = receipt.get("artifacts", [])
        artifact_items = list(raw_artifacts.values()) if isinstance(raw_artifacts, dict) else raw_artifacts
        if not isinstance(artifact_items, list):
            artifact_items = []
        artifacts = {
            (item.get("id"), item.get("current", {}).get("id"), item.get("current", {}).get("sha256"))
            for item in artifact_items if isinstance(item, dict)
        }
        referenced = {
            (row.af3_artifact_id, row.af3_artifact_version, row.af3_artifact_sha256)
            for row in delivery.itertuples()
        } | {
            (row.covdock_artifact_id, row.covdock_artifact_version, row.covdock_artifact_sha256)
            for row in delivery.itertuples()
        }
        gate("workbench.receipt_matches_structures", bool(referenced) and referenced <= artifacts, {"referenced": len(referenced), "registered": len(artifacts)})
        report_columns = {
            "selection_report_artifact_id", "selection_report_artifact_version",
            "selection_report_artifact_sha256", "traceability_report_artifact_id",
            "traceability_report_artifact_version", "traceability_report_artifact_sha256",
        }
        reports_ok = report_columns.issubset(delivery.columns)
        report_references: set[tuple[str, str, str]] = set()
        if reports_ok:
            for prefix in ("selection_report", "traceability_report"):
                values = delivery[[
                    f"{prefix}_artifact_id", f"{prefix}_artifact_version",
                    f"{prefix}_artifact_sha256",
                ]].astype(str).apply(lambda column: column.str.strip())
                reports_ok = bool(
                    reports_ok
                    and values.ne("").all().all()
                    and values[f"{prefix}_artifact_sha256"].str.fullmatch(r"[a-f0-9]{64}").all()
                )
                report_references.update(map(tuple, values.drop_duplicates().itertuples(index=False, name=None)))
        reports_ok = bool(reports_ok and report_references and report_references <= artifacts)
        gate(
            "workbench.report_references",
            reports_ok,
            {
                "missing_columns": sorted(report_columns - set(delivery.columns)),
                "referenced": len(report_references),
                "registered": len(artifacts),
            },
        )
        required_browser_evidence = (
            "production",
            "single_task_page",
            "stage_progress_visible",
            "structures_visible",
            "scores_visible",
            "gate_reasons_visible",
            "ranking_rationale_visible",
            "report_visible",
        )
        missing_browser_evidence = [
            key for key in required_browser_evidence if browser.get(key) is not True
        ]
        browser_ok = (
            not missing_browser_evidence
            and int(browser.get("candidate_count", -1)) == expected_count
        )
        gate(
            "workbench.browser_verified",
            browser_ok,
            {
                **browser,
                "missing_required_evidence": missing_browser_evidence,
                "expected_candidate_count": expected_count,
            },
        )

    passed = bool(gates) and all(record["passed"] for record in gates.values())
    return {
        "schema_version": "1.0",
        "generated_at": utc_now(),
        "status": "PASS" if passed else "INCOMPLETE_OR_FAILED",
        "expected_candidate_count": expected_count,
        "run_ids": run_ids,
        "gates": gates,
        "failed_gates": [name for name, record in gates.items() if not record["passed"]],
    }
