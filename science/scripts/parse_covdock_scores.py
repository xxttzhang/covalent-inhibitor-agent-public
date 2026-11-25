from __future__ import annotations

import argparse
import json
import hashlib
import re
from pathlib import Path
from collections.abc import Sequence

import numpy as np
import pandas as pd


INPUT_INDEX_PATTERN = re.compile(r"__i(\d+)(?:\D|$)")


def _read_table(path: Path) -> pd.DataFrame:
    return (
        pd.read_parquet(path)
        if path.suffix.lower() == ".parquet"
        else pd.read_csv(path)
    )


def _identity_index(value: object) -> int | None:
    match = INPUT_INDEX_PATTERN.search(str(value))
    return int(match.group(1)) if match else None


def parse_inspection(
    inspection_json: Path | Sequence[Path],
    identity_table: Path,
    score_property: str,
    score_direction: str,
    identity_property: str | None = None,
    engine: str = "schrodinger-covdock-vs",
) -> pd.DataFrame:
    inspection_paths = (
        [inspection_json]
        if isinstance(inspection_json, Path)
        else list(inspection_json)
    )
    pose_rows: list[dict[str, object]] = []
    for path in inspection_paths:
        inspection = json.loads(path.read_text())
        # Pose indices are local to a structure file, not globally unique.
        # Preserve the source when merging reaction groups/chunks so later
        # coordinate re-evaluation can reopen the exact selected structure.
        audit_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
        for pose in inspection.get("rows", []):
            pose_rows.append({
                **pose,
                "covdock_pose_file": inspection.get("input"),
                "covdock_inspection_file": str(path.resolve()),
                "covdock_inspection_sha256": audit_sha256,
            })
    identities = _read_table(identity_table).copy()
    if identities["input_sdf_index"].duplicated().any():
        raise ValueError("Identity table contains duplicate input_sdf_index")

    parsed_rows: list[dict[str, object]] = []
    for pose in pose_rows:
        identity_value = (
            pose.get(identity_property)
            if identity_property
            else pose.get("title")
        )
        input_index = _identity_index(identity_value)
        parsed = dict(pose)
        parsed.update(
            {
                "input_sdf_index": input_index,
                "raw_score": pd.to_numeric(
                    pose.get(score_property), errors="coerce"
                ),
                "covdock_pose_index": pose.get("pose_index"),
                "covdock_title": pose.get("title"),
                "covdock_score_property": score_property,
            }
        )
        parsed_rows.append(parsed)
    poses = pd.DataFrame(parsed_rows)
    if poses.empty:
        poses = pd.DataFrame(
            columns=[
                "input_sdf_index",
                "raw_score",
                "covdock_pose_index",
                "covdock_title",
                "covdock_score_property",
            ]
        )
    unresolved = poses["input_sdf_index"].isna()
    if unresolved.any():
        examples = poses.loc[unresolved, "covdock_title"].head(5).tolist()
        raise ValueError(
            "CovDock identities cannot be resolved safely from the selected "
            f"field; examples: {examples}. Re-run with --identity-property."
        )
    poses["input_sdf_index"] = poses["input_sdf_index"].astype(int)
    # Nonfinite values must not win a best-pose sort (e.g. -inf for lower-is-better)
    # or turn a missing/invalid score into a successful engine row.
    numeric_scores = pd.to_numeric(poses["raw_score"], errors="coerce")
    poses["raw_score"] = numeric_scores.where(np.isfinite(numeric_scores))
    poses = poses.sort_values(
        ["input_sdf_index", "raw_score"],
        ascending=[True, score_direction == "lower_is_better"],
        na_position="last",
    ).drop_duplicates("input_sdf_index", keep="first")
    out = identities.merge(
        poses, on="input_sdf_index", how="left", validate="one_to_one"
    )
    out["engine"] = engine
    out["score_direction"] = score_direction
    out["engine_status"] = np.where(
        pd.to_numeric(out["raw_score"], errors="coerce").notna(),
        "SUCCESS",
        "FAILED",
    )
    out["failure_reason"] = np.where(
        out["engine_status"].eq("SUCCESS"),
        None,
        "missing_pose_or_finite_score",
    )
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--inspection-json",
        type=Path,
        action="append",
        required=True,
        help="repeat for multiple CovDock chunk audits",
    )
    ap.add_argument("--identity-table", type=Path, required=True)
    ap.add_argument("--score-property", required=True)
    ap.add_argument(
        "--score-direction",
        choices=["higher_is_better", "lower_is_better"],
        required=True,
    )
    ap.add_argument("--identity-property")
    ap.add_argument("--engine", default="schrodinger-covdock-vs")
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    frame = parse_inspection(
        args.inspection_json,
        args.identity_table,
        args.score_property,
        args.score_direction,
        args.identity_property,
        args.engine,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(args.output, index=False)
    frame.to_csv(args.output.with_suffix(".csv"), index=False)
    failures = frame.loc[
        frame["engine_status"].ne("SUCCESS"),
        [
            column
            for column in (
                "compound_id",
                "protomer_id",
                "input_sdf_index",
                "label",
                "failure_reason",
            )
            if column in frame
        ],
    ]
    failures.to_csv(
        args.output.with_name(f"{args.output.stem}_failures.csv"), index=False
    )
    print(
        json.dumps(
            {
                "rows": len(frame),
                "success": int(frame["engine_status"].eq("SUCCESS").sum()),
                "failed": int(frame["engine_status"].ne("SUCCESS").sum()),
                "inspection_json": [str(path) for path in args.inspection_json],
                "score_property": args.score_property,
                "score_direction": args.score_direction,
                "output": str(args.output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
