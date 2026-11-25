"""Validate a registered experimental reference without guessing residue offsets."""
from pathlib import Path
import math

from rdkit import Chem

from .jobs import sha256_file


def validate_reference(asset: dict, structure: dict, site: dict) -> dict:
    pdb = Path(asset['pdb'])
    sdf = Path(asset['reference_ligand'])
    for path, digest in [(pdb, asset['pdb_sha256']), (sdf, asset['reference_sha256'])]:
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError('Registered docking reference missing or changed')
    if asset['pdb_sha256'] != structure['input_sha256']:
        raise ValueError('Docking receptor differs from site-discovery structure')
    # Component identity is supplied by a registered asset, not inferred from
    # the chemical prescreen reference SMILES (which may be a different ligand).
    component = asset['component_id']
    component_chain = asset['component_chain']
    component_residue = str(asset['component_residue_id'])
    model = int(structure['model'])
    current_model = 1
    atoms = []
    target_sg = []
    for line in pdb.read_text().splitlines():
        if line.startswith('MODEL '):
            current_model = int(line[10:14])
        if current_model != model or line[:6].strip() not in {'ATOM', 'HETATM'}:
            continue
        chain, residue = line[21:22].strip(), line[22:27].strip()
        xyz = tuple(float(line[a:b]) for a, b in [(30, 38), (38, 46), (46, 54)])
        if not all(math.isfinite(v) for v in xyz):
            raise ValueError('Nonfinite receptor coordinates')
        if (chain == site['author_chain'] and residue == str(site['author_residue_id'])
                and line[17:20].strip() == 'CYS' and line[12:16].strip() == 'SG'):
            if line[16:17].strip():
                raise ValueError("Alternate target conformer requires explicit preparation")
            target_sg.append(xyz)
        if (line[:6].strip() == 'HETATM' and chain == component_chain and residue == component_residue
                and line[17:20].strip() == component):
            if line[16:17].strip():
                raise ValueError('Alternate reference conformers need explicit preparation')
            element = line[76:78].strip().upper()
            if not element:
                raise ValueError('Reference atom lacks explicit element')
            if element not in {'H', 'D'}:
                atoms.append((element, xyz))
    if len(target_sg) != 1:
        raise ValueError('Selected author CYS SG missing or ambiguous in receptor')
    molecules = list(Chem.SDMolSupplier(str(sdf), removeHs=True))
    if len(molecules) != 1 or molecules[0] is None or not molecules[0].GetNumConformers():
        raise ValueError('Reference requires one valid posed molecule')
    molecule = molecules[0]
    if molecule.GetNumHeavyAtoms() != len(atoms) or not atoms:
        raise ValueError('Reference heavy-atom coverage differs from experimental component')
    remaining = list(atoms)
    conformer = molecule.GetConformer()
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() == 1:
            continue
        xyz = tuple(conformer.GetAtomPosition(atom.GetIdx()))
        matches = [i for i, (element, point) in enumerate(remaining)
                   if element == atom.GetSymbol().upper() and math.dist(xyz, point) <= 0.002]
        if len(matches) != 1:
            raise ValueError('Reference pose is not a unique match to experimental coordinates')
        remaining.pop(matches[0])
    if remaining:
        raise ValueError('Unmatched experimental reference atoms')
    return {'pdb': str(pdb), 'pdb_sha256': asset['pdb_sha256'],
            'reference_ligand': str(sdf), 'reference_sha256': asset['reference_sha256'],
            'component_id': component, 'component_chain': component_chain,
            'component_residue_id': component_residue, 'model_id': model,
            'covres': f"{site['author_chain']}:CYS:{site['author_residue_id']}",
            'canonical_position': site['canonical_position'], 'matched_heavy_atoms': len(atoms),
            'limitations': ['Coordinate correspondence is not a claim of covalent activity or validated reference bond orders.']}
