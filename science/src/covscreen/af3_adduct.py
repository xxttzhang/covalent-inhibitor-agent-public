"""Ligand-side, hydrogen-capped adduct graphs for explicit AF3 Cys bonds.

These are connectivity inputs, not reaction-rate or selectivity predictions.
Propiolamide product alkene geometry is unspecified, not an assigned E/Z claim.
"""
from rdkit import Chem


PATTERNS = {
    "acrylamide": "[C:1]=[C:2][C](=O)N",
    "chloroacetamide": "[CH2:1]([Cl:2])[C](=O)N",
    "vinyl_sulfonamide": "[C:1]=[C:2][S](=O)(=O)N",
    "vinyl_sulfone": "[C:1]=[C:2][S](=O)(=O)[#6]",
    "propiolamide": "[CH:1]#[C:2][C](=O)N",
}


def transform_adduct(smiles: str, warhead_class: str):
    if warhead_class not in PATTERNS:
        raise ValueError(f"Unsupported warhead: {warhead_class}")
    mol = Chem.MolFromSmiles(smiles)
    if mol is None or len(Chem.GetMolFrags(mol)) != 1:
        raise ValueError("Expected one valid ligand component")
    query = Chem.MolFromSmarts(PATTERNS[warhead_class])
    positions = {a.GetAtomMapNum(): a.GetIdx() for a in query.GetAtoms() if a.GetAtomMapNum()}
    sites = sorted({(m[positions[1]], m[positions[2]]) for m in mol.GetSubstructMatches(query)})
    if len(sites) != 1:
        raise ValueError(f"Expected one {warhead_class} site, observed {len(sites)}")
    electrophile, neighbor = sites[0]
    # Retain a stable atom identity even when deleting an earlier atom reindexes.
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    mol.GetAtomWithIdx(electrophile).SetAtomMapNum(906)
    rw = Chem.RWMol(mol)
    if warhead_class == "chloroacetamide":
        rw.RemoveAtom(neighbor)
        transformation = "C-Cl cleavage; chloride excluded from ligand component"
    else:
        bond = rw.GetBondBetweenAtoms(electrophile, neighbor)
        bond.SetBondType(Chem.BondType.DOUBLE if warhead_class == "propiolamide" else Chem.BondType.SINGLE)
        bond.SetStereo(Chem.BondStereo.STEREONONE)
        transformation = "C#C to C=C" if warhead_class == "propiolamide" else "C=C to C-C"
    product = rw.GetMol()
    # SMILES bracket H counts must be recomputed after changing connectivity.
    target = next(a for a in product.GetAtoms() if a.GetAtomMapNum() == 906)
    target.SetNumExplicitHs(0)
    target.SetNoImplicit(False)
    Chem.SanitizeMol(product)
    target_index = target.GetIdx()
    target.SetAtomMapNum(0)
    return product, target_index, {
        "warhead_class": warhead_class, "warhead_match_count": 1,
        "warhead_transformation": transformation,
        "warhead_selection_rule": "unique_declared_family_site",
        "ligand_representation": "hydrogen_capped_component_with_external_Cys_bond",
        "product_stereochemistry": "unspecified_new_alkene" if warhead_class == "propiolamide" else "no_new_stereochemistry_assigned",
    }
