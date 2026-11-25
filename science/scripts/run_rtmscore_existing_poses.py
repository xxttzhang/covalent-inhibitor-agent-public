from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pandas as pd
from rdkit import Chem


def normalize_identity_columns(scores: pd.DataFrame) -> pd.DataFrame:
    """Add discovery-safe values for columns that exist only in benchmarks."""
    scores = scores.copy()
    defaults = {
        "label": "unknown",
        "protomer_id": "0",
        "parent_id": None,
        "matched_active_id": "",
    }
    for column, default in defaults.items():
        if column not in scores:
            scores[column] = scores["compound_id"] if column == "parent_id" else default
    return scores


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _load_selected_poses(
    frame: pd.DataFrame,
    project_root: Path,
) -> list[Chem.Mol]:
    suppliers: dict[Path, list[Chem.Mol | None]] = {}
    selected: list[Chem.Mol] = []
    for row in frame.itertuples(index=False):
        pose_path = Path(row.pose_file)
        if not pose_path.is_absolute():
            pose_path = project_root / pose_path
        if pose_path not in suppliers:
            suppliers[pose_path] = list(
                Chem.SDMolSupplier(str(pose_path), removeHs=False)
            )
        record_index = int(row.selected_pose_record_index)
        records = suppliers[pose_path]
        if record_index < 0 or record_index >= len(records):
            raise IndexError(
                f"Pose index {record_index} is outside {pose_path} "
                f"with {len(records)} records"
            )
        molecule = records[record_index]
        if molecule is None:
            raise ValueError(f"Unreadable pose {record_index} in {pose_path}")
        selected.append(molecule)
    return selected


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Run RTMScore on identity-mapped existing CarsiDock poses."
    )
    ap.add_argument("--project-root", type=Path, default=Path.cwd())
    ap.add_argument("--carsidock-repo", type=Path, required=True)
    ap.add_argument("--checkpoint", type=Path, required=True)
    ap.add_argument("--pdb", type=Path, required=True)
    ap.add_argument("--reference-ligand", type=Path, required=True)
    ap.add_argument("--carsidock-scores", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--cuda-device-index", type=int, default=0)
    ap.add_argument("--max-ligands", type=int)
    args = ap.parse_args()

    args.project_root = args.project_root.resolve()
    args.carsidock_repo = args.carsidock_repo.resolve()
    args.checkpoint = args.checkpoint.resolve()
    args.pdb = args.pdb.resolve()
    args.reference_ligand = args.reference_ligand.resolve()
    args.carsidock_scores = args.carsidock_scores.resolve()
    args.output = args.output.resolve()

    for path in (
        args.carsidock_repo,
        args.checkpoint,
        args.pdb,
        args.reference_ligand,
        args.carsidock_scores,
    ):
        if not path.exists():
            raise SystemExit(f"Missing required input: {path}")

    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.cuda_device_index)
    sys.path.insert(0, str(args.carsidock_repo))
    from RTMScore.utils import get_rtmscore_model, scoring
    from src.utils.docking_utils import extract_pocket

    scores = pd.read_parquet(args.carsidock_scores)
    required = {
        "input_sdf_index",
        "compound_id",
        "engine_status",
        "pose_file",
        "selected_pose_record_index",
    }
    missing = sorted(required - set(scores.columns))
    if missing:
        raise SystemExit(f"CarsiDock score table is missing columns: {missing}")
    # Discovery libraries do not have benchmark-only labels or protomer groups.
    # Add explicit neutral values so the same registered adapter can process both
    # labelled benchmarks and prospective CovalentInDB candidates.
    scores = normalize_identity_columns(scores)
    if scores["input_sdf_index"].duplicated().any():
        raise SystemExit("CarsiDock score table contains duplicate input_sdf_index values")

    selected_rows = scores.loc[
        scores["engine_status"].eq("SUCCESS")
        & scores["selected_pose_record_index"].notna()
        & scores["pose_file"].notna()
    ].copy()
    selected_rows = selected_rows.sort_values("input_sdf_index")
    if args.max_ligands is not None:
        selected_rows = selected_rows.head(args.max_ligands)
    if selected_rows.empty:
        raise SystemExit("No successful identity-mapped CarsiDock poses found")

    poses = _load_selected_poses(selected_rows, args.project_root)
    reference = next(
        (
            molecule
            for molecule in Chem.SDMolSupplier(
                str(args.reference_ligand), removeHs=False
            )
            if molecule is not None
        ),
        None,
    )
    if reference is None:
        raise SystemExit(f"Cannot read reference ligand: {args.reference_ligand}")
    heavy_positions = [
        reference.GetConformer().GetAtomPosition(atom.GetIdx())
        for atom in reference.GetAtoms()
        if atom.GetAtomicNum() > 1
    ]
    import numpy as np

    pocket = extract_pocket(
        str(args.pdb),
        np.asarray([[p.x, p.y, p.z] for p in heavy_positions]),
        distance=10,
        del_water=True,
    )

    start = time.monotonic()
    model = get_rtmscore_model(str(args.checkpoint), device="cuda")
    ids, predictions = scoring(pocket, poses, model, device="cuda")
    runtime_seconds = time.monotonic() - start
    predictions = list(predictions)
    if len(predictions) != len(selected_rows):
        raise ValueError(
            f"RTMScore returned {len(predictions)} predictions for "
            f"{len(selected_rows)} poses"
        )

    predictions_frame = selected_rows[
        ["input_sdf_index", "compound_id", "protomer_id", "label"]
    ].copy()
    predictions_frame["rtmscore_dataset_id"] = list(ids)
    predictions_frame["raw_score"] = predictions

    identity_columns = [
        column
        for column in (
            "standardized_smiles",
            "label",
            "target_id",
            "site_id",
            "compound_id",
            "parent_id",
            "protomer_id",
            "matched_active_id",
            "source_id",
            "source_protomer_id",
            "source_row",
            "source_sheet",
            "input_sdf_index",
        )
        if column in scores
    ]
    output = scores[identity_columns].copy()
    output["source_carsidock_status"] = scores["engine_status"]
    output["source_carsiscore"] = pd.to_numeric(
        scores["raw_score"], errors="coerce"
    )
    output = output.merge(
        predictions_frame[
            ["input_sdf_index", "rtmscore_dataset_id", "raw_score"]
        ],
        on="input_sdf_index",
        how="left",
        validate="one_to_one",
    )
    output["engine"] = "rtmscore"
    output["score_direction"] = "higher_is_better"
    output["engine_status"] = output["raw_score"].notna().map(
        {True: "SUCCESS", False: "FAILED"}
    )
    selected_indices = set(selected_rows["input_sdf_index"].astype(int))
    upstream_success = scores["engine_status"].eq("SUCCESS")
    output["failure_reason"] = None
    output.loc[output["engine_status"].eq("FAILED"), "failure_reason"] = (
        "upstream_carsidock_pose_unavailable"
    )
    output.loc[
        output["engine_status"].eq("FAILED")
        & upstream_success
        & ~output["input_sdf_index"].astype(int).isin(selected_indices),
        "failure_reason",
    ] = "not_selected_by_max_ligands"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_csv(args.output, index=False)
    output.to_parquet(args.output.with_suffix(".parquet"), index=False)

    repo_head = subprocess.run(
        ["git", "-C", str(args.carsidock_repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    repo_diff = subprocess.run(
        ["git", "-C", str(args.carsidock_repo), "diff", "--binary"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    manifest = {
        "engine": "rtmscore",
        "status": "SUCCESS",
        "score_direction": "higher_is_better",
        "input_carsidock_scores": str(args.carsidock_scores),
        "input_pose_count": len(selected_rows),
        "output_row_count": len(output),
        "scored_row_count": int(output["engine_status"].eq("SUCCESS").sum()),
        "failed_row_count": int(output["engine_status"].ne("SUCCESS").sum()),
        "output_csv": str(args.output),
        "output_parquet": str(args.output.with_suffix(".parquet")),
        "checkpoint": str(args.checkpoint),
        "checkpoint_sha256": _sha256(args.checkpoint),
        "carsidock_repo": str(args.carsidock_repo),
        "carsidock_repo_head": repo_head,
        "carsidock_repo_diff_sha256": hashlib.sha256(
            repo_diff.encode()
        ).hexdigest(),
        "cuda_device_index": args.cuda_device_index,
        "runtime_seconds": runtime_seconds,
    }
    manifest_path = args.output.with_suffix(".run_manifest.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
