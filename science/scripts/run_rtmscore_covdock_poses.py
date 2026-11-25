"""Run RTMScore on extracted CovDock poses while retaining failed identities."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import yaml
from rdkit import Chem


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_table(path: Path) -> pd.DataFrame:
    return pd.read_parquet(path) if path.suffix.lower() == ".parquet" else pd.read_csv(path)


def reference_coordinates(pdb: Path, config: dict[str, object]) -> np.ndarray:
    ligand = str(config["reference_ligand"])
    chain = str(config["reference_ligand_chain"])
    residue = int(config["reference_ligand_residue"])
    coordinates = []
    for line in pdb.read_text().splitlines():
        if not line.startswith("HETATM"):
            continue
        if line[17:20].strip() != ligand or line[21:22] != chain:
            continue
        if int(line[22:26]) != residue:
            continue
        element = line[76:78].strip().upper()
        if element == "H":
            continue
        coordinates.append([float(line[30:38]), float(line[38:46]), float(line[46:54])])
    if not coordinates:
        raise ValueError(f"Reference ligand {ligand} {chain}:{residue} was not found in {pdb}")
    return np.asarray(coordinates, dtype=float)


def pose_map(path: Path) -> dict[int, Chem.Mol]:
    poses: dict[int, Chem.Mol] = {}
    for molecule in Chem.SDMolSupplier(str(path), removeHs=False):
        if molecule is None or not molecule.HasProp("input_sdf_index"):
            continue
        index = int(molecule.GetProp("input_sdf_index"))
        if index in poses:
            raise ValueError(f"Duplicate extracted pose for input_sdf_index={index}")
        poses[index] = molecule
    return poses


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--carsidock-repo", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--pdb", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument("--selected-table", type=Path, required=True)
    parser.add_argument("--pose-sdf", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cuda-device-index", type=int, default=0)
    args = parser.parse_args()

    for path in (args.carsidock_repo, args.checkpoint, args.pdb, args.target_config, args.selected_table, args.pose_sdf):
        if not path.exists():
            raise FileNotFoundError(path)
    os.environ["CUDA_VISIBLE_DEVICES"] = str(args.cuda_device_index)
    sys.path.insert(0, str(args.carsidock_repo.resolve()))
    from RTMScore.utils import get_rtmscore_model, scoring
    from src.utils.docking_utils import extract_pocket

    selected = read_table(args.selected_table).sort_values("af3_rank", kind="stable").copy()
    if selected["input_sdf_index"].duplicated().any():
        raise ValueError("Selected table contains duplicate identities")
    poses = pose_map(args.pose_sdf)
    config = yaml.safe_load(args.target_config.read_text())
    pocket = extract_pocket(
        str(args.pdb),
        reference_coordinates(args.pdb, config),
        distance=10,
        del_water=True,
    )
    output = selected[[column for column in ("input_sdf_index", "compound_id", "protomer_id", "af3_rank") if column in selected]].copy()
    output["raw_score"] = np.nan
    output["engine"] = "rtmscore"
    output["score_direction"] = "higher_is_better"
    output["engine_status"] = "FAILED"
    output["failure_reason"] = "covdock_pose_unavailable"
    available_indices = [int(index) for index in output["input_sdf_index"] if int(index) in poses]

    start = time.monotonic()
    model = get_rtmscore_model(str(args.checkpoint), device="cuda")
    batch_failure = None
    if available_indices:
        try:
            ids, predictions = scoring(
                pocket,
                [poses[index] for index in available_indices],
                model,
                device="cuda",
            )
            predictions = list(predictions)
            if len(predictions) != len(available_indices):
                raise ValueError("RTMScore prediction count mismatch")
            for index, value in zip(available_indices, predictions):
                mask = output["input_sdf_index"].eq(index)
                output.loc[mask, "raw_score"] = float(value)
                output.loc[mask, "engine_status"] = "SUCCESS"
                output.loc[mask, "failure_reason"] = None
        except Exception as exc:
            batch_failure = f"{type(exc).__name__}:{exc}"
            for index in available_indices:
                try:
                    _, prediction = scoring(pocket, [poses[index]], model, device="cuda")
                    value = float(list(prediction)[0])
                except Exception as individual_exc:
                    mask = output["input_sdf_index"].eq(index)
                    output.loc[mask, "failure_reason"] = f"rtmscore_failed:{type(individual_exc).__name__}:{individual_exc}"
                else:
                    mask = output["input_sdf_index"].eq(index)
                    output.loc[mask, "raw_score"] = value
                    output.loc[mask, "engine_status"] = "SUCCESS"
                    output.loc[mask, "failure_reason"] = None
    runtime = time.monotonic() - start
    args.output.parent.mkdir(parents=True, exist_ok=True)
    output.to_parquet(args.output, index=False)
    output.to_csv(args.output.with_suffix(".csv"), index=False)
    repo_head = subprocess.run(["git", "-C", str(args.carsidock_repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True).stdout.strip()
    manifest = {
        "engine": "rtmscore",
        "status": "SUCCESS_WITH_RETAINED_FAILURES" if output["engine_status"].ne("SUCCESS").any() else "SUCCESS",
        "selected_count": len(output),
        "pose_count": len(poses),
        "scored_count": int(output["engine_status"].eq("SUCCESS").sum()),
        "failed_count": int(output["engine_status"].ne("SUCCESS").sum()),
        "batch_failure": batch_failure,
        "runtime_seconds": runtime,
        "checkpoint": str(args.checkpoint.resolve()),
        "checkpoint_sha256": sha256_file(args.checkpoint),
        "carsidock_repo_head": repo_head,
        "pdb_sha256": sha256_file(args.pdb),
        "target_config_sha256": sha256_file(args.target_config),
        "output": str(args.output),
        "output_sha256": sha256_file(args.output),
    }
    args.output.with_suffix(".run_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
