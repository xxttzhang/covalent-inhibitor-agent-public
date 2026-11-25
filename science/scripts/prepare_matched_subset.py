from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd
from rdkit import Chem


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-table", type=Path, required=True)
    ap.add_argument("--library-sdf", type=Path, required=True)
    selection = ap.add_mutually_exclusive_group(required=True)
    selection.add_argument("--matched-active-id")
    selection.add_argument("--all", action="store_true")
    ap.add_argument("--output-prefix", type=Path, required=True)
    args = ap.parse_args()

    table = pd.read_parquet(args.library_table)
    if args.all:
        selected = table.copy()
    else:
        selected = table.loc[
            (
                table["compound_id"].astype(str).eq(args.matched_active_id)
                & table["label"].eq("active")
            )
            | (
                table["matched_active_id"].astype(str).eq(args.matched_active_id)
                & table["label"].eq("decoy")
            )
        ].copy()
    selected = selected.sort_values("input_sdf_index")
    if selected.empty:
        raise ValueError(f"No matched set for {args.matched_active_id}")
    selected_indices = set(selected["input_sdf_index"].astype(int))
    molecules: dict[int, Chem.Mol] = {}
    for molecule in Chem.SDMolSupplier(
        str(args.library_sdf), removeHs=False
    ):
        if molecule is None or not molecule.HasProp("INPUT_SDF_INDEX"):
            continue
        input_index = int(molecule.GetProp("INPUT_SDF_INDEX"))
        if input_index in selected_indices:
            molecules[input_index] = molecule

    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    sdf_path = args.output_prefix.with_suffix(".sdf")
    writer = Chem.SDWriter(str(sdf_path))
    failures = []
    for row in selected.itertuples(index=False):
        input_index = int(row.input_sdf_index)
        molecule = molecules.get(input_index)
        if molecule is None:
            failures.append(
                {
                    "compound_id": row.compound_id,
                    "input_sdf_index": input_index,
                    "failure_reason": "chemistry_preparation_failed",
                }
            )
            continue
        title = (
            f"{row.compound_id}__p{row.protomer_id}__i{input_index}"
        )
        molecule.SetProp("_Name", title)
        writer.write(molecule)
    writer.close()
    selected.to_parquet(args.output_prefix.with_suffix(".parquet"), index=False)
    selected.to_csv(args.output_prefix.with_suffix(".csv"), index=False)
    pd.DataFrame(failures).to_csv(
        args.output_prefix.with_name(
            f"{args.output_prefix.name}_failures.csv"
        ),
        index=False,
    )
    manifest = {
        "matched_active_id": args.matched_active_id,
        "selection": "all" if args.all else "matched_set",
        "rows": len(selected),
        "active": int(selected["label"].eq("active").sum()),
        "decoy": int(selected["label"].eq("decoy").sum()),
        "sdf_records": len(molecules),
        "chemistry_failures": len(failures),
        "sdf": str(sdf_path.resolve()),
        "table": str(args.output_prefix.with_suffix(".parquet").resolve()),
    }
    args.output_prefix.with_name(
        f"{args.output_prefix.name}_manifest.json"
    ).write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
