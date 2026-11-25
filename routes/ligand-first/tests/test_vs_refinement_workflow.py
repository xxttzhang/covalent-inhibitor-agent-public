"""Signed VS registry to LO input handoff; no vendor or scheduler execution.

Only the positive geometry measurement is controlled. Registered raw products,
source identities, selection, target derivation and tamper checks are real.
This is software evidence, never scientific acceptance of fixture coordinates.
"""
import json

import pytest

from covscreen.ligand_af3_execution import verify_inputs
from covscreen.ligand_refinement_input import load_refinement_input
from covscreen.ligand_workflow import DESIGN, navigate
from test_vs_evidence_workflow import completed, planned, study, design, call


@pytest.fixture
def eligible(completed, monkeypatch):
    from covscreen import ligand_vs_evidence
    from covscreen.covdock_vs_geometry import assess
    def measurement(row, campaign, parent, inventory, projection, policy, parent_strain):
        metrics = {'scaffold_rmsd_angstrom': 0.1, 'interaction_retention_fraction': 1.,
            'strain_excess_over_parent_kcal_mol': 0., 'covalent_cs_distance_angstrom': 1.8,
            'product_s_cbeta_calpha_angle_degrees': 110., 'clash_fraction': 0.,
            'severe_clash_pair_count': 0}
        return {'pose_index': row['pose_index'], 'candidate_id': row['candidate_id'],
            'input_index': row['input_index'], 'covdock_vs_score': row['score'],
            'metrics': metrics, 'assessment': assess(metrics, policy), 'md_ready': False}
    monkeypatch.setattr(ligand_vs_evidence, 'measure', measurement)
    study, raw = completed
    result = call(study, 'evaluate_vs')
    candidate = result['vs_execution']['evaluation_summary']['candidates'][0]['candidate_id']
    return study, raw, candidate


def test_vs_subset_reaches_existing_receptor_preparation_without_old_pose(eligible):
    study, raw, candidate = eligible
    root, path = study
    args = {'candidate_ids': [candidate], 'reason': 'Select the verified VS candidate for independent LO refinement'}
    output = call(study, 'prepare_vs_refinement', **args)
    assert output['workflow']['stage'] == 'VS_REFINEMENT_READY'
    state = json.loads(path.read_text())
    ref = state['refinement_input']
    assert not state.get('pose_execution')
    assert not list(root.glob('results/ligand-designs/*/pose-plan'))
    assert ref['source_kind'] == 'registered-ligand-vs-review-v1'
    table = (root/ref['candidate_table']).read_text()
    assert 'schrodinger-covdock-vs' in table and 'carsidock_score' not in table
    assert load_refinement_input(root, state)['input_count'] == 1
    assert call(study, 'prepare_vs_refinement', **args)['refinement_input'] == ref
    with pytest.raises(ValueError, match='frozen'):
        call(study, 'prepare_vs_refinement', **{**args, 'reason': 'Different selection rationale'})
    call(study, 'prepare_vs_refinement_target')
    state = json.loads(path.read_text())
    candidates, target = verify_inputs(root, state)
    assert candidates['source_run_id'] == state['vs_execution']['run_id']
    assert target['receptor_prepared'] is False
    assert 'prepare_receptor_input' in navigate(root, 'status', workflow_id=state['workflow_id'])['workflow']['tools'][DESIGN]['actions']
    output = call(study, 'prepare_receptor_input')
    assert output['result']['compute_submitted'] is False
    assert len(list(root.glob('state/jobs/*/spec.json'))) == 1
    assert not json.loads(path.read_text()).get('covdock_execution')
    before = raw.read_bytes()
    raw.write_bytes(before + b'changed')
    with pytest.raises(ValueError):
        verify_inputs(root, json.loads(path.read_text()))
    failed = navigate(root, 'status', workflow_id=state['workflow_id'])
    assert failed['assessment']['scope'] == 'vs_refinement_source'
    assert failed['refinement_input']['target'] == state['refinement_input']['target']
    raw.write_bytes(before)
    restored = navigate(root, 'resume', workflow_id=state['workflow_id'])
    assert 'source_error' not in restored['refinement_input']
    assert 'target_error' not in restored['refinement_input']
    assert verify_inputs(root, json.loads(path.read_text()))[0] == candidates


def test_vs_selection_rejects_missing_failed_and_duplicate_candidates(eligible):
    study, _, candidate = eligible
    state = json.loads(study[1].read_text())
    missing = state['vs_execution']['evaluation_summary']['candidates'][1]['candidate_id']
    for names in ([missing], ['foreign-candidate'], [candidate, candidate], []):
        with pytest.raises(ValueError):
            call(study, 'prepare_vs_refinement', candidate_ids=names, reason='Explicit test selection')
    assert not json.loads(study[1].read_text()).get('refinement_input')


def test_negative_real_geometry_does_not_offer_refinement(completed):
    study, _ = completed
    output = call(study, 'evaluate_vs')
    assert 'prepare_vs_refinement' not in output['workflow']['tools'][DESIGN]['actions']
    candidate = output['vs_execution']['evaluation_summary']['candidates'][0]['candidate_id']
    with pytest.raises(ValueError, match='jointly passing'):
        call(study, 'prepare_vs_refinement', candidate_ids=[candidate], reason='Must reject this failed pose')


def test_frozen_subset_tamper_and_cross_study_are_rejected(eligible):
    study, _, candidate = eligible
    root, path = study
    call(study, 'prepare_vs_refinement', candidate_ids=[candidate], reason='Test immutable provenance')
    state = json.loads(path.read_text())
    table = root/state['refinement_input']['candidate_table']
    raw = table.read_bytes()
    table.write_bytes(raw + b'changed')
    with pytest.raises(ValueError):
        load_refinement_input(root, state)
    table.write_bytes(raw)
    load_refinement_input(root, state)
    state['workflow_id'] = 'ligand-flow-' + 'f'*24
    with pytest.raises(ValueError):
        load_refinement_input(root, state)


def test_vs_handoff_keeps_separate_native_receptor_approval_and_downstream_status(eligible, monkeypatch):
    import subprocess
    import sys
    from pathlib import Path
    from covscreen.execution import JobManager
    from covscreen.mcp.server import dispatch
    from test_receptor_input_workflow import configure
    from test_receptor_execution_workflow import approval
    study, _, candidate = eligible
    root, path = study
    call(study, 'prepare_vs_refinement', candidate_ids=[candidate], reason='Test separate preparation approval')
    call(study, 'prepare_vs_refinement_target')
    call(study, 'prepare_receptor_input')
    state = json.loads(path.read_text())
    case = root, state['workflow_id'], path
    configure(case)
    result = call(study, 'check_receptor_runtime')
    assert result['refinement_input']['receptor_preparation']['runtime_check'] == 'CPU_PREPARATION_PLAN_COMPILED'
    with pytest.raises(ValueError, match='approval'):
        call(study, 'start_receptor_preparation', human_approved=True)
    original = subprocess.Popen
    launches = []
    def launch(command, *args, **kwargs):
        if len(command) > 1 and Path(command[1]).name == 'run_job_worker.py':
            launches.append(command)
            process = original([sys.executable, '-B', '-c', 'pass'], *args, **kwargs)
            process.wait(timeout=10)
            return process
        return original(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    proof = approval(case)
    result = dispatch(root, DESIGN, proof)
    run = result['refinement_input']['receptor_preparation']['run_id']
    assert run != result['vs_execution']['run_id']
    assert result['execution']['task_ids'] == [run]
    assert result['execution']['state'] == 'submitted'
    assert result['workflow']['stage'] == 'RECEPTOR_PREPARATION_RUNNING'
    restored = navigate(root, 'resume', workflow_id=state['workflow_id'])
    assert restored['execution']['task_ids'] == [run]
    assert restored['workflow']['tools'][DESIGN]['actions'] == [
        'receptor_preparation_status', 'refresh_receptor_preparation',
    ]
    with pytest.raises(ValueError):
        dispatch(root, DESIGN, proof)
    assert len(launches) == 1 and len(list(root.glob('state/jobs/*/spec.json'))) == 2
    assert JobManager(root).registry.load_spec(run).engine == 'library-first-receptor-preparation'
