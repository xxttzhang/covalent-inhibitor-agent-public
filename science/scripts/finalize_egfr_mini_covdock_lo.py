"""Finalize EGFR-mini CovDock-LO scores from partition property audits."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
import pandas as pd


INPUT_INDEX_PATTERN = re.compile(r"__i(\d+)(?:\D|$)")
PRIMARY_SCORE = "r_cdock_cdock_affinity"


def _read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def _input_index(row: dict[str, object]) -> int:
    explicit = row.get("i_sd_INPUT_SDF_INDEX")
    if explicit not in (None, ""):
        return int(explicit)
    match = INPUT_INDEX_PATTERN.search(str(row.get("title", "")))
    if not match:
        raise ValueError(f"Cannot recover input_sdf_index from pose: {row.get('title')!r}")
    return int(match.group(1))


def _load_poses(paths: list[Path]) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    for partition, path in enumerate(paths):
        payload = json.loads(path.read_text(encoding="utf-8"))
        for row in payload.get("rows", []):
            parsed = dict(row)
            parsed["input_sdf_index"] = _input_index(row)
            parsed["covdock_partition"] = partition
            rows.append(parsed)
    if not rows:
        raise ValueError("No CovDock poses were found in the inspection files")
    poses = pd.DataFrame(rows)
    if PRIMARY_SCORE not in poses:
        raise ValueError(f"Required CovDock affinity property is absent: {PRIMARY_SCORE}")
    poses[PRIMARY_SCORE] = pd.to_numeric(poses[PRIMARY_SCORE], errors="coerce")
    return poses


def finalize(identity: pd.DataFrame, poses: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    if identity["input_sdf_index"].duplicated().any():
        raise ValueError("Identity table contains duplicate input_sdf_index")
    identity = identity.copy()
    identity["input_sdf_index"] = pd.to_numeric(
        identity["input_sdf_index"], errors="raise"
    ).astype(int)
    unknown = sorted(set(poses["input_sdf_index"]) - set(identity["input_sdf_index"]))
    if unknown:
        raise ValueError(f"CovDock poses contain unknown input indices: {unknown[:10]}")

    finite = np.isfinite(poses[PRIMARY_SCORE])
    scored = poses.loc[finite].sort_values(
        ["input_sdf_index", PRIMARY_SCORE, "pose_index"],
        ascending=[True, True, True],
        kind="stable",
    )
    selected = scored.drop_duplicates("input_sdf_index", keep="first").copy()
    selected["covdock_pose_count"] = selected["input_sdf_index"].map(
        poses.groupby("input_sdf_index").size()
    )
    selected["raw_score"] = selected[PRIMARY_SCORE]
    selected["covdock_selected_pose_index"] = selected["pose_index"]

    # Identity is authoritative; duplicate identity fields from Maestro are audit-only.
    drop_columns = [
        column
        for column in selected
        if column.startswith("s_sd_") or column == "i_sd_PROTOMER_ID"
    ]
    selected = selected.drop(columns=drop_columns, errors="ignore")
    out = identity.merge(
        selected,
        on="input_sdf_index",
        how="left",
        validate="one_to_one",
        suffixes=("", "_pose"),
    )
    out["engine"] = "schrodinger-covdock-lo"
    out["score_direction"] = "lower_is_better"
    success = np.isfinite(pd.to_numeric(out["raw_score"], errors="coerce"))
    out["engine_status"] = np.where(success, "SUCCESS", "FAILED")
    out["docking_success"] = success
    out["scoring_success"] = success
    out["failure_reason"] = np.where(success, None, "missing_ligprep_pose_or_affinity")

    failures = out.loc[
        ~success,
        [
            column
            for column in (
                "compound_id",
                "protomer_id",
                "input_sdf_index",
                "label",
                "failure_reason",
            )
            if column in out
        ],
    ].copy()
    return out, failures


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--identity-table", type=Path, required=True)
    ap.add_argument("--inspection-json", type=Path, action="append", required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    identities = _read_table(args.identity_table)
    poses = _load_poses(args.inspection_json)
    result, failures = finalize(identities, poses)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(args.output, index=False)
    result.to_csv(args.output.with_suffix(".csv"), index=False)
    failures.to_csv(
        args.output.with_name(f"{args.output.stem}_failures.csv"), index=False
    )
    summary = {
        "n_input": len(result),
        "n_pose_records": len(poses),
        "n_unique_pose_inputs": int(poses["input_sdf_index"].nunique()),
        "n_scored": int(result["engine_status"].eq("SUCCESS").sum()),
        "n_failed": int(result["engine_status"].ne("SUCCESS").sum()),
        "active_failed": int(
            (result["engine_status"].ne("SUCCESS") & result["label"].eq("active")).sum()
        ),
        "decoy_failed": int(
            (result["engine_status"].ne("SUCCESS") & result["label"].eq("decoy")).sum()
        ),
        "primary_score": PRIMARY_SCORE,
        "score_direction": "lower_is_better",
        "output": str(args.output),
    }
    summary_path = args.output.with_name(f"{args.output.stem}_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
