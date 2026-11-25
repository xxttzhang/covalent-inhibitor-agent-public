"""Independent chemical-graph geometry audit for a relaxed receptor proposal."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from .peptide_templates import peptide_reference
from .receptor_preparation import AMINO_ACIDS


GEOMETRY_POLICY_V1 = {'schema': 'receptor-geometry-policy-v1',
    'bond_relative_tolerance': 0.25, 'angle_relative_tolerance': 0.25,
    'internal_clash_lower_tolerance': 0.30,
    'aromatic_plane_tolerance_angstrom': 0.25,
    'protein_vdw_ratio_minimum': 0.75}


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def audit_receptor_geometry(directory, receipt_sha256, quality, quality_sha256, source):
    return _audit_receptor_geometry(directory, receipt_sha256, quality, quality_sha256, source)


def audit_restrained_geometry(directory, receipt_sha256, quality, quality_sha256, source, parent):
    """Replay v2 identity before geometry; retain separate motion/context review."""
    from .receptor_quality import audit_restrained_receptor
    if _sha(quality) != quality_sha256:
        raise ValueError('Independent identity quality checksum mismatch')
    replayed = audit_restrained_receptor(directory, receipt_sha256, parent, source)
    if json.loads(json.dumps(replayed)) != json.loads(Path(quality).read_text()):
        raise ValueError('V2 identity audit differs from independent replay')
    return _audit_receptor_geometry(directory, receipt_sha256, quality, quality_sha256,
                                    source, observed_motion=True)


def _audit_receptor_geometry(directory, receipt_sha256, quality, quality_sha256, source,
                             *, observed_motion=False):
    import numpy as np
    from rdkit import Chem, rdBase
    from rdkit.Chem import rdDistGeom
    from scipy.spatial import cKDTree

    directory, quality, source = map(Path, (directory, quality, source))
    if any(path.is_symlink() for path in (directory, quality, source)):
        raise ValueError('Geometry audit inputs cannot be symbolic links')
    receipt_path = directory / 'receipt.json'
    if _sha(receipt_path) != receipt_sha256:
        raise ValueError('Relaxation receipt checksum mismatch')
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get('schema') != ('receptor-relaxation-receipt-v2' if observed_motion else 'receptor-relaxation-receipt-v1')
            or receipt.get('status') != ('position_restrained_relaxation_completed_quality_pending' if observed_motion
                                          else 'unrestrained_relaxation_completed_quality_pending')
            or receipt.get('artificial_forces_in_final_stage') is not observed_motion):
        raise ValueError('A completed unrestrained relaxation is required')
    if _sha(quality) != quality_sha256:
        raise ValueError('Independent identity quality checksum mismatch')
    prior = json.loads(quality.read_text())
    if (prior.get('schema') != ('receptor-relaxation-quality-v2' if observed_motion else 'receptor-relaxation-quality-v1')
            or prior.get('status') != 'identity_and_stereochemistry_passed_geometry_pending'
            or prior.get('relaxation_receipt_sha256') != receipt_sha256):
        raise ValueError('Identity, graph and stereochemistry must pass first')
    if _sha(source) != receipt['source_sha256']:
        raise ValueError('Original receptor source changed')
    evidence = {}
    for name in ('heavy-atom-mapping.json', 'heavy-topology.json',
                 'atom-mapping.json', 'topology.json'):
        path = directory / name
        if path.is_symlink() or not path.is_file() or _sha(path) != receipt['artifacts'][name]:
            raise ValueError('Relaxation evidence changed: ' + name)
        evidence[name] = json.loads(path.read_text())
    atoms = evidence['heavy-atom-mapping.json']['atoms']
    topology = evidence['heavy-topology.json']
    all_atoms = evidence['atom-mapping.json']['atoms']
    all_topology = evidence['topology.json']
    residues = {a['entity_position']: a['resname'] for a in atoms}
    if set(residues) != set(range(1, max(residues) + 1)):
        raise ValueError('Heavy-atom construct positions are incomplete')
    codes = {value: key for key, value in AMINO_ACIDS.items()}
    sequence = ''.join(codes[residues[position]] for position in range(1, len(residues) + 1))
    reference, corrections = peptide_reference(sequence)
    expected = [(a.GetPDBResidueInfo().GetResidueNumber(),
                 a.GetPDBResidueInfo().GetName().strip()) for a in reference.GetAtoms()]
    keys = [(a['entity_position'], a['name']) for a in atoms]
    if (len(set(keys)) != len(keys) or set(keys) != set(expected)
            or [a['index'] for a in atoms] != list(range(len(atoms)))):
        raise ValueError('Heavy-atom identities differ from the standard peptide reference')
    actual_bonds = {tuple(sorted((keys[first], keys[second]))) for first, second in topology['bonds']}
    expected_bonds = {tuple(sorted((expected[b.GetBeginAtomIdx()], expected[b.GetEndAtomIdx()])))
                      for b in reference.GetBonds()}
    if actual_bonds != expected_bonds:
        raise ValueError('Heavy-atom graph differs from the standard peptide reference')
    by_key = dict(zip(keys, atoms))
    xyz = np.asarray([by_key[key]['xyz_angstrom'] for key in expected], dtype=float)
    if xyz.shape != (len(expected), 3) or not np.isfinite(xyz).all():
        raise ValueError('Invalid relaxed heavy-atom coordinates')
    modeled = np.asarray([(by_key[key]['original_serial'] is None if observed_motion
                           else by_key[key]['coordinate_origin'] != 'retained_observed_coordinate')
                          for key in expected])
    bounds = rdDistGeom.GetMoleculeBoundsMatrix(reference, set15bounds=True, scaleVDW=True,
        doTriangleSmoothing=True, useMacrocycle14config=False)
    distances = Chem.GetDistanceMatrix(reference)
    checks = {}
    for name, mask, tolerance in (
        ('bonds', lambda value: value == 1, GEOMETRY_POLICY_V1['bond_relative_tolerance']),
        ('angles', lambda value: value == 2, GEOMETRY_POLICY_V1['angle_relative_tolerance']),
        ('internal', lambda value: value > 2, GEOMETRY_POLICY_V1['internal_clash_lower_tolerance'])):
        count, modeled_count, examples, tested = 0, 0, [], 0
        for first in range(len(expected) - 1):
            seconds = np.arange(first + 1, len(expected))
            seconds = seconds[mask(distances[first, seconds])]
            values = np.linalg.norm(xyz[seconds] - xyz[first], axis=1)
            lower, upper = bounds[seconds, first], bounds[first, seconds]
            invalid = (~np.isfinite(values) | ~np.isfinite(lower) | ~np.isfinite(upper)
                       | (lower <= 0) | (values < lower * (1 - tolerance)))
            if name != 'internal':
                invalid |= values > upper * (1 + tolerance)
            tested += len(seconds)
            for offset in np.flatnonzero(invalid):
                second = int(seconds[offset])
                involves_modeled = bool(modeled[first] or modeled[second])
                count += 1
                modeled_count += involves_modeled
                if len(examples) < 100:
                    examples.append({'atoms': [expected[first], expected[second]],
                        'distance_angstrom': float(values[offset]),
                        'bounds_angstrom': [float(lower[offset]), float(upper[offset])],
                        'involves_modeled_atom': involves_modeled})
        checks[name] = {'status': 'pass' if count == 0 else 'fail', 'tested': tested,
            'violation_count': count, 'modeled_pair_violations': modeled_count,
            ('observed_origin_pair_violations' if observed_motion else 'retained_observed_pair_violations'): count - modeled_count,
            'first_100_violations': examples}
    planarity = []
    for ring in Chem.GetSymmSSSR(reference):
        indices = list(ring)
        if len(indices) not in (5, 6) or not all(reference.GetAtomWithIdx(i).GetIsAromatic() for i in indices):
            continue
        points = xyz[indices]
        centered = points - points.mean(axis=0)
        deviation = float(np.abs(centered @ np.linalg.svd(centered)[2][-1]).max())
        if deviation > GEOMETRY_POLICY_V1['aromatic_plane_tolerance_angstrom']:
            planarity.append({'atoms': [expected[i] for i in indices],
                              'maximum_deviation_angstrom': deviation})
    adjacency = {a.GetIdx(): {n.GetIdx() for n in a.GetNeighbors()} for a in reference.GetAtoms()}
    table = Chem.GetPeriodicTable()
    radii = np.asarray([table.GetRvdw(a.GetAtomicNum()) for a in reference.GetAtoms()])
    clashes = []
    maximum = float(2 * radii.max() * GEOMETRY_POLICY_V1['protein_vdw_ratio_minimum'])
    for first, second in sorted(cKDTree(xyz).query_pairs(maximum)):
        if second in adjacency[first] or adjacency[first].intersection(adjacency[second]):
            continue
        value = float(np.linalg.norm(xyz[first] - xyz[second]))
        ratio = value / float(radii[first] + radii[second])
        if ratio < GEOMETRY_POLICY_V1['protein_vdw_ratio_minimum']:
            clashes.append({'atoms': [expected[first], expected[second]],
                'distance_angstrom': value, 'vdw_ratio': ratio,
                'involves_modeled_atom': bool(modeled[first] or modeled[second])})
    target_position = receipt['protonation']['target_entity_position']
    target = next(a for a in all_atoms if a['entity_position'] == target_position and a['name'] == 'SG')
    neighbors = []
    for first, second in all_topology['bonds']:
        if target['index'] in (first, second):
            neighbors.append(all_atoms[second if first == target['index'] else first]['name'])
    chemical_state = {'entity_position': target_position, 'author_residue': target['author_residue'],
        'resname': target['resname'], 'atom': target['name'], 'neighbors': sorted(neighbors),
        'selected_variant': receipt['protonation']['target_variant'],
        'status': 'pass' if (target['resname'] == 'CYS' and target['original_serial'] is not None
                            and set(neighbors) == {'CB', 'HG'}) else 'fail'}
    passed = (all(value['status'] == 'pass' for value in checks.values())
              and not planarity and not clashes and chemical_state['status'] == 'pass')
    result = {'schema': 'receptor-geometry-audit-v2' if observed_motion else 'receptor-geometry-audit-v1',
        'status': 'geometry_passed_environment_review_pending' if passed else 'geometry_quality_failed',
        'relaxation_receipt_sha256': receipt_sha256, 'identity_quality_sha256': quality_sha256,
        'policy': GEOMETRY_POLICY_V1,
        'policy_sha256': hashlib.sha256(json.dumps(GEOMETRY_POLICY_V1,
            sort_keys=True, separators=(',', ':')).encode()).hexdigest(),
        'audit_source_sha256': _sha(__file__), 'rdkit_version': rdBase.rdkitVersion,
        'numpy_version': np.__version__, 'ile_template_label_corrections': corrections,
        'distance_checks': checks, 'aromatic_planarity_violations': planarity,
        'vdw_clashes': clashes, 'reactive_site_prereaction_state': chemical_state,
        'receptor_prepared': False, 'campaign_ready': False,
        'remaining_checks': ['excluded environment and assembly decision',
                             'binding-site suitability and reaction chemistry',
                             'formal receptor artifact admission']}

    if observed_motion:
        result.update(observed_motion_acceptance='not_evaluated',
            coordinate_scope='position_restrained_computational_proposal',
            observed_displacements=prior['observed_coordinate_displacements'],
            atom_origin_semantics='modeled means no original atom serial; observed-origin atoms may have moved')
        result['remaining_checks'].append('independent observed-motion and binding-site preservation review')
    return result
