"""Regress the actual frozen CPU proposal and its independently found failures."""
import hashlib
import json
import os
from pathlib import Path
import shutil

import pytest

from covscreen.receptor_quality import audit_receptor_heavy_atoms


ROOT = Path(os.environ.get('COVSCREEN_HISTORICAL_PROJECT_ROOT', Path(__file__).resolve().parents[2]))
PROPOSAL = ROOT / 'reports/library-first-site-acquisition-20261014/heavy-repair'
SOURCE = Path(os.environ.get('COVSCREEN_HISTORICAL_RECEPTOR_PDB', ROOT / 'science/projects/egfr-c797/ligand_first/source_structures/1M17.pdb'))
RECEIPT = '3b9eded136db258834b3d8086b44b11de2ee8fde3e808f686347af6fad96ada3'


def test_real_repair_is_not_admitted_despite_complete_atoms_and_graph():
    result = audit_receptor_heavy_atoms(PROPOSAL, RECEIPT, SOURCE)
    assert result['status'] == 'heavy_atom_quality_failed'
    assert result['missing_atoms'] == result['extra_atoms'] == []
    assert result['missing_bonds'] == result['extra_bonds'] == []
    assert result['provenance_violations'] == []
    assert len(result['stereochemistry_violations']) == 12
    assert not result['campaign_ready'] and not result['receptor_prepared']


def test_corrupt_coordinate_file_is_rejected_before_quality_claim(tmp_path):
    out = tmp_path / 'proposal'
    shutil.copytree(PROPOSAL, out)
    (out / 'heavy-atoms.pdb').write_text('changed')
    with pytest.raises(ValueError, match='checksum'):
        audit_receptor_heavy_atoms(out, RECEIPT, SOURCE)


def test_consistently_rehashed_missing_bond_fails_independent_reference(tmp_path):
    out = tmp_path / 'proposal'
    shutil.copytree(PROPOSAL, out)
    path = out / 'topology.json'
    topology = json.loads(path.read_text())
    topology['bonds'].pop()
    path.write_text(json.dumps(topology))
    receipt = json.loads((out / 'receipt.json').read_text())
    receipt['artifacts']['topology.json'] = hashlib.sha256(path.read_bytes()).hexdigest()
    (out / 'receipt.json').write_text(json.dumps(receipt))
    result = audit_receptor_heavy_atoms(out, hashlib.sha256((out / 'receipt.json').read_bytes()).hexdigest(), SOURCE)
    assert len(result['missing_bonds']) == 1
    assert result['status'] == 'heavy_atom_quality_failed'


def test_unrestrained_regression_passes_identity_graph_and_all_stereocenters():
    from covscreen.receptor_quality import audit_relaxed_receptor
    folder = ROOT / 'reports/library-first-site-acquisition-20261014/receptor-relaxation-regression-v4'
    parent = ROOT / 'reports/library-first-site-acquisition-20261014/heavy-repair-regression'
    digest = hashlib.sha256((folder / 'receipt.json').read_bytes()).hexdigest()
    result = audit_relaxed_receptor(folder, digest, parent, SOURCE)
    assert result['status'] == 'identity_and_stereochemistry_passed_geometry_pending'
    assert result['stereocenters_checked'] == 350
    assert result['stereochemistry_violations'] == []
    assert result['final_stage_artificial_forces'] is False


def test_observed_displacements_bind_identity_and_keep_denominator():
    from copy import deepcopy
    from covscreen.receptor_quality import observed_coordinate_displacements
    atom = dict(original_serial=4, chain='A', author_residue=797, resname='CYS',
                name='SG', element='S', entity_position=102, xyz_angstrom=[0., 0., 0.])
    moved = deepcopy(atom)
    moved['xyz_angstrom'] = [0., 0.3, 0.4]
    result = observed_coordinate_displacements([atom], [moved])
    assert result['atom_count'] == 1
    assert result['maximum_displacement_angstrom'] == pytest.approx(0.5)
    assert result['rms_displacement_angstrom'] == pytest.approx(0.5)
    assert result['atoms'][0]['source_xyz_angstrom'] == [0., 0., 0.]
    for invalid in ([], [moved, moved], [dict(moved, name='CB')],
                    [dict(moved, xyz_angstrom=[float('nan'), 0., 0.])]):
        with pytest.raises(ValueError):
            observed_coordinate_displacements([atom], invalid)


def test_v2_restrained_proposal_cannot_pass_v1_admission(tmp_path):
    from covscreen.receptor_quality import audit_relaxed_receptor
    receipt = tmp_path / 'receipt.json'
    receipt.write_text(json.dumps({'schema': 'receptor-relaxation-receipt-v2',
        'status': 'position_restrained_relaxation_completed_quality_pending',
        'artificial_forces_in_final_stage': True}))
    with pytest.raises(ValueError, match='unrestrained relaxation'):
        audit_relaxed_receptor(tmp_path, hashlib.sha256(receipt.read_bytes()).hexdigest(),
                              tmp_path, tmp_path / 'unused-source.pdb')


@pytest.mark.parametrize('mutation', ['policy', 'stiffness', 'final_force', 'stage_order'])
def test_v2_audit_rejects_changed_policy_before_reading_coordinates(tmp_path, mutation):
    from copy import deepcopy
    from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
    from covscreen.receptor_quality import audit_restrained_receptor
    policy = deepcopy(RELAXATION_POLICY_V2)
    row = {'schema': 'receptor-relaxation-receipt-v2',
        'status': 'position_restrained_relaxation_completed_quality_pending',
        'artificial_forces_in_final_stage': True, 'policy': policy,
        'policy_sha256': hashlib.sha256(json.dumps(policy, sort_keys=True,
                                separators=(',', ':')).encode()).hexdigest(),
        'stages': [dict(x, artificial_position_restraint=i == 2,
            artificial_chirality_restraint=i != 2, observed_atoms_fixed=i != 2)
                   for i, x in enumerate(policy['stages'])]}
    if mutation == 'policy':
        row['policy_sha256'] = '0' * 64
    elif mutation == 'stiffness':
        row['policy']['observed_position_k_kj_mol_nm2'] = 0
    elif mutation == 'final_force':
        row['stages'][-1]['artificial_position_restraint'] = False
    else:
        row['stages'].reverse()
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match='registered v2'):
        audit_restrained_receptor(tmp_path, hashlib.sha256(path.read_bytes()).hexdigest(),
                                  tmp_path / 'missing-parent', tmp_path / 'missing-source')


def test_site_motion_retains_atoms_entering_and_leaving_neighborhood():
    from covscreen.receptor_quality import observed_site_motion
    target = dict(original_serial=1, chain='A', author_residue='797', resname='CYS',
        atom_name='SG', source_xyz_angstrom=[0, 0, 0], prepared_xyz_angstrom=[1, 0, 0])
    leaving = {**target, 'original_serial': 2, 'atom_name': 'CA', 'author_residue': '798',
        'source_xyz_angstrom': [5, 0, 0], 'prepared_xyz_angstrom': [8, 0, 0]}
    entering = {**leaving, 'original_serial': 3,
        'source_xyz_angstrom': [8, 0, 0], 'prepared_xyz_angstrom': [5, 0, 0]}
    result = observed_site_motion({'atoms': [target, leaving, entering]}, chain='A', author_residue=797)
    assert len(result['atoms']) == 2
    assert result['atoms'][0]['in_prepared_neighborhood'] is False
    assert result['atoms'][1]['in_source_neighborhood'] is False
    assert result['atoms'][0]['sg_distance_change_angstrom'] == 2
    assert result['binding_site_preservation_validated'] is False


@pytest.mark.parametrize('bounded', [False, True, 'block'])
def test_local_audit_rejects_rehashed_outside_mask_motion_on_historical_coordinates(tmp_path, bounded):
    """Synthetic v2-local receipt around frozen v1 output, never a new experiment."""
    from copy import deepcopy
    from covscreen.receptor_local_policy import make_policy, digest
    from covscreen.receptor_quality import audit_restrained_receptor
    parent = ROOT / 'reports/library-first-site-acquisition-20261014/heavy-repair-regression'
    prior = ROOT / 'reports/library-first-site-acquisition-20261014/receptor-relaxation-regression-v4'
    directory = tmp_path / 'synthetic-local-audit'
    shutil.copytree(prior, directory)
    original = json.loads((parent/'atom-mapping.json').read_text())['atoms']
    inventory = json.loads((parent/'inventory.json').read_text())
    target = inventory['mapping']['entity_seq_id']
    movable = next(a['entity_position'] for a in original if a['original_serial'] is not None and a['entity_position'] != target)
    source_sha = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    policy = make_policy(source_sha, original, [movable], [target], target)
    if bounded:
        from covscreen.receptor_local_policy import make_bounded_policy
        policy = make_bounded_policy(policy)
        if bounded == 'block':
            from covscreen.receptor_local_policy import make_block_policy
            policy = make_block_policy(policy)
    receipt = json.loads((directory/'receipt.json').read_text())
    receipt.update(schema='receptor-relaxation-receipt-v2',
        status='position_restrained_relaxation_completed_quality_pending',
        artificial_forces_in_final_stage=True, policy=policy, policy_sha256=digest(policy),
        test_fixture='synthetic receipt, no local minimization has been performed')
    receipt['stages'] = [dict(stage, observed_atoms_fixed=i != 2,
        artificial_position_restraint=i == 2, artificial_chirality_restraint=i != 2)
        for i, stage in enumerate(policy['stages'])]
    if bounded:
        receipt['stages'][-1].update(final_energy_kj_mol=0., tolerance_reached=True,
            convergence_metric='projected_gradient_max_kj_mol_nm', bounded_optimizer={
                'status':'converged', 'energy_kj_mol':0., 'projected_gradient_max_kj_mol_nm':0.,
                'iterations':0, 'evaluations':1, 'accepted_energy_history_kj_mol':[0.],
                'scientific_acceptance':False})
        if bounded == 'block':
            numerical = receipt['stages'][-1]['bounded_optimizer']
            numerical.update(full_residual_available=True,
                phases=[dict(numerical, phase='initial_full_residual')])
    mapped_path = directory/'heavy-atom-mapping.json'
    mapped = json.loads(mapped_path.read_text())
    references = {(a['entity_position'], a['name']): a['xyz_angstrom'] for a in original}
    for atom in mapped['atoms']:
        if atom['original_serial'] is not None:
            atom['source_xyz_angstrom'] = references[atom['entity_position'], atom['name']]
            atom['coordinate_origin'] = 'relaxed_observed_atom'
    def publish():
        mapped_path.write_text(json.dumps(mapped))
        for name in receipt['artifacts']:
            receipt['artifacts'][name] = hashlib.sha256((directory/name).read_bytes()).hexdigest()
        (directory/'receipt.json').write_text(json.dumps(receipt))
        return hashlib.sha256((directory/'receipt.json').read_bytes()).hexdigest()
    if bounded:
        receipt['stages'][-1]['bounded_optimizer']['status'] = 'iteration_limit'
        with pytest.raises(ValueError, match='convergence'):
            audit_restrained_receptor(directory, publish(), parent, SOURCE)
        receipt['stages'][-1]['bounded_optimizer']['status'] = 'converged'
    audited = audit_restrained_receptor(directory, publish(), parent, SOURCE)
    assert audited['status'] == 'identity_and_stereochemistry_passed_geometry_pending'
    assert audited['local_motion_audit']['status'] == 'local_motion_limits_passed'
    atom = next(a for a in mapped['atoms'] if a['entity_position']==target and a['name']=='SG')
    atom['xyz_angstrom'][0] += 0.01
    lines = (directory/'relaxed.pdb').read_text().splitlines()
    for i,line in enumerate(lines):
        if line.startswith(('ATOM  ','HETATM')) and line[12:16].strip()=='SG' and line[21].strip()==atom['chain'] and line[22:27].strip()==atom['author_residue']:
            lines[i] = line[:30] + f"{atom['xyz_angstrom'][0]:8.3f}" + line[38:]
    (directory/'relaxed.pdb').write_text('\n'.join(lines)+'\n')
    audited = audit_restrained_receptor(directory, publish(), parent, SOURCE)
    assert audited['status'] == 'local_motion_limits_failed'
    violation, = audited['local_motion_audit']['violations']
    assert violation['entity_position']==target and violation['name']=='SG' and not violation['movable']
    assert not audited['receptor_prepared']
