from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem import rdDepictor

from .jobs import sha256_file, utc_now


ALGORITHM_VERSION = "af3-mpae-selection-v1.0"


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _write_sdf(frame: pd.DataFrame, path: Path) -> None:
    writer = Chem.SDWriter(str(path))
    try:
        for row in frame.itertuples(index=False):
            molecule = Chem.MolFromSmiles(str(row.standardized_smiles))
            if molecule is None:
                raise ValueError(f"Invalid selected ligand SMILES for {row.compound_id}")
            rdDepictor.Compute2DCoords(molecule)
            molecule.SetProp("_Name", str(row.compound_id))
            molecule.SetIntProp("INPUT_SDF_INDEX", int(row.input_sdf_index))
            molecule.SetProp("COMPOUND_ID", str(row.compound_id))
            molecule.SetIntProp("AF3_MPAE_RANK", int(row.af3_mpae_rank))
            molecule.SetDoubleProp("AF3_MPAE", float(row.raw_score))
            writer.write(molecule)
    finally:
        writer.close()


def select_af3_candidates(
    scores: pd.DataFrame,
    output_dir: Path,
    *,
    top_n: int = 100,
    min_coverage: float = 0.95,
) -> dict[str, Any]:
    required = {
        "input_sdf_index",
        "compound_id",
        "standardized_smiles",
        "engine_status",
        "raw_score",
    }
    missing = sorted(required - set(scores.columns))
    if missing:
        raise ValueError(f"AF3 score table is missing columns: {missing}")
    if top_n < 1:
        raise ValueError("AF3 top_n must be positive")
    if not 0.0 <= min_coverage <= 1.0:
        raise ValueError("AF3 minimum coverage must be between zero and one")
    scores = scores.copy()
    scores["input_sdf_index"] = pd.to_numeric(
        scores["input_sdf_index"], errors="raise"
    ).astype("int64")
    if scores["input_sdf_index"].duplicated().any():
        raise ValueError("AF3 score table has duplicate input_sdf_index values")
    scores["raw_score"] = pd.to_numeric(scores["raw_score"], errors="coerce")
    valid_score = np.isfinite(scores["raw_score"]) & scores["raw_score"].ge(0)
    invalid_success = scores["engine_status"].eq("SUCCESS") & ~valid_score
    scores.loc[invalid_success, "engine_status"] = "FAILED"
    scores.loc[invalid_success, "failure_reason"] = "invalid_af3_mpae"
    scores["af3_success"] = scores["engine_status"].eq("SUCCESS") & valid_score
    ordered = scores.loc[scores["af3_success"]].sort_values(
        ["raw_score", "input_sdf_index"], ascending=[True, True], kind="stable"
    )
    selected = ordered.head(top_n).copy()
    selected["af3_mpae_rank"] = np.arange(1, len(selected) + 1, dtype=int)

    output_dir.mkdir(parents=True, exist_ok=True)
    all_parquet = output_dir / "af3_all_scores.parquet"
    all_csv = output_dir / "af3_all_scores.csv"
    failures_parquet = output_dir / "af3_failures.parquet"
    failures_csv = output_dir / "af3_failures.csv"
    top_parquet = output_dir / "stage4_top_candidates.parquet"
    top_csv = output_dir / "stage4_top_candidates.csv"
    top_sdf = output_dir / "stage4_top_candidates.sdf"
    scores.to_parquet(all_parquet, index=False)
    scores.to_csv(all_csv, index=False)
    failures = scores.loc[~scores["af3_success"]].copy()
    failures.to_parquet(failures_parquet, index=False)
    failures.to_csv(failures_csv, index=False)
    selected.to_parquet(top_parquet, index=False)
    selected.to_csv(top_csv, index=False)
    _write_sdf(selected, top_sdf)

    success_count = int(scores["af3_success"].sum())
    coverage = success_count / len(scores) if len(scores) else 0.0
    quality = {
        "input_identity_count": len(scores),
        "success_count": success_count,
        "failure_count": len(failures),
        "coverage": coverage,
        "selected_count": len(selected),
        "identity_loss_count": 0,
        "selected_identity_unique": not selected["input_sdf_index"].duplicated().any(),
        "failure_table_present": failures_parquet.is_file(),
    }
    gates = {
        "coverage_ge_minimum": coverage >= min_coverage,
        "identity_loss_zero": quality["identity_loss_count"] == 0,
        "selected_identity_unique": quality["selected_identity_unique"],
        "failure_table_present": quality["failure_table_present"],
        "selection_budget_satisfied_or_success_exhausted": len(selected)
        == min(top_n, success_count),
    }
    outputs = [
        all_parquet,
        all_csv,
        failures_parquet,
        failures_csv,
        top_parquet,
        top_csv,
        top_sdf,
    ]
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": ALGORITHM_VERSION,
        "status": "completed" if all(gates.values()) else "quality_gate_failed",
        "generated_at": utc_now(),
        "selection": {
            "primary_score": "interface_mpae",
            "score_direction": "lower_is_better",
            "top_n_requested": top_n,
            "top_n_selected": len(selected),
        },
        "quality": quality,
        "quality_gates": gates,
        "outputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)}
            for path in outputs
        },
    }
    _atomic_json(output_dir / "stage4_selection_manifest.json", manifest)
    return manifest
