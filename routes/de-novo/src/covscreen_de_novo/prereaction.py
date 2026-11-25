"""Independent noncovalent pose assessment for the fixed EGFR study.

No generation coordinates, covalent topology, or caller-supplied PASS is used.
The windows are preregistered engineering screens, not reaction-rate calibration.
This module measures real poses; native job provenance is enforced by the caller.
"""
from copy import deepcopy
import hashlib
import importlib.metadata
import json
from pathlib import Path

import numpy as np
from rdkit import Chem
from rdkit.Chem import AllChem

VERSION = 'denovo-independent-prereaction-v1'
REGISTRY = Path(__file__).resolve().parents[2] / 'registry/egfr_c797_v1'
MAX_MAPPINGS = 4096
POLICY = {
    'scope': 'EGFR_WT_4G5J_A_C797_acrylamide_exploratory_prereaction',
    'pose_selection': 'lowest_vina_score_then_output_index_before_geometry',
    'sg_beta_distance_A': [2.8, 4.0],
    'sg_beta_alpha_angle_deg': [80.0, 140.0],
    'minimum_nonbonded_vdw_ratio': 0.75,
    'generated_coordinates_used': False,
    'coordinate_constraints': False,
    'covalent_bond_present': False,
    'reactivity_calibrated': False,
    'binding_affinity_verified': False,
    'experimental_prereaction_reference_available': False,
}
DOCKING = {'seed': 20261029, 'exhaustiveness': 32, 'n_poses': 20,
           'energy_range': 100.0, 'cpu': 1, 'padding_A': 6.0, 'minimum_box_A': 22.0}
PB_KEYS = ('mol_pred_loaded', 'sanitization', 'inchi_convertible', 'all_atoms_connected',
    'no_radicals', 'bond_lengths', 'bond_angles', 'internal_steric_clash',
    'aromatic_ring_flatness', 'non-aromatic_ring_non-flatness',
    'double_bond_flatness', 'internal_energy')


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def freeze():
    import yaml
    rules = json.loads((REGISTRY/'control/rules.json').read_text())
    if (rules['pre_SG_beta_distance_A'] != POLICY['sg_beta_distance_A']
            or rules['pre_SG_beta_alpha_angle_deg'] != POLICY['sg_beta_alpha_angle_deg']
            or rules['protein_clash_vdw_ratio_min'] != POLICY['minimum_nonbonded_vdw_ratio']):
        raise ValueError('Registered geometry policy changed; freeze a reviewed new version')
    return {'version': VERSION, 'policy': deepcopy(POLICY), 'docking': deepcopy(DOCKING),
        'max_mappings': MAX_MAPPINGS, 'posebusters_keys': list(PB_KEYS),
        'posebusters_config': yaml.safe_load((REGISTRY/'control/posebusters.yml').read_text()),
        'implementation_sha256': digest(__file__),
        'registry_sha256': {name: digest(REGISTRY/name) for name in
            ('control/rules.json', 'control/posebusters.yml', 'assets/protein_A.pdb')},
        'versions': {name: importlib.metadata.version(name) for name in
            ('rdkit', 'numpy', 'posebusters', 'meeko')}}


def molecule(path, *, two_dimensional=False):
    items = list(Chem.SDMolSupplier(str(path), removeHs=False))
    if len(items) != 1 or items[0] is None:
        raise ValueError('Expected exactly one complete molecule')
    result = Chem.RemoveHs(items[0])
    Chem.SanitizeMol(result)
    if (len(Chem.GetMolFrags(result)) != 1 or result.GetNumConformers() != 1
            or not np.isfinite(result.GetConformer().GetPositions()).all()
            or result.GetNumHeavyAtoms() > 256
            or any(atom.GetNumRadicalElectrons() for atom in result.GetAtoms())):
        raise ValueError('Molecule has invalid graph, coordinates or size')
    if two_dimensional and (result.GetConformer().Is3D()
            or np.any(np.abs(result.GetConformer().GetPositions()[:, 2]) > 1e-6)):
        raise ValueError('Independent ligand input must contain only fresh 2D coordinates')
    for atom in result.GetAtoms():
        atom.SetAtomMapNum(0)
    return result


def identity(mol):
    result = Chem.RemoveHs(Chem.Mol(mol))
    for atom in result.GetAtoms():
        atom.SetAtomMapNum(0)
    Chem.SanitizeMol(result)
    Chem.AssignStereochemistry(result, cleanIt=True, force=True)
    if any(str(item.specified) != 'Specified' for item in Chem.FindPotentialStereo(result)):
        raise ValueError('All supported source stereochemistry must be specified')
    return Chem.MolToSmiles(result, isomericSmiles=True)


def independent_conformer(source, seed=DOCKING['seed']):
    """Discard ALL coordinates before embedding, including a caller's 2D layout."""
    expected = identity(source)
    result = Chem.Mol(source)
    result.RemoveAllConformers()
    result = Chem.AddHs(result)
    parameters = AllChem.ETKDGv3()
    parameters.randomSeed = seed
    parameters.numThreads = 1
    if AllChem.EmbedMolecule(result, parameters) != 0:
        raise ValueError('Independent conformer embedding failed; no fallback pose')
    if identity(result) != expected:
        raise ValueError('Independent embedding changed molecular identity')
    return result


def receptor(path):
    """Only the exact registered isolated receptor; no silent chain/altloc drops."""
    if digest(path) != digest(REGISTRY/'assets/protein_A.pdb'):
        raise ValueError('Independent receptor differs from registered 4G5J chain A')
    keys, positions, numbers = [], [], []
    table = Chem.GetPeriodicTable()
    for line in Path(path).read_text().splitlines():
        if line.startswith(('MODEL', 'HETATM', 'LINK  ', 'SSBOND')):
            raise ValueError('Unexpected model, component or external bond in receptor')
        if not line.startswith('ATOM  '):
            continue
        if line[21:22] != 'A' or line[16:17] not in (' ', 'A'):
            raise ValueError('Unexpected receptor chain or alternate conformation')
        element = line[76:78].strip().title()
        if element in ('H', 'D'):
            continue
        key = (line[21], int(line[22:26]), line[26:27].strip(), line[12:16].strip())
        if key in keys:
            raise ValueError('Duplicate receptor atom')
        if key[1] == 797 and line[17:20] != 'CYS':
            raise ValueError('Target residue is not CYS')
        number = table.GetAtomicNumber(element)
        if number <= 1:
            raise ValueError('Unrecognized receptor heavy atom')
        keys.append(key)
        positions.append([float(line[30:38]), float(line[38:46]), float(line[46:54])])
        numbers.append(number)
    xyz = np.asarray(positions)
    if xyz.shape != (len(keys), 3) or not np.isfinite(xyz).all():
        raise ValueError('Invalid receptor coordinates')
    sg = keys.index(('A', 797, '', 'SG'))
    cb = keys.index(('A', 797, '', 'CB'))
    if numbers[sg] != 16 or numbers[cb] != 6:
        raise ValueError('Target sulfur or beta carbon identity differs')
    return {'keys': keys, 'xyz': xyz, 'numbers': numbers, 'sg': sg, 'cb': cb}


def reaction_atoms(source, alpha, beta, *, control=False):
    if any(type(i) is not int or not 0 <= i < source.GetNumAtoms() for i in (alpha, beta)):
        raise ValueError('Invalid reaction atom mapping')
    matches = source.GetSubstructMatches(Chem.MolFromSmarts('[NX3][CX3](=[OX1])[CX3]=[CX3]'))
    if not any(match[3:] == (alpha, beta) for match in matches):
        raise ValueError('Mapped reaction atoms are not the unreacted acrylamide alkene')
    if not control and source.GetAtomWithIdx(beta).GetDegree() != 1:
        raise ValueError('Generated candidate must retain terminal acrylamide')


def map_pose(source, pose, alpha, beta):
    """Graph equivalence first; reaction mapping must not be chosen by geometry."""
    expected = identity(source)
    actual = Chem.RemoveHs(Chem.Mol(pose))
    if actual.GetNumConformers() != 1 or not actual.GetConformer().Is3D():
        raise ValueError('Independent result lacks a single 3D conformer')
    xyz = actual.GetConformer().GetPositions()
    if not np.isfinite(xyz).all():
        raise ValueError('Independent result has nonfinite coordinates')
    if identity(actual) != expected:
        raise ValueError('Docked graph, charge, isotope or specified stereochemistry changed')
    # Test coordinate-derived stereo as well as the stored SDF tags.
    measured = Chem.Mol(actual)
    Chem.RemoveStereochemistry(measured)
    Chem.AssignStereochemistryFrom3D(measured, replaceExistingTags=True)
    if identity(measured) != expected:
        raise ValueError('Docked coordinates disagree with source stereochemistry')
    matches = actual.GetSubstructMatches(source, uniquify=False, useChirality=True,
                                       maxMatches=MAX_MAPPINGS + 1)
    if not matches or len(matches) > MAX_MAPPINGS:
        raise ValueError('Missing or excessive full-graph atom mappings')
    targets = {(match[alpha], match[beta]) for match in matches}
    if len(targets) != 1:
        raise ValueError('Reaction atom mapping ambiguous; cannot select by distance')
    return actual, min(matches), next(iter(targets))


def geometry(pose, protein, alpha, beta):
    xyz = pose.GetConformer().GetPositions()
    sulfur = protein['xyz'][protein['sg']]
    u, v = sulfur - xyz[beta], xyz[alpha] - xyz[beta]
    denominator = np.linalg.norm(u) * np.linalg.norm(v)
    if denominator < 1e-8:
        raise ValueError('Degenerate reaction geometry')
    distance = float(np.linalg.norm(u))
    angle = float(np.degrees(np.arccos(np.clip(np.dot(u, v) / denominator, -1, 1))))
    table = Chem.GetPeriodicTable()
    radii = np.asarray([table.GetRvdw(a.GetAtomicNum()) for a in pose.GetAtoms()])
    protein_radii = np.asarray([table.GetRvdw(n) for n in protein['numbers']])
    distances = np.linalg.norm(xyz[:, None, :] - protein['xyz'][None, :, :], axis=2)
    ratios = distances / (radii[:, None] + protein_radii[None, :])
    bad = np.argwhere(ratios < POLICY['minimum_nonbonded_vdw_ratio'])
    checks = {
        'distance': POLICY['sg_beta_distance_A'][0] <= distance <= POLICY['sg_beta_distance_A'][1],
        'direction': POLICY['sg_beta_alpha_angle_deg'][0] <= angle <= POLICY['sg_beta_alpha_angle_deg'][1],
        'no_nonbonded_clashes': len(bad) == 0,
    }
    return {'checks': checks, 'passed': all(checks.values()), 'sg_beta_distance_A': distance,
        'sg_beta_alpha_angle_deg': angle, 'minimum_nonbonded_vdw_ratio': float(ratios.min()),
        'clash_count': len(bad), 'excluded_pairs': 0,
        'clashes': [{'ligand_index': int(i), 'receptor_atom': list(protein['keys'][j]),
            'distance_A': float(distances[i, j]), 'vdw_ratio': float(ratios[i, j])} for i, j in bad]}


def assess(source, poses, scores, protein, alpha, beta, *, control=False):
    """Preserve every pose; a later geometric PASS never replaces score rank 1."""
    from posebusters import PoseBusters
    binding = freeze()
    reaction_atoms(source, alpha, beta, control=control)
    identity(source)
    if not 1 <= len(poses) <= DOCKING['n_poses'] or len(scores) != len(poses):
        raise ValueError('Incomplete or unbounded docking pose census')
    if any(type(score) not in (float, int) or not np.isfinite(score) for score in scores):
        raise ValueError('Every returned pose must have a finite docking score')
    selected = min(range(len(scores)), key=lambda index: (scores[index], index))
    rows = []
    for index, (pose, score) in enumerate(zip(poses, scores)):
        row = {'pose_index': index, 'vina_score_kcal_mol': score, 'selected': index == selected}
        try:
            actual, mapping, atoms = map_pose(source, pose, alpha, beta)
            row['source_to_pose_mapping'] = list(mapping)
            row['geometry'] = geometry(actual, protein, *atoms)
            values = PoseBusters(deepcopy(binding['posebusters_config']), max_workers=0).bust(
                Chem.Mol(actual), full_report=True)
            if len(values) != 1:
                raise ValueError('PoseBusters must return exactly one result')
            pb = values.iloc[0].to_dict()
            missing = [key for key in PB_KEYS if key not in pb or not isinstance(pb[key], (bool, np.bool_))]
            if missing:
                raise ValueError('Missing PoseBusters evidence: ' + ', '.join(missing))
            row['posebusters'] = {key: bool(pb[key]) for key in PB_KEYS}
            row['state'] = 'PASS' if row['geometry']['passed'] and all(row['posebusters'].values()) else 'FAIL'
        except (ValueError, TypeError, KeyError, RuntimeError) as error:
            row.update(state='EVIDENCE_INSUFFICIENT', reason=str(error))
        rows.append(row)
    return {'schema': VERSION, 'state': rows[selected]['state'], 'selected_pose_index': selected,
        'pose_count': len(rows), 'poses': rows, 'policy': binding,
        'scope': 'exploratory_independent_prereaction_structural_screen',
        'md_admission_granted': False, 'activity_verified': False}
