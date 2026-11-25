"""Standard peptide atom naming and coordinate-independent chiral orientations."""
from functools import lru_cache


def peptide_reference(sequence):
    from rdkit import Chem
    reference = Chem.MolFromSequence(sequence)
    if reference is None:
        raise ValueError('Standard peptide reference could not be built')
    corrected = []
    for number, code in enumerate(sequence, 1):
        if code != 'I':
            continue
        names = {a.GetPDBResidueInfo().GetName().strip(): a for a in reference.GetAtoms()
                 if a.GetPDBResidueInfo().GetResidueNumber() == number}
        neighbors = {a.GetPDBResidueInfo().GetName().strip() for a in names['CD1'].GetNeighbors()}
        if neighbors == {'CG2'}:
            names['CG1'].GetPDBResidueInfo().SetName(' CG2')
            names['CG2'].GetPDBResidueInfo().SetName(' CG1')
            corrected.append(number)
        elif neighbors != {'CG1'}:
            raise ValueError('Unsupported ILE template graph')
    return reference, corrected


@lru_cache(maxsize=40)
def standard_orientation(code, center_name):
    """Derive signed-volume orientation from tags, never proposed coordinates."""
    from rdkit import Chem
    if code not in 'ACDEFHIKLMNPQRSTVWY' or center_name not in (
        ('CA', 'CB') if code in 'IT' else ('CA',)):
        raise ValueError('Unsupported standard amino-acid stereocenter')
    reference, _ = peptide_reference('A' + code + 'A')
    Chem.AssignStereochemistry(reference, cleanIt=True, force=True)
    center = next(a for a in reference.GetAtoms() if a.GetPDBResidueInfo().GetResidueNumber() == 2
                  and a.GetPDBResidueInfo().GetName().strip() == center_name)
    label = center.GetProp('_CIPCode')
    neighbors = sorted(center.GetNeighbors(), key=lambda a: a.GetPDBResidueInfo().GetName().strip())
    if len(neighbors) != 3:
        raise ValueError('Unsupported heavy-atom stereocenter neighborhood')
    matches = []
    for sign in (-1, 1):
        trial = Chem.Mol(reference)
        conformer = Chem.Conformer(trial.GetNumAtoms())
        conformer.Set3D(True)
        for index in range(trial.GetNumAtoms()):
            conformer.SetAtomPosition(index, (7.0 + index, 13.0 + index**2, 19.0 + index**3))
        conformer.SetAtomPosition(center.GetIdx(), (0, 0, 0))
        for atom, point in zip(neighbors, [(1, 0, 0), (0, 1, 0), (0, 0, sign)]):
            conformer.SetAtomPosition(atom.GetIdx(), point)
        trial.AddConformer(conformer)
        Chem.AssignStereochemistryFrom3D(trial, replaceExistingTags=True)
        Chem.AssignStereochemistry(trial, cleanIt=True, force=True)
        actual = trial.GetAtomWithIdx(center.GetIdx())
        if actual.HasProp('_CIPCode') and actual.GetProp('_CIPCode') == label:
            matches.append(sign)
    if len(matches) != 1:
        raise ValueError('Standard peptide does not define a unique orientation')
    return (label, tuple(a.GetPDBResidueInfo().GetName().strip() for a in neighbors), matches[0])


def chirality_terms(atoms):
    """Bind template orientations to the exact prepared heavy-atom inventory."""
    from .receptor_preparation import AMINO_ACIDS
    codes = {v: k for k, v in AMINO_ACIDS.items()}
    indices = {(a['entity_position'], a['name']): a for a in atoms}
    if len(indices) != len(atoms) or [a['index'] for a in atoms] != list(range(len(atoms))):
        raise ValueError('Chiral terms require a unique ordered atom inventory')
    residues = {a['entity_position']: a['resname'] for a in atoms}
    if any(a['resname'] != residues[a['entity_position']] for a in atoms):
        raise ValueError('Inconsistent residue identity')
    terms = []
    for position, residue in sorted(residues.items()):
        code = codes[residue]
        if code == 'G':
            continue
        for center in (('CA', 'CB') if code in 'IT' else ('CA',)):
            label, neighbors, sign = standard_orientation(code, center)
            selected = [indices[position, name] for name in (center, *neighbors)]
            terms.append({'entity_position': position, 'author_residue': selected[0]['author_residue'],
                'resname': residue, 'center': center, 'expected_cip': label,
                'neighbor_names': list(neighbors), 'sign': sign,
                'particles': [a['index'] for a in selected],
                'movable': any(a['original_serial'] is None for a in selected)})
    return terms


def chiral_force(terms, strength, minimum_volume):
    """OpenMM preparation-only penalty; nm, kJ/mol, kJ/mol/nm^6 units.

    Volume denotes the scalar triple product, without the tetrahedral 1/6 factor.
    """
    import math
    import openmm
    if any(isinstance(v, bool) or not math.isfinite(v) or v <= 0 for v in (strength, minimum_volume)):
        raise ValueError('Positive finite chiral penalty parameters required')
    force = openmm.CustomCompoundBondForce(4,
        '0.5*k*min(0,s*v-vmin)^2;'
        'v=(x2-x1)*((y3-y1)*(z4-z1)-(z3-z1)*(y4-y1))'
        '-(y2-y1)*((x3-x1)*(z4-z1)-(z3-z1)*(x4-x1))'
        '+(z2-z1)*((x3-x1)*(y4-y1)-(y3-y1)*(x4-x1))')
    force.addGlobalParameter('k', strength)
    force.addGlobalParameter('vmin', minimum_volume)
    force.addPerBondParameter('s')
    for term in terms:
        if (type(term['sign']) is not int or term['sign'] not in (-1, 1)
                or type(term['movable']) is not bool or len(term['particles']) != 4
                or any(type(i) is not int or i < 0 for i in term['particles'])
                or len(set(term['particles'])) != 4):
            raise ValueError('Invalid chiral term')
        if term['movable']:
            force.addBond(term['particles'], [term['sign']])
    return force
