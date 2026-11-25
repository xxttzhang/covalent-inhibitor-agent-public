from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem import rdDepictor
from rdkit.Chem.Scaffolds import MurckoScaffold

from covscreen.jobs import sha256_file, utc_now


ALGORITHM_VERSION = "carsidock-recall-pool-v1.0"


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError(f"Unsupported score-table format: {path}")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _validate_identity(frame: pd.DataFrame, name: str) -> pd.DataFrame:
    required = {"input_sdf_index", "compound_id", "engine_status", "raw_score"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{name} is missing required columns: {missing}")
    frame = frame.copy()
    frame["input_sdf_index"] = pd.to_numeric(
        frame["input_sdf_index"], errors="raise"
    ).astype("int64")
    if frame["input_sdf_index"].duplicated().any():
        duplicates = frame.loc[
            frame["input_sdf_index"].duplicated(keep=False), "input_sdf_index"
        ].astype(int).tolist()
        raise ValueError(f"{name} has duplicate input_sdf_index values: {duplicates[:10]}")
    return frame.sort_values("input_sdf_index", kind="stable").reset_index(drop=True)


def _scaffold(smiles: str) -> str:
    molecule = Chem.MolFromSmiles(smiles)
    if molecule is None:
        raise ValueError(f"Selected candidate has invalid standardized_smiles: {smiles}")
    scaffold = MurckoScaffold.GetScaffoldForMol(molecule)
    if scaffold.GetNumAtoms():
        return Chem.MolToSmiles(scaffold, canonical=True)
    digest = hashlib.sha256(Chem.MolToSmiles(molecule).encode()).hexdigest()[:16]
    return f"acyclic:{digest}"


def _rank(
    frame: pd.DataFrame,
    score: str,
    success: str,
    output: str,
    *,
    ascending: bool,
) -> None:
    frame[output] = np.nan
    eligible = frame[success] & pd.to_numeric(frame[score], errors="coerce").notna()
    ordered = frame.loc[eligible].sort_values(
        [score, "input_sdf_index"], ascending=[ascending, True], kind="stable"
    )
    frame.loc[ordered.index, output] = np.arange(1, len(ordered) + 1, dtype=float)


def _interleave(orders: Iterable[list[int]]) -> Iterable[int]:
    queues = [iter(order) for order in orders if order]
    while queues:
        remaining = []
        for queue in queues:
            try:
                yield next(queue)
            except StopIteration:
                continue
            remaining.append(queue)
        queues = remaining


def _write_sdf(frame: pd.DataFrame, path: Path) -> None:
    writer = Chem.SDWriter(str(path))
    try:
        for row in frame.itertuples(index=False):
            molecule = Chem.MolFromSmiles(str(row.standardized_smiles))
            if molecule is None:
                raise ValueError(
                    f"Cannot write selected molecule {row.compound_id}: invalid standardized_smiles"
                )
            rdDepictor.Compute2DCoords(molecule)
            molecule.SetProp("_Name", str(row.compound_id))
            molecule.SetIntProp("INPUT_SDF_INDEX", int(row.input_sdf_index))
            molecule.SetProp("COMPOUND_ID", str(row.compound_id))
            molecule.SetProp("SELECTION_CHANNEL", str(row.stage3_selection_channel))
            molecule.SetIntProp("STAGE3_RANK", int(row.stage3_rank))
            if math.isfinite(float(row.carsiscore)):
                molecule.SetDoubleProp("CARSISCORE", float(row.carsiscore))
            if math.isfinite(float(row.rtmscore)):
                molecule.SetDoubleProp("RTMSCORE", float(row.rtmscore))
            writer.write(molecule)
    finally:
        writer.close()


def build_recall_pool(
    carsidock_scores: Path,
    output_dir: Path,
    *,
    rtmscore_scores: Path | None = None,
    top_n: int = 2_000,
    score_union_n: int | None = None,
    diversity_n: int | None = None,
    rescue_n: int | None = None,
    min_carsidock_success_rate: float = 0.75,
) -> dict[str, Any]:
    if top_n < 1:
        raise ValueError("top_n must be positive")
    if not 0.0 <= min_carsidock_success_rate <= 1.0:
        raise ValueError("min_carsidock_success_rate must be between zero and one")
    carsi = _validate_identity(_read_table(carsidock_scores), "CarsiDock score table")
    if "standardized_smiles" not in carsi:
        raise ValueError("CarsiDock score table is missing standardized_smiles")
    if carsi.empty:
        raise ValueError("CarsiDock score table is empty")

    frame = carsi.rename(
        columns={
            "raw_score": "carsiscore",
            "engine_status": "carsidock_status",
            "failure_reason": "carsidock_failure_reason",
        }
    )
    frame["carsiscore"] = pd.to_numeric(frame["carsiscore"], errors="coerce")
    frame["carsidock_success"] = frame["carsidock_status"].eq("SUCCESS") & frame[
        "carsiscore"
    ].notna()

    if rtmscore_scores is not None:
        rtm = _validate_identity(_read_table(rtmscore_scores), "RTMScore table")
        carsi_identity = set(zip(frame["input_sdf_index"], frame["compound_id"].astype(str)))
        rtm_identity = set(zip(rtm["input_sdf_index"], rtm["compound_id"].astype(str)))
        if carsi_identity != rtm_identity:
            missing = sorted(carsi_identity - rtm_identity)[:10]
            extra = sorted(rtm_identity - carsi_identity)[:10]
            raise ValueError(
                f"RTMScore identity coverage differs from CarsiDock; missing={missing}, extra={extra}"
            )
        rtm = rtm[
            ["input_sdf_index", "compound_id", "raw_score", "engine_status", "failure_reason"]
        ].rename(
            columns={
                "raw_score": "rtmscore",
                "engine_status": "rtmscore_status",
                "failure_reason": "rtmscore_failure_reason",
            }
        )
        frame = frame.merge(
            rtm,
            on=["input_sdf_index", "compound_id"],
            how="left",
            validate="one_to_one",
        )
    else:
        frame["rtmscore"] = np.nan
        frame["rtmscore_status"] = "NOT_RUN"
        frame["rtmscore_failure_reason"] = "rtmscore_not_provided"

    frame["rtmscore"] = pd.to_numeric(frame["rtmscore"], errors="coerce")
    frame["rtmscore_success"] = frame["rtmscore_status"].eq("SUCCESS") & frame[
        "rtmscore"
    ].notna()
    _rank(
        frame,
        "carsiscore",
        "carsidock_success",
        "carsiscore_rank",
        ascending=True,
    )
    _rank(
        frame,
        "rtmscore",
        "rtmscore_success",
        "rtmscore_rank",
        ascending=False,
    )
    for column in ("rank", "channel_score", "reference_similarity"):
        if column not in frame:
            frame[column] = np.nan
    if "scaffold_smiles" not in frame:
        frame["scaffold_smiles"] = frame["standardized_smiles"].map(_scaffold)
    else:
        missing_scaffold = frame["scaffold_smiles"].isna() | frame[
            "scaffold_smiles"
        ].astype(str).eq("")
        frame.loc[missing_scaffold, "scaffold_smiles"] = frame.loc[
            missing_scaffold, "standardized_smiles"
        ].map(_scaffold)

    union_quota = int(score_union_n if score_union_n is not None else round(top_n * 0.70))
    diversity_quota = int(diversity_n if diversity_n is not None else round(top_n * 0.20))
    rescue_quota = int(rescue_n if rescue_n is not None else top_n - union_quota - diversity_quota)
    if min(union_quota, diversity_quota, rescue_quota) < 0:
        raise ValueError("selection quotas cannot be negative")
    if union_quota + diversity_quota + rescue_quota != top_n:
        raise ValueError("score, diversity, and rescue quotas must sum to top_n")

    chosen: list[int] = []
    channels: dict[int, str] = {}
    reasons: dict[int, str] = {}

    def take(indices: Iterable[int], quota: int, channel: str, reason: str) -> None:
        added = 0
        for index in indices:
            if added >= quota:
                return
            identity = int(frame.at[index, "input_sdf_index"])
            if identity in channels:
                continue
            chosen.append(index)
            channels[identity] = channel
            reasons[identity] = reason
            added += 1

    carsi_order = frame.loc[frame["carsidock_success"]].sort_values(
        ["carsiscore_rank", "input_sdf_index"], kind="stable"
    ).index.tolist()
    rtm_order = frame.loc[frame["rtmscore_success"]].sort_values(
        ["rtmscore_rank", "input_sdf_index"], kind="stable"
    ).index.tolist()
    take(
        _interleave([carsi_order, rtm_order]),
        union_quota,
        "score_union",
        "interleaved CarsiScore and RTMScore rank union",
    )

    frame["best_structure_rank"] = frame[["carsiscore_rank", "rtmscore_rank"]].min(
        axis=1, skipna=True
    )
    selected_scaffolds = {
        str(frame.at[index, "scaffold_smiles"]) for index in chosen
    }
    diversity_candidates = frame.loc[
        frame["carsidock_success"]
        & ~frame["input_sdf_index"].astype(int).isin(channels)
        & ~frame["scaffold_smiles"].astype(str).isin(selected_scaffolds)
    ].sort_values(
        ["best_structure_rank", "rank", "input_sdf_index"],
        na_position="last",
        kind="stable",
    )
    diversity_representatives = diversity_candidates.drop_duplicates(
        "scaffold_smiles", keep="first"
    ).index.tolist()
    take(
        diversity_representatives,
        diversity_quota,
        "scaffold_diversity",
        "best remaining representative of an unselected Bemis-Murcko scaffold",
    )

    rescue_candidates = frame.loc[
        ~frame["carsidock_success"]
        & ~frame["input_sdf_index"].astype(int).isin(channels)
    ].copy()
    rescue_candidates["_channel"] = pd.to_numeric(
        rescue_candidates.get("channel_score", np.nan), errors="coerce"
    )
    rescue_candidates["_rank"] = pd.to_numeric(
        rescue_candidates.get("rank", np.nan), errors="coerce"
    )
    rescue_candidates["_similarity"] = pd.to_numeric(
        rescue_candidates.get("reference_similarity", np.nan), errors="coerce"
    )
    rescue_order = rescue_candidates.sort_values(
        ["_channel", "_rank", "_similarity", "input_sdf_index"],
        ascending=[False, True, False, True],
        na_position="last",
        kind="stable",
    ).index.tolist()
    take(
        rescue_order,
        rescue_quota,
        "failure_rescue",
        "CarsiDock failure retained by strong stage-two evidence",
    )

    remaining_success = frame.loc[
        frame["carsidock_success"]
        & ~frame["input_sdf_index"].astype(int).isin(channels)
    ].sort_values(
        ["best_structure_rank", "rank", "input_sdf_index"],
        na_position="last",
        kind="stable",
    ).index.tolist()
    take(
        remaining_success,
        top_n - len(chosen),
        "success_backfill",
        "remaining successful pose ordered by best available structure rank",
    )
    remaining_failure = frame.loc[
        ~frame["input_sdf_index"].astype(int).isin(channels)
    ].copy()
    remaining_failure["_channel"] = pd.to_numeric(
        remaining_failure.get("channel_score", np.nan), errors="coerce"
    )
    remaining_failure["_rank"] = pd.to_numeric(
        remaining_failure.get("rank", np.nan), errors="coerce"
    )
    remaining_failure = remaining_failure.sort_values(
        ["_channel", "_rank", "input_sdf_index"],
        ascending=[False, True, True],
        na_position="last",
        kind="stable",
    ).index.tolist()
    take(
        remaining_failure,
        top_n - len(chosen),
        "evidence_backfill",
        "remaining candidate ordered by stage-two evidence",
    )

    selected = frame.loc[chosen].copy()
    selected["stage3_rank"] = np.arange(1, len(selected) + 1, dtype=int)
    selected["stage3_selection_channel"] = selected["input_sdf_index"].astype(int).map(
        channels
    )
    selected["stage3_selection_reason"] = selected["input_sdf_index"].astype(int).map(
        reasons
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet = output_dir / "stage3_recall_pool.parquet"
    csv = output_dir / "stage3_recall_pool.csv"
    sdf = output_dir / "stage3_recall_pool.sdf"
    selected.to_parquet(parquet, index=False)
    selected.to_csv(csv, index=False)
    _write_sdf(selected, sdf)

    failures = frame.loc[~frame["carsidock_success"]].copy()
    failures["selected_for_rescue"] = failures["input_sdf_index"].astype(int).isin(
        set(selected.loc[selected["stage3_selection_channel"].eq("failure_rescue"), "input_sdf_index"].astype(int))
    )
    failures_parquet = output_dir / "stage3_failures.parquet"
    failures_csv = output_dir / "stage3_failures.csv"
    failures.to_parquet(failures_parquet, index=False)
    failures.to_csv(failures_csv, index=False)

    success_count = int(frame["carsidock_success"].sum())
    rtm_success_count = int(frame["rtmscore_success"].sum())
    success_rate = success_count / len(frame)
    rtm_coverage = rtm_success_count / success_count if success_count else 0.0
    quality = {
        "input_identity_count": len(frame),
        "selected_identity_count": len(selected),
        "identity_loss_count": 0,
        "selected_identity_unique": not selected["input_sdf_index"].duplicated().any(),
        "carsidock_success_count": success_count,
        "carsidock_success_rate": success_rate,
        "rtmscore_success_count": rtm_success_count,
        "rtmscore_coverage_of_carsidock_success": rtm_coverage,
        "failure_count": len(failures),
        "failure_table_present": failures_parquet.is_file(),
    }
    gates = {
        "carsidock_success_rate_ge_minimum": success_rate
        >= min_carsidock_success_rate,
        "identity_loss_zero": quality["identity_loss_count"] == 0,
        "selected_identity_unique": quality["selected_identity_unique"],
        "failure_table_present": quality["failure_table_present"],
        "selection_budget_satisfied_or_input_exhausted": len(selected)
        == min(top_n, len(frame)),
    }
    outputs = [parquet, csv, sdf, failures_parquet, failures_csv]
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": ALGORITHM_VERSION,
        "status": "completed" if all(gates.values()) else "quality_gate_failed",
        "generated_at": utc_now(),
        "inputs": {
            "carsidock_scores": {
                "path": str(carsidock_scores),
                "sha256": sha256_file(carsidock_scores),
            },
            "rtmscore_scores": (
                {
                    "path": str(rtmscore_scores),
                    "sha256": sha256_file(rtmscore_scores),
                }
                if rtmscore_scores is not None
                else None
            ),
        },
        "selection": {
            "top_n_requested": top_n,
            "top_n_selected": len(selected),
            "quotas": {
                "score_union": union_quota,
                "scaffold_diversity": diversity_quota,
                "failure_rescue": rescue_quota,
            },
            "actual_channels": {
                str(key): int(value)
                for key, value in selected["stage3_selection_channel"].value_counts().items()
            },
        },
        "quality": quality,
        "quality_gates": gates,
        "outputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)}
            for path in outputs
        },
    }
    _atomic_json(output_dir / "stage3_selection_manifest.json", manifest)
    return manifest
