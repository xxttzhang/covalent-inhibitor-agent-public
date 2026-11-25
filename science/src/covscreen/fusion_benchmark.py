from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Iterable

import numpy as np
import pandas as pd

from .metrics import summarize


IDENTITY_COLUMNS = ["compound_id", "protomer_id", "input_sdf_index"]
ALGORITHM_VERSION = "af3_covdock_lo_cascade_v1"


def _finite_success(frame: pd.DataFrame) -> pd.Series:
    score = pd.to_numeric(frame["raw_score"], errors="coerce")
    status = frame.get("engine_status", pd.Series("FAILED", index=frame.index))
    return score.notna() & np.isfinite(score) & status.astype(str).eq("SUCCESS")


def _require_unique(frame: pd.DataFrame, name: str) -> None:
    missing = [column for column in IDENTITY_COLUMNS if column not in frame]
    if missing:
        raise ValueError(f"{name} is missing identity columns: {missing}")
    if frame.duplicated(IDENTITY_COLUMNS).any():
        raise ValueError(f"{name} contains duplicate identity tuples")


def merge_pair(
    library: pd.DataFrame,
    af3: pd.DataFrame,
    covdock_lo: pd.DataFrame,
) -> pd.DataFrame:
    """Join the frozen library to AF3 and CovDock-LO without dropping failures."""
    for name, frame in (("library", library), ("af3", af3), ("covdock_lo", covdock_lo)):
        _require_unique(frame, name)
    if "label" not in library:
        raise ValueError("library is missing label")
    labels = set(library["label"].dropna().astype(str))
    if labels != {"active", "decoy"}:
        raise ValueError(f"unexpected labels: {sorted(labels)}")

    out = library.copy()
    for name, frame in (("af3", af3), ("covdock_lo", covdock_lo)):
        success = _finite_success(frame)
        columns = frame[IDENTITY_COLUMNS + ["raw_score"]].copy()
        columns[f"{name}_success"] = success.to_numpy(dtype=bool)
        columns = columns.rename(columns={"raw_score": f"{name}_score"})
        out = out.merge(columns, on=IDENTITY_COLUMNS, how="left", validate="one_to_one")
        if out[f"{name}_success"].isna().any():
            raise ValueError(f"{name} does not cover every frozen library identity")
        out[f"{name}_success"] = out[f"{name}_success"].astype(bool)
        out.loc[~out[f"{name}_success"], f"{name}_score"] = np.nan
    return out


def deterministic_order(
    frame: pd.DataFrame,
    score_column: str,
    direction: str,
    *,
    secondary_column: str | None = None,
    secondary_direction: str = "higher_is_better",
) -> pd.Series:
    """Return a 1-based total order with failures last and label-free tie breaks."""
    if direction not in {"higher_is_better", "lower_is_better"}:
        raise ValueError(f"unknown direction: {direction}")
    work = pd.DataFrame(index=frame.index)
    work["_score"] = pd.to_numeric(frame[score_column], errors="coerce")
    work["_failed"] = ~np.isfinite(work["_score"])
    work["_input_sdf_index"] = pd.to_numeric(
        frame["input_sdf_index"], errors="raise"
    ).astype(int)
    by = ["_failed", "_score"]
    ascending = [True, direction == "lower_is_better"]
    if secondary_column and secondary_column in frame:
        work["_secondary"] = pd.to_numeric(frame[secondary_column], errors="coerce")
        work["_secondary_failed"] = ~np.isfinite(work["_secondary"])
        by.extend(["_secondary_failed", "_secondary"])
        ascending.extend([True, secondary_direction == "lower_is_better"])
    by.append("_input_sdf_index")
    ascending.append(True)
    ordered_index = work.sort_values(by, ascending=ascending, kind="stable").index
    ranks = pd.Series(index=frame.index, dtype=int)
    ranks.loc[ordered_index] = np.arange(1, len(frame) + 1, dtype=int)
    return ranks.astype(int)


def _metrics(frame: pd.DataFrame, rank_column: str) -> dict[str, Any]:
    labels = frame["label"].map({"active": 1, "decoy": 0}).astype(int).to_numpy()
    return summarize(labels, frame[rank_column].to_numpy(), "lower_is_better")


def _rrf(left_rank: pd.Series, right_rank: pd.Series, k: float) -> pd.Series:
    return 1.0 / (k + left_rank.astype(float)) + 1.0 / (k + right_rank.astype(float))


def _rank_descending_with_primary_tiebreak(
    frame: pd.DataFrame,
    score: pd.Series,
    primary_rank: pd.Series,
) -> pd.Series:
    work = pd.DataFrame(
        {
            "score": score,
            "primary_rank": primary_rank,
            "input_sdf_index": frame["input_sdf_index"].astype(int),
        },
        index=frame.index,
    )
    ordered = work.sort_values(
        ["score", "primary_rank", "input_sdf_index"],
        ascending=[False, True, True],
        kind="stable",
    ).index
    result = pd.Series(index=frame.index, dtype=int)
    result.loc[ordered] = np.arange(1, len(frame) + 1, dtype=int)
    return result.astype(int)


def simulate_fusions(
    frame: pd.DataFrame,
    fractions: Iterable[float] = (0.05, 0.10, 0.20),
    *,
    rrf_k: float = 60.0,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Simulate full fusion and budgeted AF3-first CovDock-LO cascades."""
    if rrf_k <= 0:
        raise ValueError("rrf_k must be positive")
    out = frame.copy()
    out["af3_rank"] = deterministic_order(
        out,
        "af3_score",
        "lower_is_better",
    )
    out["covdock_lo_rank"] = deterministic_order(
        out,
        "covdock_lo_score",
        "lower_is_better",
    )
    out["full_rrf_score"] = _rrf(out["af3_rank"], out["covdock_lo_rank"], rrf_k)
    out["full_rrf_rank"] = _rank_descending_with_primary_tiebreak(
        out, out["full_rrf_score"], out["af3_rank"]
    )

    method_rows: list[dict[str, Any]] = []
    for method, rank_column, fraction, selected_n in (
        ("af3_mpae", "af3_rank", 0.0, 0),
        ("covdock_lo", "covdock_lo_rank", 1.0, len(out)),
        ("full_rrf", "full_rrf_rank", 1.0, len(out)),
    ):
        method_rows.append(
            {
                "method": method,
                "rank_column": rank_column,
                "covdock_fraction": fraction,
                "covdock_selected_n": selected_n,
                **_metrics(out, rank_column),
            }
        )

    for fraction in fractions:
        if not 0 < float(fraction) <= 1:
            raise ValueError(f"invalid fraction: {fraction}")
        selected_n = max(1, math.ceil(len(out) * float(fraction)))
        selected_mask = out["af3_rank"].le(selected_n)
        selected = out.loc[selected_mask].copy()
        selected["_af3_subset_rank"] = deterministic_order(
            selected, "af3_score", "lower_is_better"
        )
        selected["_covdock_subset_rank"] = deterministic_order(
            selected, "covdock_lo_score", "lower_is_better"
        )
        selected["_rrf_score"] = _rrf(
            selected["_af3_subset_rank"], selected["_covdock_subset_rank"], rrf_k
        )
        selected = selected.sort_values(
            ["_rrf_score", "_af3_subset_rank", "input_sdf_index"],
            ascending=[False, True, True],
            kind="stable",
        )
        remainder = out.loc[~selected_mask].sort_values("af3_rank", kind="stable")
        final_order = list(selected.index) + list(remainder.index)
        suffix = f"{int(round(float(fraction) * 100)):02d}pct"
        rrf_rank_column = f"cascade_rrf_{suffix}_rank"
        out[rrf_rank_column] = 0
        out.loc[final_order, rrf_rank_column] = np.arange(1, len(out) + 1, dtype=int)
        out[f"cascade_{suffix}_selected"] = selected_mask
        method_rows.append(
            {
                "method": f"cascade_rrf_{suffix}",
                "rank_column": rrf_rank_column,
                "covdock_fraction": selected_n / len(out),
                "covdock_selected_n": selected_n,
                **_metrics(out, rrf_rank_column),
            }
        )

        gate_selected = out.loc[selected_mask].sort_values(
            ["covdock_lo_success", "af3_rank"],
            ascending=[False, True],
            kind="stable",
        )
        gate_order = list(gate_selected.index) + list(remainder.index)
        gate_rank_column = f"cascade_qc_gate_{suffix}_rank"
        out[gate_rank_column] = 0
        out.loc[gate_order, gate_rank_column] = np.arange(1, len(out) + 1, dtype=int)
        method_rows.append(
            {
                "method": f"cascade_qc_gate_{suffix}",
                "rank_column": gate_rank_column,
                "covdock_fraction": selected_n / len(out),
                "covdock_selected_n": selected_n,
                **_metrics(out, gate_rank_column),
            }
        )

    metrics = pd.DataFrame(method_rows)
    return out, metrics


def select_policy(
    metrics: pd.DataFrame,
    *,
    bedroc_margin: float = 0.03,
    top10_margin: int = 1,
    top25_margin: int = 1,
) -> dict[str, Any]:
    """Select the cheapest eligible cascade using predeclared non-inferiority gates."""
    af3_rows = metrics.loc[metrics["method"].eq("af3_mpae")]
    if len(af3_rows) != 1:
        raise ValueError("metrics must contain exactly one af3_mpae row")
    baseline = af3_rows.iloc[0]
    # Only score-fusion cascades are eligible for algorithm selection. The
    # QC-gate rows are retained as controls, but a no-op success gate must not
    # be mistaken for evidence that CovDock improved the ranking.
    candidates = metrics.loc[
        metrics["method"].str.startswith("cascade_rrf_")
    ].copy()
    candidates["eligible"] = (
        candidates["bedroc_alpha20"].ge(float(baseline["bedroc_alpha20"]) - bedroc_margin)
        & candidates["top10_active"].ge(int(baseline["top10_active"]) - top10_margin)
        & candidates["top25_active"].ge(int(baseline["top25_active"]) - top25_margin)
    )
    eligible = candidates.loc[candidates["eligible"]].sort_values(
        ["covdock_fraction", "bedroc_alpha20", "top10_active", "method"],
        ascending=[True, False, False, True],
        kind="stable",
    )
    if eligible.empty:
        return {
            "status": "NO_ELIGIBLE_CASCADE",
            "selected_method": None,
            "selection_rule": {
                "bedroc_absolute_margin": bedroc_margin,
                "top10_active_margin": top10_margin,
                "top25_active_margin": top25_margin,
            },
            "candidates": candidates.to_dict(orient="records"),
        }
    selected = eligible.iloc[0]
    return {
        "status": "SELECTED",
        "selected_method": str(selected["method"]),
        "rank_column": str(selected["rank_column"]),
        "covdock_fraction": float(selected["covdock_fraction"]),
        "covdock_selected_n": int(selected["covdock_selected_n"]),
        "selection_rule": {
            "objective": "minimum CovDock-LO fraction, then maximum BEDROC20",
            "bedroc_absolute_margin": bedroc_margin,
            "top10_active_margin": top10_margin,
            "top25_active_margin": top25_margin,
        },
        "development_metrics": {
            key: (int(selected[key]) if key.startswith("top") else float(selected[key]))
            for key in (
                "bedroc_alpha20",
                "ef1_percent",
                "ef5_percent",
                "top10_active",
                "top25_active",
                "top50_active",
                "roc_auc",
                "adjusted_logauc",
            )
        },
    }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def freeze_payload(
    policy: dict[str, Any],
    *,
    source_paths: dict[str, Path],
    target_id: str = "BTK_C481",
    rrf_k: float = 60.0,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema_version": "1.0",
        "algorithm_version": ALGORITHM_VERSION,
        "development_target": target_id,
        "policy": policy,
        "rrf_k": rrf_k,
        "score_directions": {
            "af3_mpae": "lower_is_better",
            "covdock_lo_affinity": "lower_is_better",
        },
        "failure_policy": "missing_or_failed_scores_rank_last",
        "tie_break_policy": "input_sdf_index_ascending_label_blind",
        "external_validation_rule": (
            "No cutoff, score direction, fusion constant, failure rule, or selection "
            "criterion may be changed after inspecting EGFR C797 labels."
        ),
        "sources": {
            name: {"path": str(path), "sha256": sha256_file(path)}
            for name, path in source_paths.items()
        },
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["freeze_id"] = f"{ALGORITHM_VERSION}-{hashlib.sha256(canonical).hexdigest()[:12]}"
    return payload
