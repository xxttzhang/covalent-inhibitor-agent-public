from __future__ import annotations

from itertools import combinations
from typing import Any

import pandas as pd


IDENTITY_COLUMNS = ["compound_id", "protomer_id", "input_sdf_index"]


def _json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if pd.isna(value):
        return None
    return str(value)


def _top_rows(frame: pd.DataFrame, rank_column: str, score_column: str, n: int) -> list[dict[str, Any]]:
    columns = [
        column
        for column in (*IDENTITY_COLUMNS, score_column, rank_column)
        if column in frame.columns
    ]
    selected = frame.sort_values(rank_column, kind="stable").head(n)
    return [
        {column: _json_value(row[column]) for column in columns}
        for _, row in selected[columns].iterrows()
    ]


def analyze_consensus(frame: pd.DataFrame, score_columns: list[str], top_ns: tuple[int, ...] = (10, 25, 50)) -> dict[str, Any]:
    """Summarize completeness, rank agreement, and disagreement in a fused table."""
    missing_identity = [column for column in IDENTITY_COLUMNS if column not in frame.columns]
    if missing_identity:
        raise ValueError(f"Fused table is missing identity columns: {missing_identity}")
    if not score_columns:
        raise ValueError("At least one score column is required")

    result: dict[str, Any] = {
        "rows": int(len(frame)),
        "engines": [column.removesuffix("_score") for column in score_columns],
        "score_columns": score_columns,
        "missing_by_engine": {},
        "conflict_type_counts": {},
        "pairwise_spearman": {},
        "top_n_overlap": {},
        "top_lists": {},
    }

    if "conflict_type" in frame:
        counts = frame["conflict_type"].value_counts(dropna=False)
        result["conflict_type_counts"] = {
            str(key): int(value) for key, value in counts.items()
        }

    rank_columns: dict[str, str] = {}
    for score_column in score_columns:
        if score_column not in frame:
            raise KeyError(score_column)
        rank_column = f"{score_column.removesuffix('_score')}_rank"
        if rank_column not in frame:
            raise KeyError(rank_column)
        rank_columns[score_column] = rank_column
        result["missing_by_engine"][score_column] = int(
            pd.to_numeric(frame[score_column], errors="coerce").isna().sum()
        )
        result["top_lists"][score_column] = {
            str(n): _top_rows(frame, rank_column, score_column, n)
            for n in top_ns
        }

    for left, right in combinations(score_columns, 2):
        pair = frame[[left, right]].apply(pd.to_numeric, errors="coerce").dropna()
        key = f"{left}__{right}"
        result["pairwise_spearman"][key] = {
            "n_common": int(len(pair)),
            "rho": float(pair[left].corr(pair[right], method="spearman"))
            if len(pair) >= 2
            else None,
        }
        for n in top_ns:
            left_ids = set(
                frame.loc[frame[rank_columns[left]] <= n, "input_sdf_index"].dropna().astype(int)
            )
            right_ids = set(
                frame.loc[frame[rank_columns[right]] <= n, "input_sdf_index"].dropna().astype(int)
            )
            union = left_ids | right_ids
            result["top_n_overlap"].setdefault(str(n), {})[f"{left}__{right}"] = {
                "left_count": len(left_ids),
                "right_count": len(right_ids),
                "intersection_count": len(left_ids & right_ids),
                "jaccard": len(left_ids & right_ids) / len(union) if union else None,
            }

    if "conflict_type" in frame:
        disagreement = frame.loc[frame["conflict_type"].eq("HIGH_DISAGREEMENT")].copy()
        disagreement = disagreement.sort_values(
            ["consensus_rank"], kind="stable"
        ) if "consensus_rank" in disagreement else disagreement
        display_columns = [
            column
            for column in (
                *IDENTITY_COLUMNS,
                "consensus_rank",
                "conflict_type",
                *score_columns,
                *(rank_columns.values()),
            )
            if column in disagreement.columns
        ]
        result["high_disagreement_examples"] = [
            {column: _json_value(row[column]) for column in display_columns}
            for _, row in disagreement.head(50)[display_columns].iterrows()
        ]
    return result
