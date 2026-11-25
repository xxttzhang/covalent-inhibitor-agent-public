"""Independent heavy-atom identity, graph and stereochemistry checks.

Uses standard peptide templates, not the repair engine's own atom expectations.
This is one quality gate; it never grants full receptor or simulation admission.
"""
import hashlib
import json
from pathlib import Path

from .pdb_coordinates import read_pdb_coordinates
from .receptor_preparation import AMINO_ACIDS
from .peptide_templates import peptide_reference


def observed_coordinate_displacements(originals, prepared):
    """Measure observed heavy-atom motion by identity, never granting admission.

    Inputs must already be hash-verified by the caller. Missing or relabeled
    observed atoms invalidate the measurement rather than shrinking its denominator.
    """
    import math
    identity = ('chain', 'author_residue', 'resname', 'name', 'element')
    def indexed(atoms):
        result = {}
        for atom in atoms:
            serial = atom.get('original_serial')
            if serial is None or atom['element'] in {'H', 'D'}:
                continue
            if serial in result:
                raise ValueError('Duplicate observed atom identity')
            xyz = atom['xyz_angstrom']
            if len(xyz) != 3 or not all(math.isfinite(v) for v in xyz):
                raise ValueError('Nonfinite observed atom coordinate')
            result[serial] = atom
        return result
    before, after = indexed(originals), indexed(prepared)
    if not before or before.keys() != after.keys():
        raise ValueError('Observed atom denominator changed')
    rows = []
    for serial, first in before.items():
        last = after[serial]
        if any(first[k] != last[k] for k in identity):
            raise ValueError('Observed atom identity changed')
        distance = math.dist(first['xyz_angstrom'], last['xyz_angstrom'])
        rows.append({'original_serial': serial,
            'chain': last['chain'], 'author_residue': last['author_residue'],
            'resname': last['resname'], 'element': last['element'],
            'entity_position': last['entity_position'], 'atom_name': last['name'],
            'source_xyz_angstrom': first['xyz_angstrom'],
            'prepared_xyz_angstrom': last['xyz_angstrom'], 'displacement_angstrom': distance})
    return {'scope': 'observed_heavy_atom_displacement_measurement_not_admission',
        'atom_count': len(rows),
        'rms_displacement_angstrom': math.sqrt(sum(r['displacement_angstrom'] ** 2 for r in rows) / len(rows)),
        'maximum_displacement_angstrom': max(r['displacement_angstrom'] for r in rows),
        'atoms': rows}


def audit_receptor_heavy_atoms(directory, receipt_sha256, source):
    return _audit_receptor_heavy_atoms(directory, receipt_sha256, source)


def _audit_receptor_heavy_atoms(directory, receipt_sha256, source, *, observed_motion=False):
    from rdkit import Chem, rdBase
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError('Repair directory cannot be a symbolic link')
    def read(name, digest):
        path = directory / name
        if path.is_symlink() or not path.is_file():
            raise ValueError('Repair evidence must be regular files')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError('Repair evidence checksum mismatch: ' + name)
        return data
    receipt = json.loads(read('receipt.json', receipt_sha256))
    if receipt['schema'] != 'receptor-heavy-atom-preparation-v1' or receipt['status'] != 'heavy_atoms_built_pending_independent_qc':
        raise ValueError('A completed heavy-atom proposal is required')
    data = {name: read(name, receipt['artifacts'][name]) for name in
            ('inventory.json', 'atom-mapping.json', 'topology.json', 'heavy-atoms.pdb')}
    inventory = json.loads(data['inventory.json'])
    mapped = json.loads(data['atom-mapping.json'])['atoms']
    topology = json.loads(data['topology.json'])
    raw, _, originals, _ = read_pdb_coordinates(Path(source), inventory['entry_id'], inventory['model'])
    if hashlib.sha256(raw).hexdigest() != receipt['source_sha256']:
        raise ValueError('Original receptor source checksum mismatch')
    _, _, coordinates, _ = read_pdb_coordinates(directory / 'heavy-atoms.pdb', inventory['entry_id'])
    if len(mapped) != len(coordinates) or len(mapped) != len(topology['atoms']):
        raise ValueError('Repair artifact atom counts disagree')
    retained = {a['serial']: a for a in originals}
    keys, points, violations = [], [], []
    for index, (atom, record, topo) in enumerate(zip(mapped, coordinates, topology['atoms'])):
        key = (atom['entity_position'], atom['name'])
        keys.append(key)
        points.append(record['xyz'])
        if atom['index'] != index or any(topo[k] != atom[k] for k in topo):
            raise ValueError('Repair topology and atom map disagree')
        if (record['chain'], record['residue'], record['resname'], record['name'], record['element']) != (
            atom['chain'], atom['author_residue'], atom['resname'], atom['name'], atom['element']):
            raise ValueError('Serialized PDB changed repaired atom identity')
        if any(abs(record['xyz'][j] - atom['xyz_angstrom'][j]) > 0.000501 for j in range(3)):
            raise ValueError('Serialized PDB coordinates disagree with the atom map')
        if atom['original_serial'] is not None:
            original = retained[atom['original_serial']]
            compared = ('chain', 'residue', 'resname', 'name', 'element', 'occupancy')
            if not observed_motion:
                compared += ('xyz',)
            if any(original[k] != record[k] for k in compared):
                violations.append({'atom': key, 'reason': 'observed_atom_changed'})
            if observed_motion:
                import math
                reference = atom.get('source_xyz_angstrom', [])
                if (atom.get('coordinate_origin') != 'relaxed_observed_atom'
                        or len(reference) != 3
                        or not all(math.isfinite(v) for v in reference)
                        or any(abs(reference[j] - original['xyz'][j]) > 1e-9 for j in range(3))):
                    violations.append({'atom': key, 'reason': 'observed_source_coordinate_changed'})
        elif record['occupancy'] != 0:
            violations.append({'atom': key, 'reason': 'modeled_atom_appears_observed'})
    if len(set(keys)) != len(keys):
        raise ValueError('Duplicate repaired atom identity')
    expected_serials = {a['serial'] for a in inventory['retained_atoms']}
    if {a['original_serial'] for a in mapped if a['original_serial'] is not None} != expected_serials:
        raise ValueError('Observed atom denominator changed')
    residues = {a['entity_position']: a['resname'] for a in mapped}
    if any(a['resname'] != residues[a['entity_position']] for a in mapped):
        raise ValueError('Conflicting residue identities within repaired atoms')
    codes = {v: k for k, v in AMINO_ACIDS.items()}
    sequence = ''.join(codes[residues[i]] for i in range(1, len(residues) + 1))
    if hashlib.sha256(sequence.encode()).hexdigest() != inventory['construct_audit']['construct_sequence_sha256']:
        raise ValueError('Repaired sequence differs from the deposited construct')
    reference, corrected = peptide_reference(sequence)
    expected = [(a.GetPDBResidueInfo().GetResidueNumber(), a.GetPDBResidueInfo().GetName().strip())
                for a in reference.GetAtoms()]
    missing, extra = sorted(set(expected) - set(keys)), sorted(set(keys) - set(expected))
    elements = {key: atom.GetSymbol() for key, atom in zip(expected, reference.GetAtoms())}
    element_errors = [keys[i] for i, a in enumerate(mapped)
                      if keys[i] in elements and a['element'] != elements[keys[i]]]
    graph_missing, graph_extra, inverted = [], [], []
    stereo_checked = None
    if not missing and not extra:
        actual_bonds = {tuple(sorted((keys[a], keys[b]))) for a, b in topology['bonds']}
        if len(actual_bonds) != len(topology['bonds']):
            raise ValueError('Duplicate repaired bonds')
        expected_bonds = {tuple(sorted((expected[b.GetBeginAtomIdx()], expected[b.GetEndAtomIdx()])))
                          for b in reference.GetBonds()}
        graph_missing, graph_extra = sorted(expected_bonds - actual_bonds), sorted(actual_bonds - expected_bonds)
        by_key = dict(zip(keys, points))
        conformer = Chem.Conformer(reference.GetNumAtoms())
        conformer.Set3D(True)
        for index, key in enumerate(expected):
            conformer.SetAtomPosition(index, by_key[key])
        reference.AddConformer(conformer)
        Chem.AssignStereochemistry(reference, cleanIt=True, force=True)
        labels = {a.GetIdx(): a.GetProp('_CIPCode') for a in reference.GetAtoms() if a.HasProp('_CIPCode')}
        stereo_checked = len(labels)
        actual = Chem.Mol(reference)
        Chem.AssignStereochemistryFrom3D(actual, replaceExistingTags=True)
        Chem.AssignStereochemistry(actual, cleanIt=True, force=True)
        for index, label in labels.items():
            atom = actual.GetAtomWithIdx(index)
            observed = atom.GetProp('_CIPCode') if atom.HasProp('_CIPCode') else None
            if observed != label:
                inverted.append({'atom': expected[index], 'expected': label, 'actual': observed})
    passed = not any((violations, missing, extra, element_errors, graph_missing, graph_extra, inverted))
    return {'schema': 'receptor-heavy-atom-quality-v1', 'receipt_sha256': receipt_sha256,
            'status': 'identity_and_stereochemistry_passed_geometry_pending' if passed else 'heavy_atom_quality_failed',
            'rdkit_version': rdBase.rdkitVersion,
            'audit_source_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'template_source_sha256': hashlib.sha256(Path(__file__).with_name('peptide_templates.py').read_bytes()).hexdigest(),
            'template_ile_label_corrections': corrected, 'provenance_violations': violations,
            'missing_atoms': missing, 'extra_atoms': extra, 'missing_bonds': graph_missing,
            'element_mismatches': element_errors, 'stereocenters_checked': stereo_checked,
            'extra_bonds': graph_extra, 'stereochemistry_violations': inverted,
            'receptor_prepared': False, 'campaign_ready': False,
            'remaining_checks': ['geometry and clashes', 'environment retention', 'protonation',
                                 'physical relaxation', 'independent receptor admission']}


def audit_relaxed_receptor(directory, receipt_sha256, parent, source):
    return _audit_relaxed_receptor(directory, receipt_sha256, parent, source)


def audit_restrained_receptor(directory, receipt_sha256, parent, source):
    """V2 identity and displacement audit, not geometry or receptor admission."""
    return _audit_relaxed_receptor(directory, receipt_sha256, parent, source, observed_motion=True)


def _audit_relaxed_receptor(directory, receipt_sha256, parent, source, *, observed_motion=False):
    """Reuse the canonical heavy-atom audit on a relaxation's derived heavy view."""
    import tempfile
    directory, parent = Path(directory), Path(parent)
    if any(path.is_symlink() for path in (directory, parent)):
        raise ValueError('Relaxation lineage cannot contain symbolic directories')
    receipt_path = directory / 'receipt.json'
    if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != receipt_sha256:
        raise ValueError('Relaxation receipt checksum mismatch')
    receipt = json.loads(receipt_path.read_text())
    if observed_motion:
        from .receptor_relaxation import RELAXATION_POLICY_V2
        selected_policy = receipt.get('policy')
        local_mask = isinstance(selected_policy, dict) and 'local_observation_mask' in selected_policy
        if local_mask:
            from .receptor_local_policy import validate_policy, audit_motion
            validate_policy(selected_policy)
        elif selected_policy != RELAXATION_POLICY_V2:
            raise ValueError('A registered v2 receptor policy or validated local extension is required')
        policy_sha = hashlib.sha256(json.dumps(selected_policy, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()
        stages = receipt.get('stages', [])
        if (receipt.get('schema') != 'receptor-relaxation-receipt-v2'
                or receipt.get('status') != 'position_restrained_relaxation_completed_quality_pending'
                or receipt.get('policy_sha256') != policy_sha
                or receipt.get('artificial_forces_in_final_stage') is not True
                or [r.get('name') for r in stages] != [r['name'] for r in RELAXATION_POLICY_V2['stages']]
                or stages[-1].get('artificial_position_restraint') is not True
                or stages[-1].get('artificial_chirality_restraint') is not False
                or stages[-1].get('soft_nonbonded') is not False
                or stages[-1].get('observed_atoms_fixed') is not False):
            raise ValueError('A completed registered v2 position-restrained proposal is required')
        optimizer = stages[-1].get('bounded_optimizer')
        if 'bounded_optimizer' in selected_policy:
            from .receptor_bounded_minimizer import validate_optimizer_receipt
            from .receptor_local_policy import BLOCK_OPTIMIZER
            validator = validate_optimizer_receipt
            if selected_policy['bounded_optimizer'] == BLOCK_OPTIMIZER:
                from .receptor_block_minimizer import validate_block_receipt
                validator = validate_block_receipt
            validator(optimizer, selected_policy['bounded_optimizer'])
            if (stages[-1].get('tolerance_reached') is not True
                    or stages[-1].get('convergence_metric') != 'projected_gradient_max_kj_mol_nm'
                    or stages[-1].get('final_energy_kj_mol') != optimizer['energy_kj_mol']):
                raise ValueError('Bounded optimizer stage differs from its convergence receipt')
        elif optimizer is not None:
            raise ValueError('Bounded optimizer receipt requires an explicit bounded policy')
    else:
        if (receipt.get('schema') != 'receptor-relaxation-receipt-v1'
                or receipt.get('status') != 'unrestrained_relaxation_completed_quality_pending'
                or receipt.get('artificial_forces_in_final_stage') is not False
                or receipt['stages'][-1].get('artificial_chirality_restraint') is not False
                or receipt['stages'][-1].get('soft_nonbonded') is not False):
            raise ValueError('A completed unrestrained relaxation is required')
    parent_receipt_path = parent / 'receipt.json'
    if hashlib.sha256(parent_receipt_path.read_bytes()).hexdigest() != receipt['parent_receipt_sha256']:
        raise ValueError('Relaxation parent receipt changed')
    parent_receipt = json.loads(parent_receipt_path.read_text())
    files = {'inventory.json': parent / 'inventory.json',
             'atom-mapping.json': directory / 'heavy-atom-mapping.json',
             'topology.json': directory / 'heavy-topology.json'}
    for name, path in files.items():
        expected = (parent_receipt['artifacts'][name] if name == 'inventory.json'
                    else receipt['artifacts']['heavy-' + name])
        if path.is_symlink() or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError('Relaxation quality evidence changed: ' + name)
    motion = None
    if observed_motion:
        original_map = parent / 'atom-mapping.json'
        if (original_map.is_symlink() or hashlib.sha256(original_map.read_bytes()).hexdigest()
                != parent_receipt['artifacts']['atom-mapping.json']):
            raise ValueError('Original observed atom mapping changed')
        motion = observed_coordinate_displacements(json.loads(original_map.read_text())['atoms'],
            json.loads(files['atom-mapping.json'].read_text())['atoms'])
    relaxed = directory / 'relaxed.pdb'
    if relaxed.is_symlink() or hashlib.sha256(relaxed.read_bytes()).hexdigest() != receipt['artifacts']['relaxed.pdb']:
        raise ValueError('Relaxed coordinate evidence changed')
    lines = relaxed.read_text().splitlines()
    heavy_pdb = '\n'.join(line for line in lines
        if not line.startswith(('ATOM  ', 'HETATM')) or line[76:78].strip().upper() not in {'H', 'D'}) + '\n'
    with tempfile.TemporaryDirectory(prefix='receptor-quality-') as name:
        view = Path(name)
        (view / 'inventory.json').write_bytes(files['inventory.json'].read_bytes())
        (view / 'atom-mapping.json').write_bytes(files['atom-mapping.json'].read_bytes())
        (view / 'topology.json').write_bytes(files['topology.json'].read_bytes())
        (view / 'heavy-atoms.pdb').write_text(heavy_pdb)
        artifacts = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                     for path in view.iterdir()}
        derived = {'schema': 'receptor-heavy-atom-preparation-v1',
                   'status': 'heavy_atoms_built_pending_independent_qc',
                   'source_sha256': receipt['source_sha256'], 'artifacts': artifacts}
        (view / 'receipt.json').write_text(json.dumps(derived, indent=2) + '\n')
        digest = hashlib.sha256((view / 'receipt.json').read_bytes()).hexdigest()
        result = _audit_receptor_heavy_atoms(view, digest, source, observed_motion=observed_motion)
    result.update(schema='receptor-relaxation-quality-v2' if observed_motion else 'receptor-relaxation-quality-v1',
                  relaxation_receipt_sha256=receipt_sha256,
                  derived_heavy_view_receipt_sha256=digest,
                  final_stage_artificial_forces=observed_motion,
                  protonation=receipt['protonation'])
    if observed_motion:
        result.update(observed_coordinate_displacements=motion,
            observed_motion_acceptance='not_evaluated',
            position_restraint_policy_sha256=receipt['policy_sha256'])
        if local_mask:
            local = audit_motion(selected_policy, json.loads(original_map.read_text())['atoms'],
                json.loads(files['atom-mapping.json'].read_text())['atoms'], receipt['source_sha256'])
            result['local_motion_audit'] = local
            if local['status'] != 'local_motion_limits_passed':
                result['status'] = 'local_motion_limits_failed'
    return result


def observed_site_motion(motion, *, chain, author_residue, radius_angstrom=6.0):
    """Describe the union of source/final SG neighborhoods; no acceptance cutoff."""
    import math
    if not math.isfinite(radius_angstrom) or radius_angstrom <= 0:
        raise ValueError('Site neighborhood radius must be finite and positive')
    atoms = motion['atoms']
    targets = [row for row in atoms if row['chain'] == chain
        and str(row['author_residue']) == str(author_residue)
        and row['resname'] == 'CYS' and row['atom_name'] == 'SG']
    if len(targets) != 1:
        raise ValueError('Observed site motion requires one identity-bound target cysteine SG')
    target = targets[0]
    rows = []
    for atom in atoms:
        if atom['original_serial'] == target['original_serial']:
            continue
        before = math.dist(atom['source_xyz_angstrom'], target['source_xyz_angstrom'])
        after = math.dist(atom['prepared_xyz_angstrom'], target['prepared_xyz_angstrom'])
        if min(before, after) <= radius_angstrom:
            rows.append({**atom, 'source_sg_distance_angstrom': before,
                'prepared_sg_distance_angstrom': after,
                'sg_distance_change_angstrom': after - before,
                'in_source_neighborhood': before <= radius_angstrom,
                'in_prepared_neighborhood': after <= radius_angstrom})
    return {'scope': 'observed_atom_union_neighborhood_measurement',
        'radius_angstrom': radius_angstrom, 'radius_is_acceptance_threshold': False,
        'target_sg': target, 'atoms': rows,
        'modeled_atoms_included': False, 'binding_site_preservation_validated': False}
