"""Inspect CovDock pose properties and reaction geometry.

Run the CLI with ``$SCHRODINGER/run python3``. The geometry helpers remain
dependency-free so they can be unit-tested without a Schrödinger installation.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from pathlib import Path
from typing import Iterable


STANDARD_PROTEIN_RESIDUES = {
    "ALA", "ARG", "ASN", "ASP", "CYS", "GLN", "GLU", "GLY", "HIS", "ILE",
    "LEU", "LYS", "MET", "PHE", "PRO", "SER", "THR", "TRP", "TYR", "VAL",
    "ASH", "CYM", "CYX", "GLH", "HID", "HIE", "HIP", "LYN", "MSE",
}


def json_value(value: object) -> str | int | float | bool | None:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _text(atom: object, *names: str) -> str:
    for name in names:
        value = getattr(atom, name, None)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _integer(atom: object, *names: str) -> int | None:
    for name in names:
        value = getattr(atom, name, None)
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _coordinates(atom: object) -> tuple[float, float, float]:
    xyz = getattr(atom, "xyz", None)
    if xyz is not None:
        values = tuple(float(value) for value in xyz)
    else:
        values = (float(getattr(atom, "x")), float(getattr(atom, "y")), float(getattr(atom, "z")))
    if len(values) != 3 or not all(math.isfinite(value) for value in values):
        raise ValueError("Structure coordinates must be three finite numbers")
    return values


def _atom_index(atom: object) -> int:
    value = _integer(atom, "index", "atom_index")
    return id(atom) if value is None else value


def _bonded_atoms(atom: object) -> list[object]:
    bonded = getattr(atom, "bonded_atoms", None)
    if bonded is not None:
        return list(bonded)
    bonds = getattr(atom, "bond", None)
    if bonds is None:
        return []
    neighbors: list[object] = []
    for bond in bonds:
        first = getattr(bond, "atom1", None)
        second = getattr(bond, "atom2", None)
        if first is atom and second is not None:
            neighbors.append(second)
        elif second is atom and first is not None:
            neighbors.append(first)
    return neighbors


def _residue_name(atom: object) -> str:
    name = _text(atom, "pdbres", "residue_name", "resname")
    if name:
        return name.upper()
    residue = getattr(atom, "residue", None)
    return _text(residue, "pdbres", "residue_name", "resname").upper()


def _is_hydrogen(atom: object) -> bool:
    return _text(atom, "element").upper() == "H" or _integer(
        atom, "atomic_number"
    ) == 1


def _is_protein_atom(atom: object) -> bool:
    return _residue_name(atom) in STANDARD_PROTEIN_RESIDUES


def _same_residue(first: object, second: object) -> bool:
    return (
        _text(first, "chain") == _text(second, "chain")
        and _integer(first, "resnum", "residue_number")
        == _integer(second, "resnum", "residue_number")
        and _residue_name(first) == _residue_name(second)
    )


def inspect_reaction_geometry(
    structure: object,
    *,
    attachment_chain: str,
    attachment_residue: int,
    nucleophilic_atom: str,
    severe_clash_distance: float = 1.2,
) -> dict[str, object]:
    """Return conservative geometry evidence for one CovDock pose."""

    if not math.isfinite(severe_clash_distance) or severe_clash_distance <= 0:
        raise ValueError("Clash distance must be finite and positive")
    atoms = list(getattr(structure, "atom"))
    target_matches = [
        atom
        for atom in atoms
        if _text(atom, "chain") == attachment_chain
        and _integer(atom, "resnum", "residue_number") == attachment_residue
        and _text(atom, "pdbname", "atom_name", "name").upper()
        == nucleophilic_atom.upper()
    ]
    evidence: dict[str, object] = {
        "target_atom_match_count": len(target_matches),
        "covalent_link_present": False,
        "covalent_bond_length_angstrom": None,
        "covalent_partner_atom_index": None,
        "covalent_partner_atom_name": None,
        "covalent_partner_residue_name": None,
        "minimum_nonbonded_heavy_atom_distance_angstrom": None,
        "severe_clash": None,
        "geometry_inspection_status": "TARGET_ATOM_NOT_UNIQUE",
    }
    if len(target_matches) != 1:
        return evidence

    target = target_matches[0]
    if _residue_name(target) not in {"CYS", "CYM", "CYX"} or _text(target, "element").upper() != "S":
        evidence["geometry_inspection_status"] = "TARGET_NOT_CYSTEINE_SULFUR"
        return evidence
    external_neighbors = [
        atom for atom in _bonded_atoms(target) if not _same_residue(atom, target)
    ]
    evidence["external_target_bond_count"] = len(external_neighbors)
    if len(external_neighbors) != 1:
        evidence["geometry_inspection_status"] = (
            "COVALENT_PARTNER_NOT_UNIQUE"
            if external_neighbors
            else "COVALENT_LINK_MISSING"
        )
        return evidence

    partner = external_neighbors[0]
    if _is_protein_atom(partner) or _is_hydrogen(partner):
        evidence["geometry_inspection_status"] = "PARTNER_NOT_LIGAND_HEAVY_ATOM"
        return evidence
    evidence.update(
        {
            "covalent_link_present": True,
            "covalent_bond_length_angstrom": math.dist(
                _coordinates(target), _coordinates(partner)
            ),
            "covalent_partner_atom_index": _atom_index(partner),
            "covalent_partner_atom_name": _text(
                partner, "pdbname", "atom_name", "name"
            ),
            "covalent_partner_residue_name": _residue_name(partner),
        }
    )

    # Identify the ligand as the non-protein connected component reached from
    # the covalent partner without walking back through the target atom.
    ligand: list[object] = []
    queue = [partner]
    seen = {_atom_index(target)}
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

    # SDK atom properties cross a Python/vendor boundary. Read coordinates and
    # identities once per heavy atom, not once per ligand/protein pair. Keep
    # exactly the same exhaustive distances and covalent-pair exclusion.
    protein = [(_atom_index(atom), _coordinates(atom)) for atom in atoms
               if _is_protein_atom(atom) and not _is_hydrogen(atom)]
    excluded_pair = tuple(sorted((_atom_index(target), _atom_index(partner))))
    minimum_distance: float | None = None
    for ligand_atom in ligand:
        if _is_hydrogen(ligand_atom):
            continue
        ligand_index = _atom_index(ligand_atom)
        ligand_coordinates = _coordinates(ligand_atom)
        for protein_index, protein_coordinates in protein:
            pair = tuple(sorted((ligand_index, protein_index)))
            if pair == excluded_pair:
                continue
            distance = math.dist(ligand_coordinates, protein_coordinates)
            if minimum_distance is None or distance < minimum_distance:
                minimum_distance = distance
    evidence["ligand_heavy_atom_count"] = sum(
        not _is_hydrogen(atom) for atom in ligand
    )
    evidence["minimum_nonbonded_heavy_atom_distance_angstrom"] = minimum_distance
    evidence["severe_clash"] = (
        minimum_distance < severe_clash_distance
        if minimum_distance is not None
        else None
    )
    evidence["geometry_inspection_status"] = (
        "SUCCESS" if ligand else "LIGAND_COMPONENT_NOT_IDENTIFIED"
    )
    return evidence


def inspect_structures(
    structures: Iterable[object],
    *,
    attachment_chain: str | None = None,
    attachment_residue: int | None = None,
    nucleophilic_atom: str = "SG",
    severe_clash_distance: float = 1.2,
) -> tuple[list[dict[str, object]], list[str]]:
    rows: list[dict[str, object]] = []
    property_names: set[str] = set()
    for index, structure in enumerate(structures):
        properties = {
            str(key): json_value(value)
            for key, value in getattr(structure, "property").items()
        }
        geometry: dict[str, object] = {}
        if attachment_chain is not None and attachment_residue is not None:
            geometry = inspect_reaction_geometry(
                structure,
                attachment_chain=attachment_chain,
                attachment_residue=attachment_residue,
                nucleophilic_atom=nucleophilic_atom,
                severe_clash_distance=severe_clash_distance,
            )
        property_names.update(properties)
        property_names.update(geometry)
        rows.append(
            {
                **properties,
                "pose_index": index,
                "title": getattr(structure, "title", ""),
                **geometry,
            }
        )
    score_properties = sorted(
        key
        for key in property_names
        if any(
            marker in key.lower()
            for marker in (
                "score", "gscore", "affinity", "strain", "prime", "dock"
            )
        )
    )
    return rows, score_properties


def readback_request(value):
    """Validate the host's bounded read-only request before loading the SDK."""
    if (not isinstance(value, dict) or set(value) != {"version", "files", "attachment_residue"} or
            value["version"] != "covdock-structure-readback-v1" or
            value["attachment_residue"] not in ("A:773", "A:797") or
            not isinstance(value["files"], list) or not 1 <= len(value["files"]) <= 100):
        raise ValueError("Invalid CovDock structure readback request")
    paths = value["files"]
    if (any(not isinstance(path, str) or not path.startswith("/") or
            any(part in (".", "..", "") for part in path[1:].split("/")) or
            any(ord(char) < 32 or ord(char) == 127 or char == "\\" for char in path) for path in paths) or
            len(set(paths)) != len(paths)):
        raise ValueError("Readback requires distinct canonical absolute structure paths")
    return value


def inspect_readback(request):
    """Use the real SDK only; no docking, LigPrep, output files or fallback."""
    request = readback_request(request)
    from schrodinger.structure import StructureReader

    audits = []
    chain, residue = request["attachment_residue"].split(":")
    for name in request["files"]:
        with StructureReader(name) as reader:
            # Read the whole container to preserve its actual zero-based entries.
            def bounded():
                for index, structure in enumerate(reader):
                    if index >= 1000 or structure.atom_total > 50000:
                        raise ValueError("CovDock structure exceeds readback limits")
                    yield structure
            rows, properties = inspect_structures(bounded(), attachment_chain=chain,
                attachment_residue=int(residue), nucleophilic_atom="SG", severe_clash_distance=1.2)
        audits.append({"input": name, "rows": rows, "pose_count": len(rows),
                       "candidate_score_properties": properties, "geometry_requested": True})
    return {"version": request["version"], "attachment_residue": request["attachment_residue"],
            "audits": audits, "scientific_quality_verified": False}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--readback-stdin", action="store_true")
    ap.add_argument("--input", type=Path)
    ap.add_argument("--output-prefix", type=Path)
    ap.add_argument("--attachment-chain")
    ap.add_argument("--attachment-residue", type=int)
    ap.add_argument("--nucleophilic-atom", default="SG")
    ap.add_argument("--severe-clash-distance", type=float, default=1.2)
    args = ap.parse_args()
    if args.readback_stdin:
        if (args.input or args.output_prefix or args.attachment_chain or args.attachment_residue or
                args.nucleophilic_atom != "SG" or args.severe_clash_distance != 1.2):
            ap.error("Readback mode cannot write inspection files or override frozen roles")
        raw = sys.stdin.buffer.read(128 * 1024 + 1)
        if len(raw) > 128 * 1024:
            raise ValueError("Readback request exceeds bounded size")
        result = json.dumps(inspect_readback(json.loads(raw)), allow_nan=False)
        if len(result.encode()) > 4 * 1024 * 1024:
            raise ValueError("Readback response exceeds bounded size")
        print(result)
        return 0
    if args.input is None or args.output_prefix is None:
        ap.error("--input and --output-prefix are required outside readback mode")
    if (args.attachment_chain is None) != (args.attachment_residue is None):
        ap.error("--attachment-chain and --attachment-residue must be provided together")

    from schrodinger.structure import StructureReader

    rows, score_properties = inspect_structures(
        StructureReader(str(args.input)),
        attachment_chain=args.attachment_chain,
        attachment_residue=args.attachment_residue,
        nucleophilic_atom=args.nucleophilic_atom,
        severe_clash_distance=args.severe_clash_distance,
    )
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    args.output_prefix.with_suffix(".json").write_text(
        json.dumps(
            {
                "input": str(args.input.resolve()),
                "pose_count": len(rows),
                "candidate_score_properties": score_properties,
                "geometry_requested": args.attachment_chain is not None,
                "rows": rows,
            },
            indent=2,
        )
        + "\n"
    )
    fieldnames = sorted({key for row in rows for key in row})
    preferred = [name for name in ("pose_index", "title") if name in fieldnames]
    fieldnames = [*preferred, *[name for name in fieldnames if name not in preferred]]
    with args.output_prefix.with_suffix(".csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    print(
        json.dumps(
            {
                "pose_count": len(rows),
                "candidate_score_properties": score_properties,
                "json": str(args.output_prefix.with_suffix(".json")),
                "csv": str(args.output_prefix.with_suffix(".csv")),
            },
            indent=2,
        )
    )
    return 0 if rows else 1


if __name__ == "__main__":
    raise SystemExit(main())
