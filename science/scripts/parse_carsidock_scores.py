from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd
from rdkit import Chem

from covscreen.carsidock_results import (
    audit_carsidock_outputs,
    pose_name,
    resolve_pose_compound_id,
)


def molecules(path: Path) -> list[Chem.Mol]:
    if not path.is_file():
        return []
    return [
        molecule
        for molecule in Chem.SDMolSupplier(
            str(path), removeHs=False, sanitize=False
        )
        if molecule is not None
    ]


def parse_chunk(
    chunk_table: Path,
    chunk_sdf: Path,
    output_prefix: Path,
) -> tuple[pd.DataFrame, dict[str, object]]:
    table = pd.read_parquet(chunk_table)
    inputs = molecules(chunk_sdf)
    poses = molecules(output_prefix.with_suffix(".sdf"))
    audit = audit_carsidock_outputs(output_prefix, chunk_sdf)
    compound_ids = [
        molecule.GetProp("COMPOUND_ID")
        for molecule in inputs
        if molecule.HasProp("COMPOUND_ID")
    ]
    identity_mapping_available = bool(audit["identity_mapping_available"])
    grouped_poses: dict[str, list[tuple[int, Chem.Mol, str]]] = {
        compound_id: [] for compound_id in compound_ids
    }
    if identity_mapping_available:
        for record_index, pose in enumerate(poses):
            variant_id = pose_name(pose)
            compound_id = resolve_pose_compound_id(variant_id, compound_ids)
            if compound_id is not None:
                grouped_poses[compound_id].append((record_index, pose, variant_id))

    poses_per_input = audit["poses_per_input"]
    score_rows = []
    for position, input_molecule in enumerate(inputs):
        input_index = int(input_molecule.GetProp("INPUT_SDF_INDEX"))
        compound_id = input_molecule.GetProp("COMPOUND_ID")
        candidates = grouped_poses.get(compound_id, [])
        if not identity_mapping_available and poses_per_input:
            start = position * int(poses_per_input)
            candidates = [
                (record_index, poses[record_index], pose_name(poses[record_index]))
                for record_index in range(
                    start, min(start + int(poses_per_input), len(poses))
                )
            ]
        scored = []
        for record_index, pose, variant_id in candidates:
            raw_score = (
                pd.to_numeric(pose.GetProp("CarsiScore"), errors="coerce")
                if pose.HasProp("CarsiScore")
                else np.nan
            )
            if np.isfinite(raw_score):
                scored.append((float(raw_score), record_index, pose, variant_id))
        selected = min(scored, key=lambda item: item[0]) if scored else None
        score_rows.append(
            {
                "input_sdf_index": input_index,
                "sdf_compound_id": compound_id,
                "raw_score": selected[0] if selected else np.nan,
                "pose_loss": (
                    float(selected[2].GetProp("loss"))
                    if selected and selected[2].HasProp("loss")
                    else np.nan
                ),
                "pose_mdn_score": (
                    float(selected[2].GetProp("score"))
                    if selected and selected[2].HasProp("score")
                    else np.nan
                ),
                "selected_pose_record_index": selected[1] if selected else pd.NA,
                "pose_variant_id": selected[3] if selected else None,
                "pose_count": len(candidates),
                "protomer_count": len({item[2] for item in candidates}),
            }
        )
    score_frame = pd.DataFrame(score_rows)
    if score_frame.empty:
        score_frame = pd.DataFrame(
            columns=[
                "input_sdf_index",
                "sdf_compound_id",
                "raw_score",
                "pose_loss",
                "pose_mdn_score",
                "selected_pose_record_index",
                "pose_variant_id",
                "pose_count",
                "protomer_count",
            ]
        )
    out = table.merge(
        score_frame,
        on="input_sdf_index",
        how="left",
        validate="one_to_one",
    )
    identity_mismatch = (
        out["sdf_compound_id"].notna()
        & out["sdf_compound_id"].astype(str).ne(out["compound_id"].astype(str))
    )
    if identity_mismatch.any():
        bad = out.loc[
            identity_mismatch,
            ["input_sdf_index", "compound_id", "sdf_compound_id"],
        ]
        raise ValueError(f"CarsiDock identity mismatch:\n{bad}")
    finite_carsidock_scores = np.isfinite(pd.to_numeric(
        out["raw_score"], errors="coerce"
    ))
    out["engine"] = "carsidock-cov"
    out["score_direction"] = "lower_is_better"
    out["engine_status"] = np.where(
        finite_carsidock_scores,
        "SUCCESS",
        "FAILED",
    )
    out["failure_reason"] = np.where(
        out["engine_status"].eq("SUCCESS"),
        None,
        np.where(
            ~out["input_sdf_index"].isin(
                {int(m.GetProp("INPUT_SDF_INDEX")) for m in inputs}
            ),
            "chemistry_preparation_failed_before_docking",
            np.where(
                pd.to_numeric(out["pose_count"], errors="coerce").fillna(0).eq(0),
                "carsidock_docking_output_incomplete",
                "missing_or_invalid_carsiscore",
            ),
        ),
    )
    out["pose_file"] = str(output_prefix.with_suffix(".sdf"))
    out["chunk"] = chunk_table.stem
    return out, audit


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunks-dir", type=Path, required=True)
    ap.add_argument("--results-dir", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    args = ap.parse_args()

    frames = []
    audits = {}
    for chunk_table in sorted(args.chunks_dir.glob("chunk_[0-9][0-9][0-9][0-9].parquet")):
        stem = chunk_table.stem
        frame, audit = parse_chunk(
            chunk_table,
            args.chunks_dir / f"{stem}.sdf",
            args.results_dir / stem,
        )
        frames.append(frame)
        audits[stem] = audit
    if not frames:
        raise ValueError("No chunk tables found")
    merged = pd.concat(frames, ignore_index=True)
    if merged["input_sdf_index"].duplicated().any():
        raise ValueError("Duplicate input_sdf_index after chunk merge")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    merged.to_parquet(args.output, index=False)
    merged.to_csv(args.output.with_suffix(".csv"), index=False)
    failures = merged.loc[
        merged["engine_status"].ne("SUCCESS"),
        [
            "compound_id",
            "input_sdf_index",
            "label",
            "chunk",
            "failure_reason",
        ],
    ].copy()
    failures["engine"] = "carsidock-cov"
    failures["stage"] = np.where(
        failures["failure_reason"].eq(
            "chemistry_preparation_failed_before_docking"
        ),
        "chemistry_preparation",
        "pose_scoring",
    )
    failures.to_csv(
        args.output.with_name(f"{args.output.stem}_failures.csv"), index=False
    )
    audit_path = args.output.with_name(f"{args.output.stem}_audit.json")
    audit_path.write_text(json.dumps(audits, indent=2) + "\n")
    print(
        {
            "rows": len(merged),
            "success": int(merged["engine_status"].eq("SUCCESS").sum()),
            "failed": int(merged["engine_status"].ne("SUCCESS").sum()),
            "output": str(args.output),
            "audit": str(audit_path),
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
