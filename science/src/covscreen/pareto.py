from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold

from .jobs import sha256_file, utc_now


ALGORITHM_VERSIONS = {
    "library-first": "library-first-pareto-v2.0",
    "ligand-first": "ligand-first-pareto-v1.0",
}
Direction = Literal["min", "max"]


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _boolean_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    normalized = frame[column].astype(str).str.strip().str.lower()
    return normalized.map(
        {"true": 1.0, "1": 1.0, "yes": 1.0, "false": 0.0, "0": 0.0, "no": 0.0}
    )


def _numeric_series(frame: pd.DataFrame, column: str) -> pd.Series:
    if column not in frame:
        return pd.Series(np.nan, index=frame.index, dtype=float)
    return pd.to_numeric(frame[column], errors="coerce")


def _scaffold(smiles: str) -> str:
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError(f"invalid standardized_smiles in Pareto input: {smiles}")
    scaffold = MurckoScaffold.GetScaffoldForMol(molecule)
    return Chem.MolToSmiles(scaffold, canonical=True) if scaffold.GetNumAtoms() else smiles


def _pareto_fronts(values: np.ndarray, directions: list[Direction]) -> np.ndarray:
    """Return one-based non-dominated front numbers for a complete numeric matrix."""

    transformed = values.copy()
    for index, direction in enumerate(directions):
        if direction == "max":
            transformed[:, index] *= -1.0
    remaining = list(range(len(transformed)))
    fronts = np.zeros(len(transformed), dtype=int)
    front_number = 1
    while remaining:
        current: list[int] = []
        for candidate in remaining:
            dominated = False
            for competitor in remaining:
                if competitor == candidate:
                    continue
                no_worse = np.all(transformed[competitor] <= transformed[candidate])
                strictly_better = np.any(transformed[competitor] < transformed[candidate])
                if no_worse and strictly_better:
                    dominated = True
                    break
            if not dominated:
                current.append(candidate)
        if not current:
            raise RuntimeError("Pareto sorting made no progress")
        fronts[current] = front_number
        chosen = set(current)
        remaining = [index for index in remaining if index not in chosen]
        front_number += 1
    return fronts


def _write_sdf(frame: pd.DataFrame, path: Path) -> None:
    writer = Chem.SDWriter(str(path))
    try:
        for row in frame.itertuples(index=False):
            molecule = Chem.MolFromSmiles(str(row.standardized_smiles))
            if molecule is None:
                raise ValueError(f"invalid final candidate SMILES: {row.compound_id}")
            molecule.SetProp("_Name", str(row.compound_id))
            molecule.SetIntProp("INPUT_SDF_INDEX", int(row.input_sdf_index))
            molecule.SetProp("COMPOUND_ID", str(row.compound_id))
            molecule.SetIntProp("FINAL_RANK", int(row.final_rank))
            molecule.SetIntProp("PARETO_FRONT", int(row.pareto_front))
            molecule.SetProp("COVDOCK_GATE", str(row.covdock_gate))
            if hasattr(row, "covdock_gate_detail"):
                molecule.SetProp("COVDOCK_GATE_DETAIL", str(row.covdock_gate_detail))
            if hasattr(row, "physical_validity"):
                molecule.SetProp("PHYSICAL_VALIDITY", str(row.physical_validity))
            if hasattr(row, "cross_method_consensus"):
                molecule.SetProp(
                    "CROSS_METHOD_CONSENSUS", str(row.cross_method_consensus)
                )
            if np.isfinite(float(row.af3_mpae)):
                molecule.SetDoubleProp("AF3_MPAE", float(row.af3_mpae))
            writer.write(molecule)
    finally:
        writer.close()


def select_pareto_candidates(
    candidates: pd.DataFrame,
    output_dir: Path,
    *,
    final_n: int = 30,
    additional_objectives: dict[str, Direction] | None = None,
    profile: Literal["library-first", "ligand-first"] = "library-first",
) -> dict[str, Any]:
    required = {
        "input_sdf_index",
        "compound_id",
        "standardized_smiles",
        "covdock_gate",
        "af3_mpae",
        "property_fit_score",
    }
    missing = sorted(required - set(candidates))
    if missing:
        raise ValueError(f"Pareto candidate table is missing columns: {missing}")
    if final_n < 1:
        raise ValueError("Pareto final_n must be positive")
    frame = candidates.copy()
    # Persist the exact route-specific ranking algorithm on every row.  The
    # manifest alone is insufficient for exported candidate tables and the
    # unified workbench must never infer a ranking policy from file order.
    frame["ranking_policy"] = ALGORITHM_VERSIONS[profile]
    frame["input_sdf_index"] = pd.to_numeric(
        frame["input_sdf_index"], errors="raise"
    ).astype("int64")
    if frame["input_sdf_index"].duplicated().any():
        raise ValueError("Pareto candidate table has duplicate input_sdf_index values")
    frame["af3_mpae"] = pd.to_numeric(frame["af3_mpae"], errors="coerce")
    frame["property_fit_score"] = pd.to_numeric(
        frame["property_fit_score"], errors="coerce"
    )
    if "physical_validity" not in frame:
        frame["physical_validity"] = frame["covdock_gate"].map(
            {"A": "PASS", "B": "WARN", "C": "FAIL"}
        )
    frame["physical_validity"] = (
        frame["physical_validity"].astype(str).str.strip().str.upper()
    )
    if "cross_method_consensus" not in frame:
        pose_rmsd = _numeric_series(frame, "af3_covdock_ligand_rmsd_angstrom")
        frame["cross_method_consensus"] = np.where(
            pose_rmsd.notna(), np.where(pose_rmsd.le(3.0), "HIGH", "LOW"), "NA"
        )
    frame["cross_method_consensus"] = (
        frame["cross_method_consensus"].astype(str).str.strip().str.upper()
    )
    if "covdock_gate_detail" not in frame:
        frame["covdock_gate_detail"] = frame["covdock_gate"]
    frame["af3_covdock_ligand_rmsd_angstrom"] = _numeric_series(
        frame, "af3_covdock_ligand_rmsd_angstrom"
    )
    frame["local_strain_energy"] = _numeric_series(frame, "local_strain_energy")

    risk_columns = {
        "high_reactivity_alert": _boolean_series(frame, "high_reactivity_alert"),
        "aggregation_risk": _boolean_series(frame, "aggregation_risk"),
    }
    pains_available = "pains_alert" in frame
    if pains_available:
        risk_columns["pains_alert"] = (
            frame["pains_alert"].fillna("").astype(str).str.strip().ne("").astype(float)
        )
    risk_evidence_available = all(
        series.notna().all() for series in risk_columns.values()
    ) and pains_available
    if risk_evidence_available:
        frame["chemistry_risk_count"] = sum(risk_columns.values())
    else:
        frame["chemistry_risk_count"] = np.nan

    if "scaffold_smiles" not in frame:
        frame["scaffold_smiles"] = frame["standardized_smiles"].map(_scaffold)
    missing_scaffold = frame["scaffold_smiles"].isna() | frame[
        "scaffold_smiles"
    ].astype(str).str.strip().eq("")
    frame.loc[missing_scaffold, "scaffold_smiles"] = frame.loc[
        missing_scaffold, "standardized_smiles"
    ].map(_scaffold)

    eligible = frame["physical_validity"].isin(["PASS", "WARN"])
    if profile == "ligand-first":
        if "independent_gate" not in frame:
            raise ValueError("Ligand-first Pareto requires independent_gate")
        frame["independent_gate"] = frame["independent_gate"].astype(str).str.strip().str.upper()
        eligible &= frame["independent_gate"].isin(["PASS", "WARN"])
    objectives: dict[str, Direction] = {
        "af3_mpae": "min",
        "property_fit_score": "max",
        "chemistry_risk_count": "min",
        "local_strain_energy": "min",
    }
    if profile == "library-first":
        objectives["af3_covdock_ligand_rmsd_angstrom"] = "min"
    else:
        if "independent_parent_mcs_fraction" not in frame:
            raise ValueError(
                "Ligand-first Pareto input lacks independent_parent_mcs_fraction"
            )
        frame["independent_parent_mcs_fraction"] = pd.to_numeric(
            frame["independent_parent_mcs_fraction"], errors="coerce"
        )
        objectives["independent_parent_mcs_fraction"] = "max"
        if frame.loc[eligible, "af3_covdock_ligand_rmsd_angstrom"].notna().all():
            objectives["af3_covdock_ligand_rmsd_angstrom"] = "min"
    optional_evidence: dict[str, str] = {
        "admet": "unavailable",
        "commercial_availability": "unavailable",
    }
    for column in ("admet_score", "admet_probability"):
        if column in frame and pd.to_numeric(frame[column], errors="coerce").notna().all():
            objectives[column] = "max"
            frame[column] = pd.to_numeric(frame[column], errors="coerce")
            optional_evidence["admet"] = f"available:{column}"
            break
    for column in ("commercially_available", "commercial_availability"):
        if column in frame:
            commercial = _boolean_series(frame, column)
            if commercial.notna().all():
                frame["commercial_availability_objective"] = commercial
                objectives["commercial_availability_objective"] = "max"
                optional_evidence["commercial_availability"] = f"available:{column}"
                break
    for column, direction in (additional_objectives or {}).items():
        if direction not in {"min", "max"}:
            raise ValueError(f"invalid objective direction for {column}: {direction}")
        if column not in frame:
            raise ValueError(f"additional Pareto objective is absent: {column}")
        numeric = pd.to_numeric(frame[column], errors="coerce")
        if not numeric.notna().all():
            raise ValueError(f"additional Pareto objective is incomplete: {column}")
        frame[column] = numeric
        objectives[column] = direction

    for column in objectives:
        frame[column] = pd.to_numeric(frame[column], errors="coerce").replace(
            [np.inf, -np.inf], np.nan)

    baseline_available = {
        "af3_mpae": bool(frame.loc[eligible, "af3_mpae"].notna().all()),
        "local_strain_energy": bool(
            frame.loc[eligible, "local_strain_energy"].notna().all()
        ),
        "physical_validity": bool(
            frame["physical_validity"].isin(["PASS", "WARN", "FAIL"]).all()
        ),
        "property_fit_score": bool(
            frame.loc[eligible, "property_fit_score"].notna().all()
        ),
        "chemistry_risk": bool(risk_evidence_available),
    }
    if profile == "library-first":
        baseline_available["pose_consistency_rmsd"] = bool(
            frame.loc[eligible, "af3_covdock_ligand_rmsd_angstrom"].notna().all()
        )
    else:
        baseline_available["independent_parent_mcs_fraction"] = bool(
            frame.loc[eligible, "independent_parent_mcs_fraction"].notna().all()
        )
    complete = np.isfinite(frame[list(objectives)].to_numpy(dtype=float)).all(axis=1)
    frame["pareto_eligible"] = eligible & complete
    frame["pareto_exclusion_reason"] = np.where(
        ~eligible,
        "physical_or_independent_gate_FAIL_or_unknown" if profile == "ligand-first"
        else "physical_validity_FAIL_or_unknown",
        np.where(~complete, "incomplete_required_objective", ""),
    )
    decision = frame.loc[frame["pareto_eligible"]].copy()
    if not decision.empty:
        decision["pareto_front"] = _pareto_fronts(
            decision[list(objectives)].to_numpy(dtype=float),
            list(objectives.values()),
        )
    else:
        decision["pareto_front"] = pd.Series(dtype=int)
    frame["pareto_front"] = np.nan
    frame.loc[decision.index, "pareto_front"] = decision["pareto_front"]

    # Walk Pareto fronts in order. Within each front, take unseen scaffolds
    # first, then backfill deterministically. Labels only break ties inside a
    # front; they are not numeric Pareto objectives and cannot be double-counted.
    af3_order_column = "af3_mpae_rank" if "af3_mpae_rank" in decision else "af3_mpae"
    decision["_physical_rank"] = decision["physical_validity"].map(
        {"PASS": 0, "WARN": 1}
    )
    decision["_consensus_rank"] = decision["cross_method_consensus"].map(
        {"HIGH": 0, "LOW": 1, "NA": 2}
    ).fillna(3)
    selected_indices: list[int] = []
    selected_scaffolds: set[str] = set()
    for front in sorted(decision["pareto_front"].unique()):
        subset = decision.loc[decision["pareto_front"].eq(front)].sort_values(
            [
                "_physical_rank",
                "_consensus_rank",
                af3_order_column,
                "af3_covdock_ligand_rmsd_angstrom",
                "chemistry_risk_count",
                "input_sdf_index",
            ],
            kind="stable",
        )
        novel_indices: list[int] = []
        repeated_indices: list[int] = []
        seen_this_front = set(selected_scaffolds)
        for index, scaffold in subset["scaffold_smiles"].astype(str).items():
            if scaffold in seen_this_front:
                repeated_indices.append(index)
            else:
                novel_indices.append(index)
                seen_this_front.add(scaffold)
        for index in [*novel_indices, *repeated_indices]:
            if len(selected_indices) >= final_n:
                break
            selected_indices.append(index)
            selected_scaffolds.add(str(frame.at[index, "scaffold_smiles"]))
        if len(selected_indices) >= final_n:
            break
    selected = frame.loc[selected_indices].copy()
    selected["final_rank"] = np.arange(1, len(selected) + 1, dtype=int)
    selected["experimental_priority_band"] = np.select(
        [selected["final_rank"].le(20), selected["final_rank"].le(50)],
        ["TOP20", "TOP50"],
        default="TOP100",
    )
    selected["selection_reason"] = (
        "physical hard-filter; Pareto front order; unique scaffold first; "
        "physical/consensus labels and AF3-mPAE as within-front tie-breaks"
    )

    output_dir.mkdir(parents=True, exist_ok=True)
    all_parquet = output_dir / "stage6_pareto_all_candidates.parquet"
    all_csv = output_dir / "stage6_pareto_all_candidates.csv"
    front_parquet = output_dir / "stage6_pareto_front.parquet"
    front_csv = output_dir / "stage6_pareto_front.csv"
    final_parquet = output_dir / "stage6_final_candidates.parquet"
    final_csv = output_dir / "stage6_final_candidates.csv"
    final_sdf = output_dir / "stage6_final_candidates.sdf"
    frame.to_parquet(all_parquet, index=False)
    frame.to_csv(all_csv, index=False)
    first_front = frame.loc[frame["pareto_front"].eq(1)].copy()
    first_front.to_parquet(front_parquet, index=False)
    first_front.to_csv(front_csv, index=False)
    selected.to_parquet(final_parquet, index=False)
    selected.to_csv(final_csv, index=False)
    _write_sdf(selected, final_sdf)

    card_columns = [
        column
        for column in (
            "final_rank", "compound_id", "input_sdf_index", "standardized_smiles",
            "scaffold_smiles", "pareto_front", "experimental_priority_band",
            "covdock_gate", "covdock_gate_detail", "physical_validity",
            "cross_method_consensus", "covdock_gate_reason",
            "af3_mpae", "af3_mpae_rank", "covdock_score", "local_strain_energy",
            "af3_covdock_ligand_rmsd_angstrom",
            "covalent_bond_length_angstrom", "property_fit_score",
            "chemistry_risk_count", "reference_similarity", "source_record_id",
            "warhead_class", "linker_name", "exit_vector_id",
            "independent_gate", "independent_parent_mcs_fraction",
            "independent_failure_reasons", "independent_warning_reasons",
        )
        if column in selected
    ]
    cards = selected[card_columns].replace({np.nan: None}).to_dict(orient="records")
    cards_path = output_dir / "stage6_candidate_cards.json"
    _atomic_json(cards_path, {"schema_version": "1.0", "candidates": cards})

    report_path = output_dir / "stage6_selection_report.md"
    report_path.write_text(
        "# Covalent inhibitor candidate selection report\n\n"
        f"- Route profile: {profile}\n"
        f"- Input candidates: {len(frame)}\n"
        f"- Physically eligible with complete objectives: {len(decision)}\n"
        f"- Pareto front 1: {len(first_front)}\n"
        f"- Final candidates: {len(selected)}\n"
        f"- Objectives: {json.dumps(objectives, ensure_ascii=False)}\n"
        "- Policy: physical FAIL candidates are excluded; remaining candidates are "
        "ordered by non-dominated fronts with scaffold round-robin diversity.\n"
        "- Interpretation: rankings are computational priorities and require medicinal "
        "chemistry review and experimental validation before synthesis or purchase.\n"
    )

    gates = {
        "identity_unique": bool(not frame["input_sdf_index"].duplicated().any()),
        "baseline_objectives_available": bool(all(baseline_available.values())),
        "eligible_candidates_available": len(decision) > 0,
        "selection_budget_satisfied_or_eligible_exhausted": len(selected)
        == min(final_n, len(decision)),
    }
    outputs = [
        all_parquet, all_csv, front_parquet, front_csv, final_parquet, final_csv,
        final_sdf, cards_path, report_path,
    ]
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": ALGORITHM_VERSIONS[profile],
        "status": "completed" if all(gates.values()) else "quality_gate_failed",
        "generated_at": utc_now(),
        "selection_policy": {
            "route_profile": profile,
            "method": "non_dominated_sorting_with_scaffold_round_robin",
            "weighted_sum_used": False,
            "objectives": objectives,
            "hard_filter": (
                "physical_validity and independent_gate in {PASS, WARN}; finite objectives required"
                if profile == "ligand-first" else
                "physical_validity in {PASS, WARN}; FAIL/unknown excluded; finite objectives required"
            ),
            "gate_labels_are_objectives": False,
            "within_front_tie_break": [
                "physical_validity",
                "cross_method_consensus",
                "AF3-mPAE",
                "pose-consistency RMSD",
                "chemistry risk count",
                "input identity",
            ],
            "final_n_requested": final_n,
            "final_n_selected": len(selected),
        },
        "evidence": {
            "baseline": baseline_available,
            "optional": optional_evidence,
            "decision_evidence_status": (
                "complete_for_baseline_pareto"
                if all(value.startswith("available:") for value in optional_evidence.values())
                else "baseline_ready_optional_evidence_missing"
            ),
            "manual_review_required_before_purchase": True,
        },
        "quality": {
            "input_count": len(frame),
            "eligible_count": len(decision),
            "pareto_front_one_count": len(first_front),
            "selected_count": len(selected),
            "selected_unique_scaffolds": selected["scaffold_smiles"].nunique(),
            "identity_loss_count": 0,
        },
        "quality_gates": gates,
        "outputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)}
            for path in outputs
        },
    }
    _atomic_json(output_dir / "stage6_pareto_manifest.json", manifest)
    return manifest
