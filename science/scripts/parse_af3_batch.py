from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd


def _find_job_dir(results_dir: Path, job_name: str) -> Path | None:
    expected_model = f"{job_name}_model.cif"
    matches = sorted(results_dir.rglob(expected_model))
    if len(matches) > 1:
        raise ValueError(
            f"Multiple AF3 model files found for {job_name}: {matches}"
        )
    return matches[0].parent if matches else None


def _parquet_safe(value: object) -> object:
    """Serialize nested manifest metadata without losing it in Parquet."""
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return value


def parse_batch(
    manifests_dir: Path | None,
    results_dir: Path,
    target_config: Path,
    *,
    batch_manifest: Path | None = None,
) -> pd.DataFrame:
    rows: list[dict[str, object]] = []
    if batch_manifest is not None:
        identities = json.loads(batch_manifest.read_text())
        if not isinstance(identities, list) or not identities:
            raise ValueError(f"AF3 batch manifest must be a non-empty list: {batch_manifest}")
    else:
        if manifests_dir is None:
            raise ValueError("Either manifests_dir or batch_manifest is required")
        manifest_paths = sorted(manifests_dir.glob("*.manifest.json"))
        if not manifest_paths:
            raise ValueError(f"No AF3 manifests found in {manifests_dir}")
        identities = [json.loads(path.read_text()) for path in manifest_paths]
    for identity in identities:
        job_name = str(identity["job_name"])
        row: dict[str, object] = {
            key: _parquet_safe(value) for key, value in identity.items()
        }
        preparation_status = str(identity.get("status", "SUCCESS"))
        preparation_failure = identity.get("failure_reason")
        row["preparation_status"] = preparation_status
        row["preparation_failure_reason"] = _parquet_safe(preparation_failure)
        initial_failure = "missing_af3_output"
        if preparation_status != "SUCCESS":
            initial_failure = f"preparation_failed:{preparation_failure}"
        row.update(
            {
                "engine": "alphafold3",
                "raw_score": np.nan,
                "score_direction": "lower_is_better",
                "engine_status": "FAILED",
                "failure_reason": initial_failure,
            }
        )
        # Stale output files cannot turn failed input preparation into success.
        # Preserve the original failure and denominator without parsing them.
        job_dir = _find_job_dir(results_dir, job_name) if preparation_status == "SUCCESS" else None
        if job_dir is not None:
            try:
                from covscreen.af3_results import parse_af3_job

                parsed = parse_af3_job(
                    job_dir,
                    ligand_smiles=str(identity["standardized_smiles"]),
                    target_config=target_config,
                    warhead_class=identity.get("warhead_class"),
                )
            except Exception as exc:
                row["failure_reason"] = (
                    f"af3_result_parse_failed:{type(exc).__name__}:{exc}"
                )
            else:
                row.update(parsed)
                row["raw_score"] = parsed["mpae"]
                row["score_direction"] = "lower_is_better"
                row["engine_status"] = "SUCCESS"
                row["failure_reason"] = None
                row["job_dir"] = str(job_dir)
        rows.append(row)
    result = pd.DataFrame(rows)
    if "input_sdf_index" in result:
        result["input_sdf_index"] = pd.to_numeric(
            result["input_sdf_index"], errors="raise"
        ).astype("int64")
    identity_columns = [
        column
        for column in ("compound_id", "protomer_id", "input_sdf_index")
        if column in result
    ]
    if result.duplicated(identity_columns).any():
        raise ValueError(f"Duplicate AF3 identities: {identity_columns}")
    return result


def main() -> int:
    ap = argparse.ArgumentParser()
    source = ap.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifests-dir", type=Path)
    source.add_argument(
        "--batch-manifest",
        type=Path,
        help="JSON list written by prepare_af3_covalent_batch.py",
    )
    ap.add_argument("--results-dir", type=Path, required=True)
    ap.add_argument(
        "--target-config",
        type=Path,
        default=Path("configs/targets/BTK.yaml"),
    )
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    frame = parse_batch(
        args.manifests_dir,
        args.results_dir,
        args.target_config,
        batch_manifest=args.batch_manifest,
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
                "output": str(args.output),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
