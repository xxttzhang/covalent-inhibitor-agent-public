from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from covscreen.consensus import COMPLETE_CASE_POLICY, fuse_complete_rankings, fuse_rankings


def read_table(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        return pd.read_parquet(path)
    return pd.read_csv(path)


def parse_engine_spec(spec: str) -> tuple[str, Path, str, str]:
    parts = spec.split(":", 3)
    if len(parts) != 4:
        raise ValueError(
            "--engine must be NAME:PATH:SCORE_COLUMN:DIRECTION"
        )
    name, path, score_column, direction = parts
    return name, Path(path), score_column, direction


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library", type=Path, required=True)
    ap.add_argument(
        "--engine",
        action="append",
        required=True,
        help="NAME:PATH:SCORE_COLUMN:DIRECTION",
    )
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--policy", choices=("legacy", COMPLETE_CASE_POLICY), default="legacy",
                    help="Explicit ranking policy; incomplete evidence is unranked under the provisional complete-case policy.")
    args = ap.parse_args()

    library = read_table(args.library)
    identity_columns = ["compound_id", "protomer_id", "input_sdf_index"]
    missing_library_identity = [
        column for column in identity_columns if column not in library.columns
    ]
    if missing_library_identity:
        raise ValueError(
            f"Library is missing identity columns: {missing_library_identity}"
        )
    if library.duplicated(identity_columns).any():
        raise ValueError("Library identity tuple contains duplicates")
    merged = library.copy()
    directions: dict[str, str] = {}
    for spec in args.engine:
        name, path, source_column, direction = parse_engine_spec(spec)
        scores = read_table(path)
        missing_score_identity = [
            column for column in identity_columns if column not in scores.columns
        ]
        if missing_score_identity:
            raise ValueError(
                f"{name} is missing identity columns: {missing_score_identity}"
            )
        if scores.duplicated(identity_columns).any():
            raise ValueError(f"{name} identity tuple contains duplicates")
        if source_column not in scores.columns:
            raise ValueError(f"{name} is missing score column: {source_column}")
        target_column = f"{name}_score"
        scores = scores[identity_columns + [source_column]].rename(
            columns={source_column: target_column}
        )
        merged = merged.merge(
            scores,
            on=identity_columns,
            how="left",
            validate="one_to_one",
        )
        directions[target_column] = direction

    fused = (fuse_complete_rankings if args.policy == COMPLETE_CASE_POLICY else fuse_rankings)(merged, directions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    fused.to_parquet(args.output, index=False)
    fused.to_csv(args.output.with_suffix(".csv"), index=False)
    print(
        {
            "rows": len(fused),
            "policy": args.policy,
            "engines": list(directions),
            "missing_by_engine": {
                column: int(fused[column].isna().sum()) for column in directions
            },
            "output": str(args.output),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
