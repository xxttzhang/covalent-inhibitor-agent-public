"""Cross-check a coordinate-audited parent against a frozen CCD definition.

This checks declared chemical identity and heavy-atom completeness, not measured
bond orders, stereochemistry of the bound coordinates, or noncovalent activity.
"""
import hashlib
import re
from pathlib import Path
from Bio.PDB.MMCIF2Dict import MMCIF2Dict


def audit_parent_chemical_identity(ccd_path: Path, coordinate_audit: dict, expected_inchikey: str) -> dict:
    if not re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", expected_inchikey):
        raise ValueError("Full expected InChIKey is required")
    if coordinate_audit.get("status") not in {
        "coordinate_checks_passed_linkage_review_pending", "explicit_linkage_requires_review"
    }:
        raise ValueError("Successful coordinate audit required")
    ccd = MMCIF2Dict(str(ccd_path))
    component = coordinate_audit["component"]
    if ccd.get("_chem_comp.id") != [component]:
        raise ValueError("CCD component identity mismatch")
    fields = [ccd.get("_chem_comp_atom." + key, []) for key in ("comp_id", "atom_id", "type_symbol")]
    if not fields[0] or len({len(col) for col in fields}) != 1 or set(fields[0]) != {component}:
        raise ValueError("Malformed or mixed-component CCD atom table")
    names, elements = fields[1:]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate CCD atom names")
    atoms = {name: element.upper() for name, element in zip(names, elements) if element.upper() not in {"H", "D"}}
    observed = coordinate_audit.get("ligand_heavy_atoms", [])
    if not observed or len({a["name"] for a in observed}) != len(observed):
        raise ValueError("Missing or duplicate observed atom identities")
    observed_atoms = {a["name"]: a["element"].upper() for a in observed}
    if atoms != observed_atoms:
        raise ValueError("CCD/coordinate heavy atom identities are incomplete or inconsistent")
    descriptors = [ccd.get("_pdbx_chem_comp_descriptor." + key, [])
                   for key in ("comp_id", "type", "descriptor")]
    if not descriptors[0] or len({len(col) for col in descriptors}) != 1:
        raise ValueError("Malformed CCD descriptor table")
    keys = {value for comp, kind, value in zip(*descriptors) if comp == component and kind == "InChIKey"}
    if keys != {expected_inchikey}:
        raise ValueError("CCD full InChIKey does not match requested parent identity")
    bonds = [ccd.get("_chem_comp_bond." + key, []) for key in ("atom_id_1", "atom_id_2", "value_order")]
    if not bonds[0] or len({len(col) for col in bonds}) != 1:
        raise ValueError("Missing or malformed CCD bond table")
    edges = set()
    for left, right, order in zip(*bonds):
        edge = tuple(sorted((left, right)))
        if left not in names or right not in names or left == right or edge in edges:
            raise ValueError("Invalid CCD bond endpoints")
        if order not in {"SING", "DOUB", "TRIP", "AROM"}:
            raise ValueError("Unsupported CCD bond order")
        edges.add(edge)
    return {"status": "declared_parent_identity_and_heavy_atoms_verified",
            "component": component, "expected_inchikey": expected_inchikey,
            "ccd_sha256": hashlib.sha256(ccd_path.read_bytes()).hexdigest(),
            "structure_sha256": coordinate_audit["structure_sha256"],
            "heavy_atom_count": len(atoms), "ccd_bond_count": len(edges),
            "coordinate_atom_order": [a["name"] for a in observed],
            "campaign_ready": False,
            "limitations": ["CCD connectivity is a definition, not observed bond-order evidence.",
                            "Bound-coordinate stereochemistry is not assessed.",
                            "Noncovalent engagement and biological activity remain unverified."]}
