"""Check a mapped Cys and one parent-ligand instance in unmodified PDB coordinates.

This is an input audit, not an attachment planner or an activity predictor.
Ambiguous models/alternate locations fail visibly instead of being merged.
"""
from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

from .pdb_coordinates import read_pdb_coordinates


def audit_structure_site(
    pdb_path: Path, mapping: dict, component: str, *,
    ligand_chain: str | None = None, ligand_residue: str | None = None,
    model: int | None = None, altloc: str | None = None,
) -> dict:
    if mapping.get("status") != "sequence_mapping_verified_coordinates_pending":
        raise ValueError("A verified sequence-mapping result is required")
    if not re.fullmatch(r"[A-Z0-9]{1,3}", component):
        raise ValueError("PDB audit requires a 1-3 character CCD ID; use mmCIF for larger IDs")
    if (ligand_chain is None) != (ligand_residue is None):
        raise ValueError("Specify ligand author chain and residue together, or omit BOTH to select a unique deposited instance. Never guess residue numbers.")
    if altloc is not None and not re.fullmatch(r"[A-Za-z0-9]", altloc):
        raise ValueError("Select one explicit alternate location")
    if model is not None and (type(model) is not int or model < 1):
        raise ValueError("Model must be a positive integer")
    chain = str(mapping["auth_asym_id"])
    residue = str(mapping["author_residue_id"])
    raw, lines, atoms, selected_model = read_pdb_coordinates(pdb_path, mapping["entry_id"], model)
    sites = [a for a in atoms if a["chain"] == chain and a["residue"] == residue
             and a["resname"] == "CYS" and a["name"] == "SG"]
    ligands = [a for a in atoms if a["resname"] == component]
    instances = sorted({(a["chain"], a["residue"]) for a in ligands})
    if ligand_chain is None:
        if len(instances) != 1:
            raise ValueError(f"Select a unique ligand instance; candidates={instances}")
        ligand_chain, ligand_residue = instances[0]
    ligands = [a for a in ligands if (a["chain"], a["residue"]) == (ligand_chain, ligand_residue)]
    if not sites:
        observed = sorted({(a["resname"], a["name"]) for a in atoms
                           if a["chain"] == chain and a["residue"] == residue})
        raise ValueError(
            f"Required CYS SG coordinate not found: author chain={chain}, residue={residue}; "
            f"observed residue/atom labels={observed}. "
            "This exact-label check does not establish that all sulfur coordinates are absent "
            "or determine oxidation/modification chemistry; no modified residue is accepted as CYS.")
    if not ligands:
        raise ValueError(f"Selected ligand instance is absent; Cys SG IS present. Actual {component} (author chain, residue) candidates={instances}. Omit BOTH selectors for a unique instance, or select an actual listed pair.")
    present_altlocs = {a["altloc"] for a in sites + ligands if a["altloc"]}
    if present_altlocs and altloc is None:
        raise ValueError(f"Alternate coordinates require explicit selection: {sorted(present_altlocs)}")
    if altloc is not None and altloc not in present_altlocs:
        raise ValueError("Requested alternate location is absent")
    keep = lambda a: not a["altloc"] or a["altloc"] == altloc
    chosen_sites = [a for a in sites if keep(a)]
    chosen_ligands = [a for a in ligands if keep(a)]
    if len(chosen_sites) != 1 or chosen_sites[0]["element"] != "S":
        raise ValueError("Expected one explicitly identified sulfur atom at mapped Cys SG")
    if {a["name"] for a in chosen_ligands} != {a["name"] for a in ligands}:
        raise ValueError("Selected alternate location has incomplete ligand atom coverage")
    if len({a["name"] for a in chosen_ligands}) != len(chosen_ligands):
        raise ValueError("Duplicate ligand atom names after model/altloc selection")
    if any(a["occupancy"] <= 0 for a in chosen_sites + chosen_ligands):
        raise ValueError("Selected coordinates contain zero-occupancy atoms")
    if any(not a["element"] for a in chosen_ligands):
        raise ValueError("Ligand element fields are missing")
    heavy = [a for a in chosen_ligands if a["element"] not in {"H", "D"}]
    if not heavy:
        raise ValueError("Selected ligand has no heavy atoms")
    # Do not equate missing connectivity records with proven noncovalent identity.
    ligand_serials = {a["serial"] for a in ligands}
    protein_serials = {a["serial"] for a in atoms if a["record"] == "ATOM"}
    explicit_connections = []
    for line in lines:
        if line.startswith("LINK  "):
            partners = [(line[21:22].strip(), line[22:26].strip() + line[26:27].strip(), line[17:20].strip()),
                        (line[51:52].strip(), line[52:56].strip() + line[56:57].strip(), line[47:50].strip())]
            if (ligand_chain, ligand_residue, component) in partners:
                explicit_connections.append({"record": "LINK", "partners": partners})
        elif line.startswith("CONECT"):
            serials = [int(line[i:i+5]) for i in range(6, len(line), 5) if line[i:i+5].strip()]
            if serials:
                for other in serials[1:]:
                    first = serials[0]
                    if ((first in ligand_serials and other in protein_serials) or
                            (other in ligand_serials and first in protein_serials)):
                        explicit_connections.append({"record": "CONECT", "serials": [first, other]})
    distances = sorted((math.dist(chosen_sites[0]["xyz"], a["xyz"]), a["name"]) for a in heavy)
    from .ligand_parent_pair import cys_connections
    return {"schema_version": "1.0", "status": "coordinate_checks_passed_linkage_review_pending"
            if not explicit_connections else "explicit_linkage_requires_review",
        "entry_id": mapping["entry_id"], "uniprot": mapping["uniprot"],
        "uniprot_residue": mapping["uniprot_residue"], "protein_author_chain": chain,
        "cys_author_residue": residue, "model": selected_model, "altloc": altloc,
        "component": component, "ligand_author_chain": ligand_chain,
        "ligand_author_residue": ligand_residue, "ligand_heavy_atom_count": len(heavy),
        "ligand_heavy_atoms": [{"name": a["name"], "element": a["element"]} for a in heavy],
        "minimum_occupancy": min(a["occupancy"] for a in chosen_sites + chosen_ligands),
        "closest_ligand_atom": distances[0][1], "sg_ligand_min_distance_angstrom": distances[0][0],
        "explicit_connections": explicit_connections,
        "cys_environment": cys_connections(lines, atoms, chosen_sites[0]),
        "structure_sha256": hashlib.sha256(raw).hexdigest(),
        "campaign_ready": False,
        "remaining_checks": ["CCD chemical identity and atom completeness", "noncovalent parent evidence",
                             "mutation state and species", "binding pose suitability for derivatization"],
        "warnings": ["No LINK/CONECT record is not proof of noncovalent binding.",
                     "SG-ligand distance alone does not prove attachment feasibility or activity."]}
