"""Create a protein-only AF3 data-pipeline seed from an RCSB mmCIF chain."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re

import yaml
from Bio.Data.PDBData import protein_letters_3to1_extended
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from covscreen.af3_target import validate_target


def values(item: object) -> list[str]:
    return [str(value) for value in item] if isinstance(item, list) else [str(item)]


def chain_sequence(cif_path: Path, chain: str) -> tuple[str, list[dict[str, object]]]:
    cif = MMCIF2Dict(str(cif_path))
    chains = values(cif["_pdbx_poly_seq_scheme.pdb_strand_id"])
    seq_ids = values(cif["_pdbx_poly_seq_scheme.seq_id"])
    monomers = values(cif["_pdbx_poly_seq_scheme.mon_id"])
    auth = values(cif["_pdbx_poly_seq_scheme.auth_seq_num"])
    asym = values(cif["_pdbx_poly_seq_scheme.asym_id"])
    insertion = values(cif["_pdbx_poly_seq_scheme.pdb_ins_code"])
    if len({len(column) for column in (chains, seq_ids, monomers, auth, asym, insertion)}) != 1:
        raise ValueError("Inconsistent mmCIF polymer mapping columns")
    rows: dict[int, dict[str, object]] = {}
    for row_chain, seq_id, monomer, auth_number, label_chain, code in zip(
            chains, seq_ids, monomers, auth, asym, insertion):
        if row_chain != chain:
            continue
        if not re.fullmatch(r"[1-9][0-9]*", seq_id):
            raise ValueError("Invalid mmCIF polymer sequence index")
        index = int(seq_id)
        if index in rows:
            raise ValueError("Ambiguous mmCIF polymer mapping for the selected author chain")
        rows[index] = {
            "sequence_index": index,
            "monomer": monomer,
            "auth_residue_number": auth_number,
            "insertion_code": "" if code in {".", "?"} else code,
            "label_chain": label_chain,
        }
    if not rows or sorted(rows) != list(range(1, max(rows) + 1)):
        raise ValueError(f"Non-contiguous polymer sequence for chain {chain}")
    if len({row["label_chain"] for row in rows.values()}) != 1:
        raise ValueError("Author chain maps to multiple label chains")
    sequence = "".join(
        protein_letters_3to1_extended.get(str(rows[index]["monomer"]).upper(), "X")
        for index in sorted(rows)
    )
    return sequence, [rows[index] for index in sorted(rows)]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cif", type=Path, required=True)
    parser.add_argument("--target-config", type=Path, required=True)
    parser.add_argument("--model-seed", type=int, default=20260826)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    config = yaml.safe_load(args.target_config.read_text())
    chain = str(config["protein_chain"])
    sequence, mapping = chain_sequence(args.cif, chain)
    if type(config["residue_number"]) is not int or type(config["cysteine_sequence_index"]) is not int:
        raise ValueError("AF3 residue number and sequence index must be explicit integers")
    if config.get("residue_insertion_code", ""):
        raise ValueError("AF3 source insertion-code support requires an explicit mapping adapter")
    target = [
        row
        for row in mapping
        if row["auth_residue_number"] not in {".", "?"}
        and re.fullmatch(r"-?[0-9]+", str(row["auth_residue_number"]))
        and int(row["auth_residue_number"]) == config["residue_number"]
    ]
    expected = config["cysteine_sequence_index"]
    if (len(target) != 1 or target[0]["sequence_index"] != expected
            or target[0]["insertion_code"] or target[0]["monomer"] != "CYS"):
        raise ValueError(f"Configured covalent residue does not map to Cys at sequence index {expected}")
    entities: list[dict[str, object]] = [
        {"protein": {"id": chain, "sequence": sequence}}
    ]
    cofactor_chains = iter("GHIJKLMNOPQRSTUVWXYZ")
    for cofactor in config.get("retain_cofactors", []):
        cofactor = str(cofactor).upper()
        cofactor_chain = next(cofactor_chains)
        # Native AF3 represents ions and small molecules through the same
        # ligand entity with one or more CCD codes.
        entities.append({"ligand": {"id": cofactor_chain, "ccdCodes": [cofactor]}})
    payload = {
        "name": f"{config['target_id']}_protein_pipeline_seed",
        "modelSeeds": [args.model_seed],
        "sequences": entities,
        "dialect": "alphafold3",
        "version": 1,
    }
    seed_check = validate_target(payload, chain, expected,
        config.get("ligand_chain", "L"), config["nucleophilic_atom"],
        config.get("protein_sequence_sha256"))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(payload, indent=2) + "\n")
    manifest = {
        "status": "SUCCESS",
        "target_id": config["target_id"],
        "site_id": config["site_id"],
        "chain": chain,
        "sequence_length": len(sequence),
        "cysteine_sequence_index": expected,
        "cofactor_count": len(entities) - 1,
        "source_cif": str(args.cif),
        "source_cif_sha256": sha256_file(args.cif),
        "target_config_sha256": sha256_file(args.target_config),
        "output": str(args.output),
        "output_sha256": sha256_file(args.output),
        "af3_target_seed_check": seed_check,
        "source_residue_mapping": target[0],
        "polymer_residue_mapping": mapping,
        "canonical_mapping_verified": False,
    }
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
