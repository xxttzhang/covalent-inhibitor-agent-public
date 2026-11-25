"""Freeze label-blind score files before an answer key may be revealed."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def inspect_score_file(path: Path, expected_rows: int | None = None) -> dict[str, object]:
    if not path.is_file():
        raise FileNotFoundError(path)
    frame = read_table(path)
    required = {"input_sdf_index", "raw_score", "engine_status", "failure_reason"}
    missing = sorted(required - set(frame.columns))
    if missing:
        raise ValueError(f"{path} is missing columns: {missing}")
    if frame["input_sdf_index"].duplicated().any():
        raise ValueError(f"{path} contains duplicate input_sdf_index values")
    if expected_rows is not None and len(frame) != expected_rows:
        raise ValueError(f"{path}: expected {expected_rows} rows, found {len(frame)}")
    for column in frame.select_dtypes(include=["object", "string"]).columns:
        values = set(frame[column].dropna().astype(str).str.lower().unique())
        if values & {"active", "decoy"}:
            raise ValueError(f"{path} exposes class labels in column {column}")
    scores = pd.to_numeric(frame["raw_score"], errors="coerce")
    status = frame["engine_status"].astype(str)
    if not set(status).issubset({"SUCCESS", "FAILED"}):
        raise ValueError(f"{path} has invalid engine_status")
    success = status.eq("SUCCESS")
    failed = status.eq("FAILED")
    if scores.loc[success].isna().any() or ~np.isfinite(scores.loc[success]).all():
        raise ValueError(f"{path} has SUCCESS rows without finite raw_score")
    if frame.loc[success, "failure_reason"].notna().any() and frame.loc[success, "failure_reason"].astype(str).str.strip().ne("").any():
        raise ValueError(f"{path} has SUCCESS rows with failure_reason")
    if scores.loc[failed].notna().any():
        raise ValueError(f"{path} has FAILED rows with raw_score")
    if frame.loc[failed, "failure_reason"].isna().any() or frame.loc[failed, "failure_reason"].astype(str).str.strip().eq("").any():
        raise ValueError(f"{path} has FAILED rows without failure_reason")
    return {
        "path": str(path),
        "sha256": sha256_file(path),
        "rows": len(frame),
        "unique_inputs": int(frame["input_sdf_index"].nunique()),
        "finite_scores": int(np.isfinite(scores).sum()),
        "successful_rows": int(success.sum()),
        "failed_rows": int(failed.sum()),
        "failure_reason_counts": frame.loc[failed, "failure_reason"].astype(str).value_counts().to_dict(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--panel-config", type=Path, required=True)
    parser.add_argument("--results-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    panel = json.loads(args.panel_config.read_text())
    expected_rows = int(panel["dataset"]["rows_per_target"])
    records = []
    for target in panel["targets"]:
        target_id = str(target["target_id"])
        root = args.results_root / target_id
        files = {
            "af3": root / "af3_scores.parquet",
            "covdock_lo": root / "covdock_lo_scores.parquet",
            "rtmscore": root / "rtmscore.parquet",
        }
        inspected = {
            name: inspect_score_file(path, expected_rows if name == "af3" else None)
            for name, path in files.items()
        }
        records.append({"target_id": target_id, "scores": inspected})
    payload = {
        "schema_version": 1,
        "status": "PREDICTIONS_FROZEN_BEFORE_UNBLINDING",
        "panel_id": panel["panel_id"],
        "panel_config": str(args.panel_config),
        "panel_config_sha256": sha256_file(args.panel_config),
        "targets": records,
        "labels_present": False,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    payload["prediction_lock_id"] = hashlib.sha256(canonical).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"status": payload["status"], "prediction_lock_id": payload["prediction_lock_id"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
