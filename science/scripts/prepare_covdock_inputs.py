from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

import pandas as pd
from rdkit import Chem

from covscreen.schrodinger import sha256_file


def select_molecule(sdf_path: Path, input_sdf_index: int) -> Chem.Mol:
    matches = []
    for molecule in Chem.SDMolSupplier(str(sdf_path), removeHs=False):
        if molecule is None or not molecule.HasProp("INPUT_SDF_INDEX"):
            continue
        if int(molecule.GetProp("INPUT_SDF_INDEX")) == input_sdf_index:
            matches.append(molecule)
    if len(matches) != 1:
        raise ValueError(
            f"Expected one SDF record for INPUT_SDF_INDEX={input_sdf_index}; "
            f"found {len(matches)}"
        )
    return matches[0]


def write_chain_only_pdb(input_path: Path, output_path: Path, chain: str) -> None:
    kept = []
    for line in input_path.read_text().splitlines():
        if line.startswith("ATOM  ") and line[21:22] == chain:
            altloc = line[16:17]
            if altloc in {" ", "A"}:
                kept.append(line)
    if not kept:
        raise ValueError(f"No ATOM records found for chain {chain}")
    output_path.write_text("\n".join(kept + ["TER", "END"]) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--library-table", type=Path, required=True)
    ap.add_argument("--library-sdf", type=Path, required=True)
    ap.add_argument("--input-sdf-index", type=int, required=True)
    ap.add_argument("--receptor-pdb", type=Path, required=True)
    ap.add_argument("--protein-chain", default="A")
    ap.add_argument("--output-dir", type=Path, required=True)
    ap.add_argument("--schrodinger-root", type=Path)
    args = ap.parse_args()

    table = pd.read_parquet(args.library_table)
    selected = table.loc[table["input_sdf_index"] == args.input_sdf_index]
    if len(selected) != 1:
        raise ValueError(
            f"Expected one table row for input_sdf_index={args.input_sdf_index}; "
            f"found {len(selected)}"
        )
    identity = selected.iloc[0].to_dict()
    molecule = select_molecule(args.library_sdf, args.input_sdf_index)
    expected = {
        "COMPOUND_ID": str(identity["compound_id"]),
        "PROTOMER_ID": str(identity["protomer_id"]),
        "INPUT_SDF_INDEX": str(args.input_sdf_index),
    }
    for key, value in expected.items():
        if molecule.GetProp(key) != value:
            raise ValueError(
                f"Identity mismatch for {key}: {molecule.GetProp(key)} != {value}"
            )

    args.output_dir.mkdir(parents=True, exist_ok=True)
    stem = (
        f"{identity['compound_id']}__p{identity['protomer_id']}"
        f"__i{args.input_sdf_index}"
    )
    ligand_sdf = args.output_dir / f"{stem}.sdf"
    molecule.SetProp("_Name", stem)
    writer = Chem.SDWriter(str(ligand_sdf))
    writer.write(molecule)
    writer.close()

    receptor_pdb = args.output_dir / f"BTK_chain_{args.protein_chain}.pdb"
    write_chain_only_pdb(args.receptor_pdb, receptor_pdb, args.protein_chain)

    manifest: dict[str, object] = {
        "identity": identity,
        "input_sdf_index": args.input_sdf_index,
        "ligand_sdf": str(ligand_sdf.resolve()),
        "ligand_sdf_sha256": sha256_file(ligand_sdf),
        "receptor_pdb": str(receptor_pdb.resolve()),
        "receptor_pdb_sha256": sha256_file(receptor_pdb),
        "protein_chain": args.protein_chain,
        "attachment_residue": f"{args.protein_chain}:481",
        "reaction_type": "Michael Addition",
        "covdock_mode": "enrichment",
    }

    if args.schrodinger_root:
        structconvert = args.schrodinger_root / "utilities" / "structconvert"
        if not structconvert.is_file():
            raise FileNotFoundError(structconvert)
        ligand_mae = ligand_sdf.with_suffix(".mae")
        receptor_mae = receptor_pdb.with_suffix(".mae")
        env = os.environ.copy()
        for key in (
            "SCHRODINGER",
            "SCHROD_LICENSE_FILE",
            "LM_LICENSE_FILE",
            "PYTHONHOME",
            "LD_LIBRARY_PATH",
            "LD_PRELOAD",
        ):
            env.pop(key, None)
        for source, destination in (
            (ligand_sdf, ligand_mae),
            (receptor_pdb, receptor_mae),
        ):
            subprocess.run(
                [str(structconvert), str(source), str(destination)],
                env=env,
                check=True,
            )
        manifest.update(
            {
                "ligand_mae": str(ligand_mae.resolve()),
                "ligand_mae_sha256": sha256_file(ligand_mae),
                "receptor_unprepared_mae": str(receptor_mae.resolve()),
                "receptor_unprepared_mae_sha256": sha256_file(receptor_mae),
            }
        )

    manifest_path = args.output_dir / "input_manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, default=str) + "\n")
    print(json.dumps(manifest, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
