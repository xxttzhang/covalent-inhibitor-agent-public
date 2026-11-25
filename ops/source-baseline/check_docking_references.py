"""Read-only preflight for operator-registered experimental docking references.

Run with the installed scientific host Python before browser acceptance.
This checks deployment assets, not receptor suitability or workflow approval.
"""
import argparse
import json
from pathlib import Path

from covscreen.docking_reference import validate_reference
from covscreen.receptor_evidence import regular


def check(root, model, chain, residue, canonical):
    root = Path(root).resolve(strict=True)
    registry, checksum = regular(root, root / "configs/library_docking_references.json")
    data = json.loads(registry.read_text())
    references = data.get("references")
    if data.get("schema") != "library-docking-references-v1" or not isinstance(references, list) or not references:
        raise ValueError("A nonempty library-docking-references-v1 registry is required")
    checked = []
    for asset in references:
        for path_key, hash_key in (("pdb", "pdb_sha256"), ("reference_ligand", "reference_sha256")):
            path = Path(asset[path_key])
            if not path.is_absolute():
                raise ValueError("Registered reference paths must be absolute")
            regular(Path(path.anchor), path, asset[hash_key])
        result = validate_reference(asset, {"input_sha256": asset["pdb_sha256"], "model": model},
            {"author_chain": chain, "author_residue_id": residue, "canonical_position": canonical})
        checked.append({key: result[key] for key in
            ("pdb_sha256", "reference_sha256", "matched_heavy_atoms", "covres", "canonical_position")})
    return {"registry_sha256": checksum, "references": checked,
            "scope": "deployment reference integrity and coordinate correspondence only",
            "scientific_acceptance": False, "compute_submitted": False}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", required=True)
    parser.add_argument("--model", type=int, required=True)
    parser.add_argument("--author-chain", required=True)
    parser.add_argument("--author-residue", required=True)
    parser.add_argument("--canonical-position", type=int, required=True)
    args = parser.parse_args()
    print(json.dumps(check(args.data_root, args.model, args.author_chain,
        args.author_residue, args.canonical_position), indent=2))
