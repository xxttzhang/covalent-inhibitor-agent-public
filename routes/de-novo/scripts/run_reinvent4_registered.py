from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
import subprocess
from pathlib import Path

import pandas as pd

from generator_adapter_common import (
    canonical_smiles,
    clean_generator_environment,
    stable_candidate_id,
    write_candidate_table,
    write_generation_report,
)


def toml_string(value: str) -> str:
    return json.dumps(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run registered REINVENT4 de novo sampling.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--generator-python", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--num-smiles", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--cuda-device-index", type=int, default=0)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    args = parser.parse_args()

    args.output_dir.mkdir(parents=True, exist_ok=True)
    output_csv = args.output_dir / "reinvent4_raw.csv"
    log_path = args.output_dir / "reinvent4_external.log"
    json_config = args.output_dir / "reinvent4_effective_config.json"
    config_path = args.output_dir / "reinvent4_sampling.toml"
    config_path.write_text(
        "run_type = \"sampling\"\n"
        f"device = {toml_string('cuda:0' if args.device == 'cuda' else 'cpu')}\n"
        f"json_out_config = {toml_string(str(json_config))}\n\n"
        "[parameters]\n"
        f"model_file = {toml_string(str(args.checkpoint.resolve()))}\n"
        f"output_file = {toml_string(str(output_csv.resolve()))}\n"
        f"num_smiles = {args.num_smiles}\n"
        "unique_molecules = true\n"
        "randomize_smiles = true\n"
    )
    executable = args.generator_python.parent / "reinvent"
    command = [
        str(executable),
        "-l", str(log_path),
        "-s", str(args.seed),
        str(config_path),
    ]
    completed = subprocess.run(
        command,
        cwd=args.repo,
        env=clean_generator_environment(
            args.generator_python, args.cuda_device_index, use_cuda=args.device == "cuda"
        ),
        check=False,
    )
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    if output_csv.is_file():
        frame = pd.read_csv(output_csv)
        smiles_column = "SMILES" if "SMILES" in frame.columns else "smiles"
        if smiles_column not in frame.columns:
            raise ValueError("REINVENT4 output has no SMILES column")
        for index, record in frame.iterrows():
            smiles = str(record.get(smiles_column, ""))
            canonical = canonical_smiles(smiles)
            valid = canonical is not None
            is_unique = bool(valid and canonical not in seen)
            if canonical:
                seen.add(canonical)
            nll = record.get("NLL")
            rows.append(
                {
                    "candidate_id": stable_candidate_id("reinvent4", int(index), canonical),
                    "generator": "reinvent4",
                    "source_index": int(index),
                    "smiles": smiles,
                    "canonical_smiles": canonical or "",
                    "valid": valid,
                    "unique": is_unique,
                    "engine_status": "SUCCESS" if valid else "FAILED",
                    "failure_reason": "" if valid else "invalid_smiles",
                    "raw_score": "" if pd.isna(nll) else float(nll),
                    "score_name": "reinvent4_negative_log_likelihood",
                    "score_direction": "lower_is_more_likely_not_binding",
                    "source_file": str(output_csv),
                    "smiles_state": record.get("SMILES_state", ""),
                }
            )
    candidate_path = args.output_dir / "reinvent4_candidates.csv"
    write_candidate_table(candidate_path, rows)
    report = write_generation_report(
        args.output_dir / "reinvent4_generation_report.json",
        generator="reinvent4",
        requested=args.num_smiles,
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
