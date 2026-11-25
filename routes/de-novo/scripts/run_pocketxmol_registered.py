from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
import subprocess
from pathlib import Path

import pandas as pd
import yaml

from generator_adapter_common import (
    canonical_smiles,
    clean_generator_environment,
    stable_candidate_id,
    write_candidate_table,
    write_generation_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run registered PocketXMol SBDD sampling.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--generator-python", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--protein", type=Path, required=True)
    parser.add_argument("--pocket-center", type=float, nargs=3, required=True)
    parser.add_argument("--pocket-radius", type=float, default=15.0)
    parser.add_argument("--mean-atoms", type=int, default=28)
    parser.add_argument("--std-atoms", type=float, default=2.0)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--num-molecules", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--cuda-device-index", type=int, default=0)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    raw_dir = args.output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    task_path = args.output_dir / "pocketxmol_task.yml"
    model_path = args.output_dir / "pocketxmol_model.yml"
    center = [float(value) for value in args.pocket_center]
    task = {
        "sample": {
            "seed": args.seed,
            "batch_size": min(args.num_molecules, 8),
            "num_mols": args.num_molecules,
            "save_traj_prob": 0.0,
        },
        "data": {
            "protein_path": str(args.protein.resolve()),
            "is_pep": False,
            "pocket_args": {"pocket_coord": center, "radius": args.pocket_radius},
            "pocmol_args": {"data_id": "registered_sbdd", "pdbid": "registered"},
        },
        "transforms": {
            "featurizer_pocket": {"center": center},
            "variable_mol_size": {
                "name": "variable_mol_size",
                "num_atoms_distri": {
                    "strategy": "mol_atoms_based",
                    "mean": {"coef": 0, "bias": args.mean_atoms},
                    "std": {"coef": 0, "bias": args.std_atoms},
                    "min": 5,
                },
            },
        },
        "task": {"name": "sbdd", "transform": {"name": "sbdd"}},
        "noise": {
            "name": "sbdd",
            "num_steps": 100,
            "prior": "from_train",
            "level": {
                "name": "advance",
                "min": 0.0,
                "max": 1.0,
                "step2level": {
                    "scale_start": 0.99999,
                    "scale_end": 0.00001,
                    "width": 3,
                },
            },
        },
    }
    task_path.write_text(yaml.safe_dump(task, sort_keys=False))
    model_path.write_text(yaml.safe_dump({"model": {"checkpoint": str(args.checkpoint.resolve())}}))
    command = [
        str(args.generator_python),
        str(args.repo / "scripts" / "sample_use.py"),
        "--config_task", str(task_path),
        "--config_model", str(model_path),
        "--outdir", str(raw_dir),
        "--device", "cuda:0" if args.device == "cuda" else "cpu",
        "--batch_size", str(min(args.num_molecules, 8)),
        "--num_workers", "0",
    ]
    completed = subprocess.run(
        command,
        cwd=args.repo,
        env=clean_generator_environment(
            args.generator_python, args.cuda_device_index, use_cuda=args.device == "cuda"
        ),
        check=False,
    )
    info_files = sorted(raw_dir.rglob("gen_info.csv"))
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    if info_files:
        info_path = info_files[-1]
        frame = pd.read_csv(info_path)
        pure_sdf_dir = info_path.parent / f"{info_path.parent.name}_SDF"
        for index, record in frame.iterrows():
            raw_smiles = record.get("smiles", "")
            smiles = "" if pd.isna(raw_smiles) else str(raw_smiles)
            canonical = canonical_smiles(smiles)
            raw_tag = record.get("tag", "")
            tag = "" if pd.isna(raw_tag) else str(raw_tag)
            valid = canonical is not None and tag == ""
            is_unique = bool(valid and canonical not in seen)
            if canonical:
                seen.add(canonical)
            filename = record.get("filename", "")
            source_file = pure_sdf_dir / str(filename) if filename else None
            score = record.get("cfd_pos")
            rows.append(
                {
                    "candidate_id": stable_candidate_id("pocketxmol", int(index), canonical),
                    "generator": "pocketxmol",
                    "source_index": int(index),
                    "smiles": smiles,
                    "canonical_smiles": canonical or "",
                    "valid": valid,
                    "unique": is_unique,
                    "engine_status": "SUCCESS" if valid else "FAILED",
                    "failure_reason": "" if valid else (tag or "invalid_smiles"),
                    "raw_score": "" if pd.isna(score) else float(score),
                    "score_name": "pocketxmol_position_confidence",
                    "score_direction": "higher_is_generation_confidence_not_binding",
                    "source_file": str(source_file) if source_file else "",
                    "cfd_traj": record.get("cfd_traj", ""),
                    "cfd_node": record.get("cfd_node", ""),
                    "cfd_edge": record.get("cfd_edge", ""),
                }
            )
    write_candidate_table(args.output_dir / "pocketxmol_candidates.csv", rows)
    report = write_generation_report(
        args.output_dir / "pocketxmol_generation_report.json",
        generator="pocketxmol",
        requested=args.num_molecules,
        rows=rows,
        seed=args.seed,
        external_return_code=completed.returncode,
    )
    print(json.dumps(report, indent=2))
    if completed.returncode != 0:
        return completed.returncode
    return 0 if report["n_valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
