from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
from pathlib import Path

import pandas as pd

from covscreen.ligand_first_evaluation import (
    independently_evaluate_ligand_first_batch,
    write_ligand_first_evaluation,
)
from covscreen.coordinate_recheck import recheck_coordinates


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Independently re-evaluate a Ligand-first candidate batch."
    )
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--min-parent-mcs-fraction", type=float, default=0.60)
    parser.add_argument("--schrodinger-root", type=Path)
    parser.add_argument("--attachment-chain")
    parser.add_argument("--attachment-residue", type=int)
    args = parser.parse_args()
    coordinate_options = (args.schrodinger_root, args.attachment_chain, args.attachment_residue)
    if any(value is not None for value in coordinate_options) and not all(
        value is not None for value in coordinate_options
    ):
        parser.error("Coordinate recheck requires schrodinger-root, attachment-chain and attachment-residue")
    frame = (
        pd.read_parquet(args.candidate_table)
        if args.candidate_table.suffix.lower() == ".parquet"
        else pd.read_csv(args.candidate_table)
    )
    coordinates = None
    if args.schrodinger_root:
        coordinates = recheck_coordinates(
            frame, schrodinger_root=args.schrodinger_root.resolve(),
            inspector=(_bootstrap.PROJECT / "scripts/inspect_covdock_output.py").resolve(),
            output_dir=args.output_dir.resolve() / "coordinate_recheck",
            attachment_chain=args.attachment_chain, attachment_residue=args.attachment_residue,
        )
    evaluated = independently_evaluate_ligand_first_batch(
        frame,
        args.campaign.resolve(),
        min_parent_mcs_fraction=args.min_parent_mcs_fraction,
        coordinate_evidence=coordinates,
    )
    manifest = write_ligand_first_evaluation(
        evaluated,
        args.output_dir,
        input_path=args.candidate_table.resolve(),
        campaign_path=args.campaign.resolve(),
        require_coordinate_recheck=bool(args.schrodinger_root),
    )
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
