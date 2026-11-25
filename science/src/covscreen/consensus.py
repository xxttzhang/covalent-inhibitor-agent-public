from __future__ import annotations

from collections.abc import Mapping

import numpy as np
import pandas as pd


VALID_DIRECTIONS = {"higher_is_better", "lower_is_better"}
COMPLETE_CASE_POLICY = "rrf-finite-complete-case-v2"


def fuse_complete_rankings(
    frame: pd.DataFrame,
    score_directions: Mapping[str, str],
    rrf_k: float = 60.0,
) -> pd.DataFrame:
    """Versioned offline policy: fuse only candidates with all finite scores.

    Historical ``fuse_rankings`` remains unchanged for frozen-plan replay.
    Per-engine diagnostic ranks cover finite results; fusion component ranks
    are recomputed on the same complete-case universe for every engine.
    Partial/unmeasured candidates remain in the output, without a fused rank.
    This is a data-quality policy, not a calibrated activity predictor.
    """
    if not score_directions:
        raise ValueError("At least one score column is required")
    if isinstance(rrf_k, (bool, np.bool_)) or not isinstance(rrf_k, (int, float)) or not np.isfinite(rrf_k) or rrf_k < 0:
        raise ValueError("rrf_k must be a finite nonnegative number")
    stems = [column.removesuffix("_score") for column in score_directions]
    if len(set(stems)) != len(stems):
        raise ValueError("Score columns must have distinct output stems")
    out = frame.copy()
    # Use a positional internal index so duplicate caller indices stay safe.
    numeric = pd.DataFrame(index=pd.RangeIndex(len(out)))
    for column, direction in score_directions.items():
        if column not in out:
            raise KeyError(column)
        if direction not in VALID_DIRECTIONS:
            raise ValueError(f"Unknown score direction: {direction}")
        values = pd.to_numeric(out[column], errors="coerce").to_numpy(dtype=float, na_value=np.nan)
        numeric[column] = np.where(np.isfinite(values), values, np.nan)
        stem = column.removesuffix("_score")
        out[f"{stem}_measurement_status"] = np.where(
            np.isfinite(values), "VALID",
            np.where(out[column].isna().to_numpy(), "MISSING", "INVALID_OR_NONFINITE"),
        )
        ranks = numeric[column].rank(method="average", ascending=direction == "lower_is_better")
        out[f"{stem}_rank"] = ranks.to_numpy()
    count = numeric.notna().sum(axis=1)
    complete = count.eq(len(score_directions))
    score = pd.Series(np.nan, index=numeric.index, dtype=float)
    score.loc[complete] = 0.0
    for column, direction in score_directions.items():
        ranks = numeric.loc[complete, column].rank(method="average", ascending=direction == "lower_is_better")
        component = pd.Series(np.nan, index=numeric.index, dtype=float)
        component.loc[complete] = ranks
        out[f"{column.removesuffix('_score')}_fusion_rank"] = component.to_numpy()
        score.loc[complete] += 1.0 / (rrf_k + ranks)
    out["n_engines_success"] = count.to_numpy()
    out["evidence_coverage"] = np.where(complete, "COMPLETE", np.where(count.eq(0), "NONE", "PARTIAL"))
    out["consensus_eligible"] = complete.to_numpy()
    out["consensus_score"] = score.to_numpy()
    out["consensus_rank"] = score.rank(method="min", ascending=False).to_numpy()
    out["consensus_policy"] = COMPLETE_CASE_POLICY
    out["thresholds_calibrated"] = False
    return out


def rank_scores(
    values: pd.Series,
    direction: str,
) -> tuple[pd.Series, pd.Series]:
    """Rank finite scores and conservatively place failures at the bottom."""
    if direction not in VALID_DIRECTIONS:
        raise ValueError(f"Unknown score direction: {direction}")
    numeric = pd.to_numeric(values, errors="coerce")
    ascending = direction == "lower_is_better"
    rank = numeric.rank(method="first", ascending=ascending, na_option="bottom")
    n = max(1, len(rank))
    percentile = 1.0 - (rank - 1.0) / n
    return rank.astype(int), percentile.astype(float)


def fuse_rankings(
    frame: pd.DataFrame,
    score_directions: Mapping[str, str],
    rrf_k: float = 60.0,
) -> pd.DataFrame:
    """Create per-engine ranks and reciprocal-rank fusion without imputation."""
    if not score_directions:
        raise ValueError("At least one score column is required")
    out = frame.copy()
    rank_columns = []
    percentile_columns = []
    for score_column, direction in score_directions.items():
        if score_column not in out:
            raise KeyError(score_column)
        stem = score_column.removesuffix("_score")
        rank_column = f"{stem}_rank"
        percentile_column = f"{stem}_percentile"
        out[rank_column], out[percentile_column] = rank_scores(
            out[score_column], direction
        )
        rank_columns.append(rank_column)
        percentile_columns.append(percentile_column)
    out["consensus_score"] = sum(
        1.0 / (rrf_k + out[column]) for column in rank_columns
    )
    out["consensus_rank"] = out["consensus_score"].rank(
        method="first", ascending=False
    ).astype(int)

    finite = np.column_stack(
        [pd.to_numeric(out[column], errors="coerce").notna() for column in score_directions]
    )
    out["n_engines_success"] = finite.sum(axis=1)
    max_percentile = out[percentile_columns].max(axis=1)
    min_percentile = out[percentile_columns].min(axis=1)
    out["conflict_type"] = np.select(
        [
            out["n_engines_success"] < len(score_directions),
            (max_percentile >= 0.9) & (min_percentile <= 0.1),
            min_percentile >= 0.8,
            max_percentile <= 0.2,
        ],
        [
            "MISSING_ENGINE",
            "HIGH_DISAGREEMENT",
            "CONSISTENT_TOP",
            "CONSISTENT_LOW",
        ],
        default="MIXED",
    )
    return out
