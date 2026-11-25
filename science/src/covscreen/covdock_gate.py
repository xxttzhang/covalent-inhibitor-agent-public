from __future__ import annotations

import json
import os
from numbers import Real
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .jobs import sha256_file, utc_now


ALGORITHM_VERSION = "covdock-lo-pose-gate-v2.1"
STRICT_ALGORITHM_VERSION = "covdock-lo-pose-gate-v3.0-provisional"
LIGAND_ALGORITHM_VERSION = "ligand-covdock-lo-physical-gate-v4.0-provisional"
LIBRARY_ALGORITHM_VERSION = "library-covdock-lo-physical-gate-v1.0-provisional"
OPTIONAL_CONSENSUS_VERSIONS = {LIGAND_ALGORITHM_VERSION, LIBRARY_ALGORITHM_VERSION}
# Terminal batch outcomes eligible for independent readback, not acceptance.
REVIEWABLE_BATCH_OUTCOMES = frozenset({('COMPLETED', 0), ('FAILED', 2), ('FAILED', 4)})
STRAIN_COLUMNS = (
    "r_strain_Local_Strain_Energy",
    "r_strain_Strain_Energy",
    "r_strain_Solvated_Local_Strain_Energy",
    "r_strain_Solvated_Strain_Energy",
)
POSE_CONSISTENCY_COLUMNS = (
    "af3_covdock_ligand_rmsd_angstrom",
    "protein_alignment_rmsd_angstrom",
    "protein_alignment_atom_count",
    "ligand_mapped_heavy_atom_count",
    "pose_consistency_status",
    "pose_consistency_failure_reason",
    "pose_topology_status",
    "topology_heavy_atom_count",
    "topology_heavy_bond_count",
    "topology_covalent_partner",
    "ligand_mapping_policy",
    "af3_topology_source",
    "af3_topology_source_sha256",
)


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _boolean(value: object) -> bool | None:
    if value is None or (isinstance(value, float) and np.isnan(value)):
        return None
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _first_finite(frame: pd.DataFrame, columns: tuple[str, ...]) -> pd.Series:
    available = [column for column in columns if column in frame]
    if not available:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    numeric = pd.concat(
        [pd.to_numeric(frame[column], errors="coerce") for column in available],
        axis=1,
    )
    return numeric.where(np.isfinite(numeric)).bfill(axis=1).iloc[:, 0]


def _identity_indices(values: pd.Series) -> pd.Series:
    numeric = pd.to_numeric(values, errors="raise")
    if any(isinstance(original, (bool, np.bool_)) or not np.isfinite(value) or value < 0 or value % 1 != 0 or
           int(value) > np.iinfo(np.int64).max for original, value in zip(values, numeric)):
        raise ValueError("CovDock input identities require finite nonnegative integer indices")
    return numeric.astype("int64")


def merge_pose_consistency(
    scores: pd.DataFrame, pose_consistency: pd.DataFrame
) -> pd.DataFrame:
    """Merge an identity-audited AF3/CovDock local-alignment result table."""

    required = {"input_sdf_index", "af3_covdock_ligand_rmsd_angstrom"}
    missing = sorted(required - set(pose_consistency))
    if missing:
        raise ValueError(f"pose-consistency table is missing columns: {missing}")
    left = scores.copy()
    right = pose_consistency.copy()
    for frame, label in ((left, "CovDock score"), (right, "pose-consistency")):
        frame["input_sdf_index"] = _identity_indices(frame["input_sdf_index"])
        if frame["input_sdf_index"].duplicated().any():
            raise ValueError(f"{label} table has duplicate input_sdf_index values")
    left_ids = set(left["input_sdf_index"])
    right_ids = set(right["input_sdf_index"])
    if left_ids != right_ids:
        raise ValueError(
            "pose-consistency identity set differs from CovDock scores: "
            f"missing={sorted(left_ids - right_ids)[:10]}, "
            f"extra={sorted(right_ids - left_ids)[:10]}"
        )
    available = [column for column in POSE_CONSISTENCY_COLUMNS if column in right]
    left = left.drop(columns=[column for column in available if column in left])
    return left.merge(
        right[["input_sdf_index", *available]],
        on="input_sdf_index",
        how="left",
        validate="one_to_one",
    )


def assess_covdock_candidates(
    scores: pd.DataFrame,
    *,
    max_local_strain: float = 10.0,
    hard_max_strain: float = 20.0,
    min_covalent_bond_length: float = 1.5,
    max_covalent_bond_length: float = 2.2,
    max_pose_rmsd: float = 3.0,
    algorithm_version: str = ALGORITHM_VERSION,
) -> pd.DataFrame:
    """Recompute row-level gates without writing files or changing source data.

    Input columns must come from separately verified structural inspections.
    This numeric assessment alone does not authenticate those measurements.
    The provisional v3 policy must be explicitly selected. It keeps the same
    thresholds, accepts only the exact local-strain field, and separates engine
    execution from physical evidence. The default preserves frozen v2.1 replay.
    """
    if algorithm_version not in {ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION, *OPTIONAL_CONSENSUS_VERSIONS}:
        raise ValueError("Unknown CovDock gate algorithm version")
    strict = algorithm_version in {STRICT_ALGORITHM_VERSION, *OPTIONAL_CONSENSUS_VERSIONS}
    optional_consensus = algorithm_version in OPTIONAL_CONSENSUS_VERSIONS
    required = {"input_sdf_index", "compound_id", "engine_status", "raw_score"}
    missing = sorted(required - set(scores.columns))
    if missing:
        raise ValueError(f"CovDock-LO score table is missing columns: {missing}")
    parameters = (max_local_strain, hard_max_strain, min_covalent_bond_length,
                  max_covalent_bond_length, max_pose_rmsd)
    if any(not isinstance(value, Real) or isinstance(value, (bool, np.bool_)) or not np.isfinite(value) or value < 0 for value in parameters):
        raise ValueError("CovDock quality thresholds must be finite nonnegative numbers")
    if min_covalent_bond_length <= 0 or max_covalent_bond_length < min_covalent_bond_length:
        raise ValueError("CovDock bond-length range must be positive and ordered")
    if hard_max_strain < max_local_strain:
        raise ValueError("hard_max_strain cannot be smaller than max_local_strain")
    scores = scores.copy()
    scores["input_sdf_index"] = _identity_indices(scores["input_sdf_index"])
    if scores["input_sdf_index"].duplicated().any():
        raise ValueError("CovDock-LO score table has duplicate input_sdf_index values")
    scores["covdock_score"] = pd.to_numeric(scores["raw_score"], errors="coerce")
    scores["covdock_success"] = scores["engine_status"].eq("SUCCESS") & np.isfinite(scores["covdock_score"])
    scores["local_strain_energy"] = _first_finite(scores, STRAIN_COLUMNS[:1] if strict else STRAIN_COLUMNS)
    if strict:
        original = scores.get(STRAIN_COLUMNS[0], pd.Series(np.nan, index=scores.index))
        scores["strain_measurement_status"] = np.where(
            scores["local_strain_energy"].notna(), "VALID",
            np.where(original.isna(), "MISSING", "INVALID_OR_NONFINITE"),
        )
        scores["strain_source_field"] = STRAIN_COLUMNS[0]
        scores["strain_definition"] = "vendor_local_strain_unmodified"
        scores["strain_unit"] = "kcal/mol"
        scores["evaluation_policy"] = algorithm_version
        scores["thresholds_calibrated"] = False
        scores["execution_status"] = np.where(
            scores["engine_status"].ne("SUCCESS"), "ENGINE_NOT_SUCCESSFUL",
            np.where(scores["covdock_success"], "SUCCESS", "SCORE_MISSING_OR_INVALID"),
        )
    scores["covalent_link_verified"] = [
        _boolean(value)
        for value in scores.get(
            "covalent_link_present", pd.Series(None, index=scores.index)
        )
    ]
    scores["severe_clash_verified"] = [
        _boolean(value)
        for value in scores.get("severe_clash", pd.Series(None, index=scores.index))
    ]
    scores["covalent_bond_length_angstrom"] = pd.to_numeric(
        scores.get("covalent_bond_length_angstrom", np.nan), errors="coerce"
    )
    scores["af3_covdock_ligand_rmsd_angstrom"] = pd.to_numeric(
        scores.get("af3_covdock_ligand_rmsd_angstrom", np.nan), errors="coerce"
    )

    grades = []
    detailed_grades = []
    physical_validity = []
    cross_method_consensus = []
    reasons = []
    completeness = []
    for row in scores.itertuples(index=False):
        failures = []
        execution_failures = []
        physical_warnings = []
        consensus_warnings = []
        missing_evidence = []
        if not bool(row.covdock_success):
            (execution_failures if strict else failures).append("covdock_failed_or_score_missing")
        if row.covalent_link_verified is False:
            failures.append("specified_covalent_link_missing")
        if row.covalent_link_verified is None:
            missing_evidence.append("covalent_link_not_verified")
        bond_length = float(row.covalent_bond_length_angstrom)
        if np.isfinite(bond_length) and not (
            min_covalent_bond_length <= bond_length <= max_covalent_bond_length
        ):
            failures.append("covalent_bond_length_outside_gate")
        if not np.isfinite(bond_length):
            missing_evidence.append("covalent_bond_length_missing")
        strain = float(row.local_strain_energy)
        if np.isfinite(strain) and strain > hard_max_strain:
            failures.append("ligand_strain_above_hard_gate")
        elif np.isfinite(strain) and strain > max_local_strain:
            physical_warnings.append("ligand_strain_above_preferred_gate")
        elif not np.isfinite(strain):
            missing_evidence.append("ligand_strain_missing")
        if row.severe_clash_verified is True:
            failures.append("severe_nonbonded_clash")
        if row.severe_clash_verified is None:
            missing_evidence.append("clash_check_missing")
        physical_complete = not missing_evidence
        rmsd = float(row.af3_covdock_ligand_rmsd_angstrom)
        if np.isfinite(rmsd) and rmsd >= 0:
            consensus = "HIGH" if rmsd <= max_pose_rmsd else "LOW"
            if consensus == "LOW":
                consensus_warnings.append("af3_covdock_pose_disagreement")
        else:
            consensus = "NA"
            if not optional_consensus:
                missing_evidence.append("af3_covdock_pose_consistency_invalid" if rmsd < 0 or np.isinf(rmsd)
                                        else "af3_covdock_pose_consistency_not_evaluated")

        physical = "FAIL" if failures else "UNKNOWN" if not physical_complete else "WARN" if physical_warnings else "PASS"
        if failures or execution_failures:
            detailed_grade = "C"
        elif missing_evidence:
            detailed_grade = "B2"
        elif physical == "PASS" and (optional_consensus or consensus == "HIGH"):
            detailed_grade = "A"
        else:
            detailed_grade = "B1"
        # Keep the historical A/B/C field for compatibility. B1 and B2 expose
        # whether the evidence is contradictory or merely incomplete.
        grade = "B" if detailed_grade in {"B1", "B2"} else detailed_grade
        grades.append(grade)
        detailed_grades.append(detailed_grade)
        physical_validity.append(physical)
        completeness.append(physical_complete)
        cross_method_consensus.append(consensus)
        reasons.append(
            ";".join(
                [
                    *failures,
                    *execution_failures,
                    *physical_warnings,
                    *consensus_warnings,
                    *missing_evidence,
                ]
            )
            or ("physical_checks_passed_optional_consensus" if optional_consensus else "physical_checks_passed_and_high_pose_consensus")
        )
    scores["covdock_gate"] = grades
    scores["covdock_gate_detail"] = detailed_grades
    scores["physical_validity"] = physical_validity
    scores["physical_evidence_complete"] = completeness
    scores["cross_method_consensus"] = cross_method_consensus
    scores["covdock_gate_reason"] = reasons
    if strict:
        # Review readiness is not MD readiness, resource approval or activity.
        # Missing required evidence remains blocked even though it is not FAIL.
        scores["review_eligible"] = (
            scores["covdock_success"]
            & scores["physical_validity"].isin(["PASS", "WARN"])
            & (True if optional_consensus else scores["cross_method_consensus"].isin(["HIGH", "LOW"]))
        )
    return scores


def gate_covdock_candidates(
    scores: pd.DataFrame,
    output_dir: Path,
    *,
    final_n: int = 30,
    max_local_strain: float = 10.0,
    hard_max_strain: float = 20.0,
    min_covalent_bond_length: float = 1.5,
    max_covalent_bond_length: float = 2.2,
    max_pose_rmsd: float = 3.0,
    min_success_rate: float = 0.80,
    algorithm_version: str = ALGORITHM_VERSION,
) -> dict[str, Any]:
    if type(final_n) is not int or final_n < 1:
        raise ValueError("final_n must be a positive integer")
    if (not isinstance(min_success_rate, Real) or isinstance(min_success_rate, (bool, np.bool_)) or not np.isfinite(min_success_rate) or
            not 0 <= min_success_rate <= 1):
        raise ValueError("CovDock minimum success rate must be finite and in [0, 1]")
    scores = assess_covdock_candidates(scores, max_local_strain=max_local_strain,
        hard_max_strain=hard_max_strain, min_covalent_bond_length=min_covalent_bond_length,
        max_covalent_bond_length=max_covalent_bond_length, max_pose_rmsd=max_pose_rmsd,
        algorithm_version=algorithm_version)

    af3_rank_column = next(
        (
            column
            for column in ("af3_mpae_rank", "af3_rank", "stage4_rank")
            if column in scores
        ),
        None,
    )
    if af3_rank_column is None or algorithm_version in OPTIONAL_CONSENSUS_VERSIONS:
        scores["_af3_rank"] = np.inf
    else:
        scores["_af3_rank"] = pd.to_numeric(
            scores[af3_rank_column], errors="coerce"
        ).fillna(np.inf)
    scores["_gate_rank"] = scores["covdock_gate_detail"].map(
        {"A": 0, "B1": 1, "B2": 2, "C": 3}
    )
    scores = scores.sort_values(
        ["_gate_rank", "_af3_rank", "input_sdf_index"], kind="stable"
    ).reset_index(drop=True)
    eligible = scores.loc[scores["covdock_gate"].isin(["A", "B"])].copy()
    if algorithm_version in {STRICT_ALGORITHM_VERSION, *OPTIONAL_CONSENSUS_VERSIONS}:
        eligible = eligible.loc[eligible["review_eligible"]].copy()
    final = eligible.head(final_n).copy()
    final["final_rank"] = np.arange(1, len(final) + 1, dtype=int)

    output_dir.mkdir(parents=True, exist_ok=True)
    gated_parquet = output_dir / "stage5_gated_candidates.parquet"
    gated_csv = output_dir / "stage5_gated_candidates.csv"
    final_parquet = output_dir / "final_candidates.parquet"
    final_csv = output_dir / "final_candidates.csv"
    failures_parquet = output_dir / "stage5_failures.parquet"
    failures_csv = output_dir / "stage5_failures.csv"
    scores.drop(columns=["_gate_rank", "_af3_rank"]).to_parquet(
        gated_parquet, index=False
    )
    scores.drop(columns=["_gate_rank", "_af3_rank"]).to_csv(gated_csv, index=False)
    final.drop(columns=["_gate_rank", "_af3_rank"]).to_parquet(
        final_parquet, index=False
    )
    final.drop(columns=["_gate_rank", "_af3_rank"]).to_csv(final_csv, index=False)
    failures = scores.loc[scores["covdock_gate"].eq("C")].drop(
        columns=["_gate_rank", "_af3_rank"]
    )
    failures.to_parquet(failures_parquet, index=False)
    failures.to_csv(failures_csv, index=False)

    success_count = int(scores["covdock_success"].sum())
    success_rate = success_count / len(scores) if len(scores) else 0.0
    counts = {grade: int(scores["covdock_gate"].eq(grade).sum()) for grade in "ABC"}
    detailed_counts = {
        grade: int(scores["covdock_gate_detail"].eq(grade).sum())
        for grade in ("A", "B1", "B2", "C")
    }
    quality = {
        "input_identity_count": len(scores),
        "covdock_success_count": success_count,
        "covdock_success_rate": success_rate,
        "gate_counts": counts,
        "detailed_gate_counts": detailed_counts,
        "final_candidate_count": len(final),
        "identity_loss_count": 0,
        "failure_table_present": failures_parquet.is_file(),
        "physical_unknown_count": int(scores["physical_validity"].eq("UNKNOWN").sum()),
    }
    gates = {
        "covdock_success_rate_ge_minimum": success_rate >= min_success_rate,
        "identity_loss_zero": quality["identity_loss_count"] == 0,
        "failure_table_present": quality["failure_table_present"],
        "final_candidates_available": len(final) > 0,
    }
    outputs = [
        gated_parquet,
        gated_csv,
        final_parquet,
        final_csv,
        failures_parquet,
        failures_csv,
    ]
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": algorithm_version,
        "status": "completed" if all(gates.values()) else "quality_gate_failed",
        "generated_at": utc_now(),
        "policy": {
            "final_n": final_n,
            "min_success_rate": min_success_rate,
            "max_local_strain": max_local_strain,
            "hard_max_strain": hard_max_strain,
            "covalent_bond_length_range": [
                min_covalent_bond_length,
                max_covalent_bond_length,
            ],
            "max_pose_rmsd": max_pose_rmsd,
            "gate_semantics": {
                "A": "physical PASS and AF3-CovDock consensus HIGH",
                "B1": "complete evidence with a soft physical warning or LOW consensus",
                "B2": "one or more required pose-quality checks are missing",
                "C": "one or more hard physical-validity checks failed",
            },
            "physical_validity_semantics": {
                "PASS": "required physical checks present and passing",
                "WARN": "required physical checks present with a soft warning",
                "FAIL": "a hard check failed or no finite successful engine score",
                "UNKNOWN": "one or more required physical measurements are missing or invalid",
            },
            "primary_order_within_gate": "AF3-mPAE rank",
            "covdock_score_role": "supporting evidence, not global reranking",
        },
        "quality": quality,
        "quality_gates": gates,
        "outputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)}
            for path in outputs
        },
    }
    if algorithm_version in {STRICT_ALGORITHM_VERSION, *OPTIONAL_CONSENSUS_VERSIONS}:
        manifest["policy"].update({
            "thresholds_calibrated": False,
            "strain_source_field": STRAIN_COLUMNS[0],
            "selection_rule": "successful execution, complete physical evidence and evaluated pose consensus; review only",
        })
        manifest["policy"]["gate_semantics"]["C"] = "hard physical check failed or execution/score unavailable; inspect independent axes"
        manifest["policy"]["physical_validity_semantics"]["FAIL"] = "a measured hard physical-validity check failed"
    if algorithm_version in OPTIONAL_CONSENSUS_VERSIONS:
        manifest['policy'].update(cross_method_consensus_required=False,
            selection_rule='successful execution and complete physical evidence with PASS or WARN; optional AF3 consensus; review only',
            primary_order_within_gate='stable input order; optional AF3 comparison does not grant or block admission')
        manifest['policy']['gate_semantics'].update(A='physical PASS; cross-method comparison optional',
            B1='complete physical evidence with a soft warning', B2='required physical evidence missing')
    _atomic_json(output_dir / "stage5_gate_manifest.json", manifest)
    return manifest
