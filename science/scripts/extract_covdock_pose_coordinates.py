"""Extract selected CovDock protein C-alpha and covalently linked ligand coordinates.

Run this script with ``$SCHRODINGER/run python3``.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

from inspect_covdock_output import (
    _atom_index,
    _bonded_atoms,
    _coordinates,
    _integer,
    _is_hydrogen,
    _is_protein_atom,
    _residue_name,
    _same_residue,
    _text,
)


def selected_pose_indices(path: Path) -> set[int]:
    with path.open(newline="") as handle:
        rows = list(csv.DictReader(handle))
    if not rows or "covdock_pose_index" not in rows[0]:
        raise ValueError("selection CSV must contain covdock_pose_index")
    indices = {int(float(row["covdock_pose_index"])) for row in rows}
    if len(indices) != len(rows):
        raise ValueError("selection CSV contains duplicate covdock_pose_index values")
    return indices


def extract_pose(
    structure: object,
    pose_index: int,
    *,
    attachment_chain: str,
    attachment_residue: int,
    nucleophilic_atom: str,
) -> dict[str, object]:
    atoms = list(getattr(structure, "atom"))
    target = [
        atom
        for atom in atoms
        if _text(atom, "chain") == attachment_chain
        and _integer(atom, "resnum", "residue_number") == attachment_residue
        and _text(atom, "pdbname", "atom_name", "name").upper()
        == nucleophilic_atom.upper()
    ]
    if len(target) != 1:
        raise ValueError(f"pose {pose_index}: target atom match count={len(target)}")
    target_atom = target[0]
    partner = [
        atom for atom in _bonded_atoms(target_atom) if not _same_residue(atom, target_atom)
    ]
    if len(partner) != 1:
        raise ValueError(f"pose {pose_index}: covalent partner count={len(partner)}")

    ligand: list[object] = []
    queue = [partner[0]]
    seen = {_atom_index(target_atom)}
    while queue:
        atom = queue.pop()
        atom_index = _atom_index(atom)
        if atom_index in seen:
            continue
        seen.add(atom_index)
        if _is_protein_atom(atom):
            continue
        ligand.append(atom)
        queue.extend(_bonded_atoms(atom))

    protein_ca = [
        {
            "chain": _text(atom, "chain"),
            "resnum": _integer(atom, "resnum", "residue_number"),
            "resname": _residue_name(atom),
            "atom_name": _text(atom, "pdbname", "atom_name", "name").upper(),
            "xyz": list(_coordinates(atom)),
        }
        for atom in atoms
        if _is_protein_atom(atom)
        and _text(atom, "chain") == attachment_chain
        and _text(atom, "pdbname", "atom_name", "name").upper() == "CA"
    ]
    ligand_heavy = [
        {
            "atom_index": _atom_index(atom),
            "atom_name": _text(atom, "pdbname", "atom_name", "name").upper(),
            "element": _text(atom, "element").upper(),
            "atomic_number": _integer(atom, "atomic_number"),
            "xyz": list(_coordinates(atom)),
        }
        for atom in ligand
        if not _is_hydrogen(atom)
    ]
    if len(protein_ca) < 3 or not ligand_heavy:
        raise ValueError(f"pose {pose_index}: insufficient protein or ligand coordinates")
    heavy_indices = {atom["atom_index"] for atom in ligand_heavy}
    heavy_bonds = []
    for bond in getattr(structure, "bond"):
        first, second = bond.atom1, bond.atom2
        if _atom_index(first) in heavy_indices and _atom_index(second) in heavy_indices:
            heavy_bonds.append({
                "atom_name_1": _text(first, "pdbname", "atom_name", "name").upper(),
                "atom_name_2": _text(second, "pdbname", "atom_name", "name").upper(),
                "order": float(bond.order),
            })
    return {
        "pose_index": pose_index,
        "title": str(getattr(structure, "title", "")),
        "protein_ca": protein_ca,
        "ligand_heavy_atoms": ligand_heavy,
        "ligand_heavy_bonds": heavy_bonds,
        "covalent_partner_atom_name": _text(partner[0], "pdbname", "atom_name", "name").upper(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--selection-csv", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--attachment-chain", required=True)
    parser.add_argument("--attachment-residue", type=int, required=True)
    parser.add_argument("--nucleophilic-atom", default="SG")
    args = parser.parse_args()

    from schrodinger.structure import StructureReader

    selected = selected_pose_indices(args.selection_csv)
    rows = []
    for pose_index, structure in enumerate(StructureReader(str(args.input))):
        if pose_index not in selected:
            continue
        rows.append(
            extract_pose(
                structure,
                pose_index,
                attachment_chain=args.attachment_chain,
                attachment_residue=args.attachment_residue,
                nucleophilic_atom=args.nucleophilic_atom,
            )
        )
    found = {int(row["pose_index"]) for row in rows}
    missing = sorted(selected - found)
    if missing:
        raise ValueError(f"selected CovDock poses are missing: {missing[:20]}")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "input": str(args.input.resolve()),
                "selection_csv": str(args.selection_csv.resolve()),
                "selected_pose_count": len(selected),
                "rows": rows,
            },
            indent=2,
        )
        + "\n"
    )
    print(json.dumps({"selected": len(selected), "output": str(args.output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
