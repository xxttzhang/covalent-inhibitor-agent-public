"""Build a CCD-defined RDKit template in observed PDB atom order.

Ideal CCD coordinates define stereochemistry; these are NOT a predicted binding
pose. The existing parent-pose reader replaces coordinates with the audited PDB.
"""
import hashlib
import json
import math
from pathlib import Path
from Bio.PDB.MMCIF2Dict import MMCIF2Dict
from rdkit import Chem

from .parent_chemical_identity import audit_parent_chemical_identity
from .structure_site_audit import audit_structure_site


def ccd_template_molecule(ccd_path, atom_order, expected_inchikey):
    data = MMCIF2Dict(str(ccd_path))
    names = data["_chem_comp_atom.atom_id"]
    symbols = data["_chem_comp_atom.type_symbol"]
    charges = data["_chem_comp_atom.charge"]
    xyz = [data["_chem_comp_atom.pdbx_model_Cartn_" + axis + "_ideal"] for axis in "xyz"]
    if len({len(col) for col in (names, symbols, charges, *xyz)}) != 1 or len(names) != len(set(names)):
        raise ValueError("Malformed CCD atom/ideal-coordinate table")
    editable = Chem.RWMol()
    indices = {}
    conformer = Chem.Conformer(len(names))
    conformer.Set3D(True)
    for index, (name, symbol, charge, x, y, z) in enumerate(zip(names, symbols, charges, *xyz)):
        point = tuple(float(v) for v in (x, y, z))
        if not all(math.isfinite(v) for v in point):
            raise ValueError("Nonfinite CCD ideal coordinate")
        # CCD uses uppercase two-letter elements (CL/BR); RDKit expects Cl/Br.
        atom = Chem.Atom(symbol.title())
        atom.SetFormalCharge(int(charge))
        atom.SetNoImplicit(True)  # The CCD supplies explicit hydrogen atoms.
        atom.SetProp("pdb_atom_name", name)
        indices[name] = editable.AddAtom(atom)
        conformer.SetAtomPosition(index, point)
    bond_columns = [data["_chem_comp_bond." + field] for field in ("atom_id_1", "atom_id_2", "value_order")]
    if len({len(col) for col in bond_columns}) != 1:
        raise ValueError("Malformed CCD bond table")
    kinds = {"SING": Chem.BondType.SINGLE, "DOUB": Chem.BondType.DOUBLE,
             "TRIP": Chem.BondType.TRIPLE, "AROM": Chem.BondType.AROMATIC}
    for left, right, order in zip(*bond_columns):
        if order not in kinds or left not in indices or right not in indices:
            raise ValueError("Invalid CCD bond")
        editable.AddBond(indices[left], indices[right], kinds[order])
    molecule = editable.GetMol()
    molecule.AddConformer(conformer)
    Chem.SanitizeMol(molecule)
    Chem.AssignStereochemistryFrom3D(molecule)
    if Chem.MolToInchiKey(molecule) != expected_inchikey:
        raise ValueError("Reconstructed CCD graph/stereochemistry does not match full InChIKey")
    molecule = Chem.RemoveHs(molecule)
    if len(Chem.GetMolFrags(molecule)) != 1 or any(a.GetNumRadicalElectrons() for a in molecule.GetAtoms()):
        raise ValueError("Parent template must be single-component and radical-free")
    heavy = {a.GetProp("pdb_atom_name"): a.GetIdx() for a in molecule.GetAtoms()}
    if len(atom_order) != len(set(atom_order)) or set(atom_order) != set(heavy):
        raise ValueError("Observed atom order does not cover the CCD heavy atoms exactly")
    return Chem.RenumberAtoms(molecule, [heavy[name] for name in atom_order])


def build_parent_template(ccd_path: Path, pdb_path: Path, mapping: dict,
                          component: str, expected_inchikey: str, out: Path, **selection):
    coordinate_audit = audit_structure_site(pdb_path, mapping, component, **selection)
    identity = audit_parent_chemical_identity(ccd_path, coordinate_audit, expected_inchikey)
    molecule = ccd_template_molecule(ccd_path, identity["coordinate_atom_order"], expected_inchikey)
    molecule.SetProp("COORDINATE_ROLE", "CCD_ideal_template_not_binding_pose")
    molecule.SetProp("ATOM_NAMES_IN_ORDER", json.dumps(identity["coordinate_atom_order"]))
    out.mkdir(parents=True, exist_ok=False)
    path = out / "parent_template.sdf"
    with Chem.SDWriter(str(path)) as writer:
        writer.write(molecule)
    reread = Chem.MolFromMolFile(str(path), removeHs=True, sanitize=True)
    if reread is None or Chem.MolToInchiKey(reread) != expected_inchikey:
        raise ValueError("SDF round-trip changes parent chemical identity")
    manifest = {"status": "parent_template_prepared_review_pending", "campaign_ready": False,
                "template_path": path.name, "template_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "chemical_identity": identity, "coordinate_audit": coordinate_audit,
                "canonical_smiles": Chem.MolToSmiles(reread),
                "coordinate_role": "CCD_ideal_template_not_binding_pose",
                "limitations": ["Parent pose coordinates must be reconstructed from the audited experimental PDB.",
                                "Noncovalent evidence, mutation/species and suitability review are still required."]}
    (out / "parent_template.json").write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest
