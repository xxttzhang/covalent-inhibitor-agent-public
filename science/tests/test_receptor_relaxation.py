import json
import os
from pathlib import Path

import numpy as np

from covscreen.receptor_relaxation import (RELAXATION_POLICY_V1, _bind_chirality_terms,
                                            _chirality_summary)


ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(os.environ.get('COVSCREEN_HISTORICAL_PROJECT_ROOT', ROOT)) / 'reports/library-first-site-acquisition-20261014'


def test_registered_policy_ends_with_real_forcefield_only():
    policy = RELAXATION_POLICY_V1
    assert policy['hydrogen_seed'] == 0
    assert policy['rebuild_hydrogens_before_unrestrained'] is True
    final = policy['stages'][-1]
    assert final == {'name': 'amber_unrestrained', 'soft_nonbonded': False,
                     'chirality_restraint': False, 'max_iterations': 1000,
                     'tolerance_kj_mol_nm': 10.0}


def test_real_plan_rebinds_to_protonated_atom_order_without_changing_meaning():
    plan = json.loads((REPORT / 'chirality-plan-regression.json').read_text())['terms']
    heavy = json.loads((REPORT / 'heavy-repair-regression/atom-mapping.json').read_text())['atoms']
    all_atoms = json.loads((REPORT / 'receptor-relaxation-regression-v4/atom-mapping.json').read_text())['atoms']
    assert _bind_chirality_terms(plan, heavy) == plan
    rebound = _bind_chirality_terms(plan, all_atoms)
    fields = ('entity_position', 'author_residue', 'resname', 'center',
              'expected_cip', 'neighbor_names', 'sign', 'movable')
    assert [{k: term[k] for k in fields} for term in rebound] == [
        {k: term[k] for k in fields} for term in plan]
    assert any(a['index'] != b['index'] for term_a, term_b in zip(plan, rebound)
               for a, b in zip([heavy[i] for i in term_a['particles']],
                               [all_atoms[i] for i in term_b['particles']]))


def test_signed_volume_summary_identifies_only_wrong_orientation():
    terms = [{'entity_position': 1, 'center': 'CA', 'sign': 1, 'movable': True,
              'particles': [0, 1, 2, 3]},
             {'entity_position': 2, 'center': 'CA', 'sign': -1, 'movable': False,
              'particles': [0, 1, 2, 3]}]
    xyz = np.asarray([[0, 0, 0], [1, 0, 0], [0, 1, 0], [0, 0, 1]], dtype=float)
    summary = _chirality_summary(terms, xyz)
    assert summary['checked'] == 2
    assert [(v['entity_position'], v['signed_volume_nm3']) for v in summary['violations']] == [(2, -1.0)]
    assert summary['minimum_movable_signed_volume_nm3'] == 1.0


def test_observed_position_restraint_energy_force_and_mobility():
    import openmm as mm
    from openmm import unit
    from covscreen.receptor_relaxation import observed_position_force
    system = mm.System()
    system.addParticle(12.0)
    system.addForce(observed_position_force([0], [[0., 0., 0.]], 1000.))
    integrator = mm.VerletIntegrator(0.001)
    context = mm.Context(system, integrator, mm.Platform.getPlatformByName('Reference'))
    context.setPositions([[0.1, 0., 0.]])
    state = context.getState(getEnergy=True, getForces=True)
    assert abs(state.getPotentialEnergy().value_in_unit(unit.kilojoule_per_mole) - 5.) < 1e-9
    force = state.getForces(asNumpy=True).value_in_unit(unit.kilojoule_per_mole / unit.nanometer)
    assert np.allclose(force[0], [-100., 0., 0.])
    mm.LocalEnergyMinimizer.minimize(context, 0.001, 100)
    position = context.getState(getPositions=True).getPositions(asNumpy=True).value_in_unit(unit.nanometer)
    assert np.linalg.norm(position[0]) < 1e-6
    del context, integrator


def test_position_restraint_rejects_invalid_references():
    import pytest
    from covscreen.receptor_relaxation import observed_position_force
    for indices, coordinates, stiffness in (([0, 0], [[0, 0, 0]], 1),
            ([1], [[0, 0, 0]], 1), ([0], [[float('nan'), 0, 0]], 1),
            ([0], [[0, 0, 0]], -1)):
        with pytest.raises(ValueError):
            observed_position_force(indices, coordinates, stiffness)


def test_worker_binds_explicit_policy_before_relaxation(tmp_path, monkeypatch):
    """Exercise the executable boundary without launching scientific computation."""
    import hashlib
    import runpy
    import sys
    import pytest
    import covscreen.receptor_relaxation as relaxation
    from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
    worker = ROOT / 'routes/library-first/scripts/receptor_preparation_worker.py'
    request_path = tmp_path / 'request.json'
    request = dict(parent='parent', parent_receipt_sha256='parent-sha',
        chirality_plan='plan', chirality_plan_sha256='plan-sha', source='source',
        relaxation_policy=RELAXATION_POLICY_V2)
    calls = []
    def capture(*args, **kwargs):
        calls.append(kwargs)
        return {'status': 'software_test_only'}
    monkeypatch.setattr(relaxation, 'relax_receptor', capture)
    monkeypatch.setattr(sys, 'argv', [str(worker), '--mode', 'relax',
        '--request', str(request_path), '--output', str(tmp_path / 'output')])
    request['relaxation_policy_sha256'] = '0' * 64
    request_path.write_text(json.dumps(request))
    with pytest.raises(ValueError, match='frozen request digest'):
        runpy.run_path(str(worker), run_name='__main__')
    assert calls == []
    request['relaxation_policy_sha256'] = hashlib.sha256(json.dumps(
        RELAXATION_POLICY_V2, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    request_path.write_text(json.dumps(request))
    runpy.run_path(str(worker), run_name='__main__')
    assert calls == [{'policy': RELAXATION_POLICY_V2}]
    del request['relaxation_policy']
    request_path.write_text(json.dumps(request))
    with pytest.raises(ValueError, match='requires an explicit policy'):
        runpy.run_path(str(worker), run_name='__main__')
    assert len(calls) == 1


def test_submission_compiler_refuses_unbound_relaxation_policy():
    import pytest
    from covscreen.execution import JobManager
    base = dict(receptor_input_artifact_id='fixture', manifest_sha256='0' * 64,
        altlocs={}, heavy_seed=20260725, max_missing_residues=27)
    for extra, message in [
        ({'relaxation_policy_version': 'unknown'}, 'Unregistered'),
        ({'relaxation_policy_version': 'v2'}, 'exact registered policy'),
        ({'relaxation_policy': {}}, 'requires its registered v2'),
    ]:
        with pytest.raises(ValueError, match=message):
            JobManager._build_receptor_preparation(None, 'software-test', {**base, **extra})


def test_public_receptor_plan_discloses_observed_motion():
    from covscreen.ligand_receptor_source import public_record
    from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
    spec = {'parameters': {'relaxation_policy_version': 'v2',
        'relaxation_policy': RELAXATION_POLICY_V2, 'relaxation_policy_sha256': 'fixture'}}
    result = public_record({'spec': spec})['plan']
    assert result['observed_atoms_may_move'] is True
    assert result['relaxation_policy'] == RELAXATION_POLICY_V2
    assert result['observed_motion_acceptance'] == 'not_evaluated'
    assert result['receptor_admission_after_computation'] is False
    assert 'observed_atoms_may_move' not in public_record({'spec': {'parameters': {}}})['plan']


def test_review_does_not_turn_pending_motion_into_admission(monkeypatch):
    from covscreen import ligand_receptor_execution as execution
    from covscreen import ligand_receptor_evidence as evidence
    from types import SimpleNamespace
    record = {'run_id': 'synthetic-review'}
    state = {'vs_receptor': {'receptor_preparation': record}}
    evaluation = {'receptor_prepared': False, 'prepared_scope': None,
        'output_validation': 'OBSERVED_MOTION_REVIEW_PENDING'}
    registry = SimpleNamespace(load_spec=lambda _: None, load_result=lambda _: None)
    monkeypatch.setattr(execution, 'JobManager', lambda _: SimpleNamespace(registry=registry))
    monkeypatch.setattr(evidence, 'evaluate', lambda *args, **kwargs: evaluation)
    monkeypatch.setattr(evidence, 'freeze_review', lambda *args: {'synthetic': True})
    output = execution.review(None, state, {})
    assert record['evaluation_verification'] == 'verified'
    assert record['receptor_prepared'] is False
    assert output['receptor_prepared'] is False
    assert output['md_ready'] is False


def test_restrained_report_reopens_without_claiming_admission(tmp_path):
    from covscreen.ligand_receptor_report import freeze
    review = {'schema': 'ligand-receptor-review-v2', 'run_id': 'synthetic',
        'receptor_prepared': False, 'prepared_scope': None, 'result': {
            'quality': {'status': 'synthetic_identity_pass', 'observed_coordinate_displacements': {
                'atom_count': 2, 'rms_displacement_angstrom': 0.1,
                'maximum_displacement_angstrom': 0.2}},
            'geometry': {'status': 'synthetic_geometry_pass'}}}
    record = {'evaluation_verification': 'verified', 'evaluation': review}
    state = {'workflow_id': 'synthetic', 'design_id': 'synthetic', 'target': {},
        'vs_receptor': {'receptor_preparation': record}}
    report = freeze(tmp_path, state)
    record['report'] = report
    assert freeze(tmp_path, state) == report
    text = (tmp_path / report['report_markdown']).read_text()
    assert '受体尚未准入' in text and '0.200000' in text
    assert report['md_ready'] is False and report['covdock_ready'] is False


def test_public_receptor_history_uses_same_projection_as_current_record():
    from covscreen.ligand_receptor_source import public_context
    record = {'run_id': 'old-failure', 'job_state': 'FAILED',
        'spec': {'engine': 'fixture', 'parameters': {}},
        'approved_scope': {'private_fixture': True}}
    state = {'receptor_preparation_history': [record]}
    public = public_context(state)['receptor_preparation_history'][0]
    assert public['run_id'] == 'old-failure' and public['job_state'] == 'FAILED'
    assert 'spec' not in public and 'approved_scope' not in public
    assert public['plan']['engine'] == 'fixture'
    assert state['receptor_preparation_history'][0] == record


def test_vs_receptor_handoff_refuses_verified_but_unadmitted_proposal(monkeypatch):
    from covscreen import ligand_receptor_execution as execution
    from covscreen.ligand_vs_receptor import augment
    import pytest
    state = {'vs_receptor': {'receptor_preparation': {'run_id': 'synthetic',
        'evaluation_artifact': {'synthetic': True},
        'evaluation': {'receptor_prepared': False, 'result': {}}}}}
    monkeypatch.setattr(execution, 'observe', lambda *args: {})
    monkeypatch.setattr(execution, 'review', lambda *args, **kwargs: {})
    with pytest.raises(ValueError, match='current receptor admission'):
        augment(None, state, {}, {})


def test_stage_identity_is_written_before_optimization_and_rejects_wrong_order(tmp_path):
    import pytest
    import openmm.app as app
    from covscreen.receptor_relaxation import _write_stage_identity, _sha
    top=app.Topology();chain=top.addChain('A');res=top.addResidue('CYS',chain,'797')
    cb=top.addAtom('CB',app.element.carbon,res);sg=top.addAtom('SG',app.element.sulfur,res)
    top.addBond(cb,sg)
    atoms=[dict(index=i,entity_position=102,chain='A',author_residue='797',resname='CYS',
                name=name,element=element,xyz_angstrom=[float(i),0.,0.],original_serial=i+1)
           for i,(name,element) in enumerate([('CB','C'),('SG','S')])]
    digest=_write_stage_identity(tmp_path,atoms,top)
    assert digest['atom_mapping_sha256']==_sha(tmp_path/'stage-atom-mapping.json')
    assert digest['topology_sha256']==_sha(tmp_path/'stage-topology.json')
    assert json.loads((tmp_path/'stage-atom-mapping.json').read_text())['atoms']==atoms
    assert json.loads((tmp_path/'stage-topology.json').read_text())['bonds']==[[0,1]]
    assert not (tmp_path/'receipt.json').exists()  # Identity is not a completion/admission receipt.
    wrong=[dict(a,index=i) for i,a in enumerate(reversed(atoms))]
    with pytest.raises(ValueError,match='actual OpenMM topology'):_write_stage_identity(tmp_path,wrong,top)
    assert json.loads((tmp_path/'stage-atom-mapping.json').read_text())['atoms']==atoms


def test_stage_connectivity_detects_changed_hydrogen_parent():
    from types import SimpleNamespace
    from covscreen.receptor_relaxation import _topology_bonds
    atoms = [SimpleNamespace(index=i) for i in range(3)]
    def topology(edges):
        return SimpleNamespace(bonds=lambda: [(atoms[a], atoms[b]) for a, b in edges])
    original = topology([(0, 1), (0, 2)])
    reordered = topology([(2, 0), (1, 0)])
    changed_parent = topology([(0, 1), (1, 2)])
    assert _topology_bonds(original) == _topology_bonds(reordered)
    assert _topology_bonds(original) != _topology_bonds(changed_parent)
