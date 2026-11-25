from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path

from rdkit import Chem

from generator_adapter_common import (
    clean_generator_environment,
    stable_candidate_id,
    write_candidate_table,
    write_generation_report,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run registered pocket-conditioned DiffLinker.")
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--generator-python", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--fragments", type=Path, required=True)
    parser.add_argument("--protein", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--n-samples", type=int, required=True)
    parser.add_argument("--n-steps", type=int)
    parser.add_argument("--linker-size", required=True)
    parser.add_argument("--anchors")
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--cuda-device-index", type=int, default=0)
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cuda")
    args = parser.parse_args()

    raw_dir = args.output_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    command = [
        str(args.generator_python),
        str(args.repo / "generate_with_protein.py"),
        "--fragments", str(args.fragments),
        "--protein", str(args.protein),
        "--model", str(args.checkpoint),
        "--linker_size", args.linker_size,
        "--output", str(raw_dir),
        "--n_samples", str(args.n_samples),
        "--max_batch_size", str(min(args.n_samples, 16)),
        "--random_seed", str(args.seed),
    ]
    if args.n_steps is not None:
        command.extend(["--n_steps", str(args.n_steps)])
    if args.anchors:
        command.extend(["--anchors", args.anchors])
    completed = subprocess.run(
        command,
        cwd=args.repo,
        env=clean_generator_environment(
            args.generator_python, args.cuda_device_index, use_cuda=args.device == "cuda"
        ),
        check=False,
    )

    indexed: dict[int, Path] = {}
    for path in sorted(raw_dir.glob("output_*.sdf")):
        match = re.match(r"output_(\d+)_", path.name)
        if match:
            indexed.setdefault(int(match.group(1)), path)
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    combined_path = args.output_dir / "difflinker_candidates.sdf"
    writer = Chem.SDWriter(str(combined_path))
    try:
        for index in range(args.n_samples):
            source = indexed.get(index)
            molecule = (
                Chem.MolFromMolFile(str(source), sanitize=True, removeHs=False)
                if source is not None
                else None
            )
            canonical = (
                Chem.MolToSmiles(molecule, canonical=True, isomericSmiles=True)
                if molecule is not None
                else None
            )
            valid = canonical is not None
            is_unique = bool(valid and canonical not in seen)
            if canonical:
                seen.add(canonical)
            candidate_id = stable_candidate_id("difflinker", index, canonical)
            if molecule is not None:
                molecule.SetProp("CANDIDATE_ID", candidate_id)
                molecule.SetIntProp("SOURCE_INDEX", index)
                writer.write(molecule)
            rows.append(
                {
                    "candidate_id": candidate_id,
                    "generator": "difflinker",
                    "source_index": index,
                    "smiles": canonical or "",
                    "canonical_smiles": canonical or "",
                    "valid": valid,
                    "unique": is_unique,
                    "engine_status": "SUCCESS" if valid else "FAILED",
                    "failure_reason": "" if valid else "missing_or_invalid_generated_sdf",
                    "raw_score": "",
                    "score_name": "",
                    "score_direction": "not_applicable",
                    "source_file": str(source) if source else "",
                }
            )
    finally:
        writer.close()
    write_candidate_table(args.output_dir / "difflinker_candidates.csv", rows)
    report = write_generation_report(
        args.output_dir / "difflinker_generation_report.json",
        generator="difflinker",
        requested=args.n_samples,
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
