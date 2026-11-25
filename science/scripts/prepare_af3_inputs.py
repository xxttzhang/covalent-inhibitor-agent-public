from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from covscreen.af3 import write_af3_inputs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--input",
        type=Path,
        default=Path("data/processed/BTK/btk_mini.parquet"),
    )
    ap.add_argument("--protein-fasta", type=Path, required=True)
    ap.add_argument(
        "--output-dir",
        type=Path,
        default=Path("data/processed/BTK/af3_inputs"),
    )
    ap.add_argument("--manifest", type=Path)
    ap.add_argument("--label", choices=["active", "decoy", "all"], default="all")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--seed", type=int, default=20260725)
    ap.add_argument("--target-id", default="BTK")
    ap.add_argument(
        "--protein-description",
        default="BTK 5P9J chain A; Cys481 is sequence index 88",
    )
    args = ap.parse_args()

    frame = pd.read_parquet(args.input)
    if args.label != "all":
        frame = frame[frame["label"].astype(str).eq(args.label)]
    frame = frame.sort_values("input_sdf_index")
    if args.limit is not None:
        frame = frame.head(args.limit)
    if frame.empty:
        raise SystemExit("No BTK-mini rows selected for AF3")

    fasta_lines = [
        line.strip()
        for line in args.protein_fasta.read_text().splitlines()
        if line.strip() and not line.startswith(">")
    ]
    protein_sequence = "".join(fasta_lines)
    if not protein_sequence:
        raise SystemExit("Protein FASTA contains no sequence")

    manifest = write_af3_inputs(
        frame=frame,
        protein_sequence=protein_sequence,
        output_dir=args.output_dir,
        seed=args.seed,
        target_id=args.target_id,
        protein_description=args.protein_description,
    )
    manifest_path = args.manifest or args.output_dir / "manifest.csv"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest.to_csv(manifest_path, index=False)
    manifest.to_json(manifest_path.with_suffix(".json"), orient="records", indent=2)
    print(
        {
            "inputs": len(manifest),
            "labels": manifest["label"].value_counts().to_dict(),
            "manifest": str(manifest_path),
            "covalent_bond_constraint": False,
        }
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
