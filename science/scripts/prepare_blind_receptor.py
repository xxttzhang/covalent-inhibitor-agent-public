"""Prepare a target receptor PDB while retaining only registered cofactors."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import yaml


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pdb", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = yaml.safe_load(args.target_config.read_text())
    chain = str(config["protein_chain"])
    reference_ligand = str(config["reference_ligand"]).upper()
    retain = {str(value).upper() for value in config.get("retain_cofactors", [])}
    kept = []
    atom_count = 0
    cofactor_counts: dict[str, int] = {}
    for line in args.pdb.read_text().splitlines():
        record = line[:6]
        residue_name = line[17:20].strip().upper()
        line_chain = line[21:22]
        if record == "ATOM  " and line_chain == chain:
            if line[16:17] in {" ", "A"}:
                kept.append(line)
                atom_count += 1
        elif record == "HETATM" and residue_name in retain:
            if residue_name == reference_ligand:
                continue
            kept.append(line)
            cofactor_counts[residue_name] = cofactor_counts.get(residue_name, 0) + 1
    if atom_count == 0:
        raise ValueError(f"No protein atoms found for chain {chain}")
    missing = sorted(retain - set(cofactor_counts))
    if missing:
        raise ValueError(f"Registered cofactors absent from receptor PDB: {missing}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("\n".join([*kept, "TER", "END"]) + "\n")
    manifest = {
        "status": "SUCCESS",
        "target_id": config["target_id"],
        "site_id": config["site_id"],
        "protein_chain": chain,
        "protein_atom_count": atom_count,
        "retained_cofactor_atom_counts": cofactor_counts,
        "removed_reference_ligand": reference_ligand,
        "source_pdb": str(args.pdb),
        "source_pdb_sha256": sha256_file(args.pdb),
        "target_config_sha256": sha256_file(args.target_config),
        "output": str(args.output),
        "output_sha256": sha256_file(args.output),
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
