"""Real MCP, compiler and external queue registration; no dispatcher or GPU.

The upstream pose review and runtime asset files are software fixtures. Native
approval proofs are signed only with an isolated test key.
"""
import copy
import json
import time
from uuid import uuid4

import pytest

from covscreen.approval_scope import approval_scope
from covscreen.execution import JobManager
from covscreen.ligand_design import write
from covscreen.ligand_workflow import DESIGN, ENTRY, navigate
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed, needs_approval
from test_refinement_target import design, graph_design, pose, positive_review
from test_pose_execution_workflow import args, completed_pose_files


SUBMIT = JobManager.submit


@pytest.fixture
def inputs(pose, monkeypatch):
    completed_pose_files(pose, monkeypatch)
    review = positive_review(pose)
    monkeypatch.setattr('covscreen.ligand_pose_evidence.evaluate_outputs', lambda *args: copy.deepcopy(review))
    dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement'))
    dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement_target'))
    # The existing external-inspire backend registers a queue item without
    # launching a worker. Exercise that actual implementation for AF3.
    monkeypatch.setattr(JobManager, 'submit', SUBMIT)
    return pose


def configure(inputs):
    root = inputs[0]
    runtime = root / 'runtime'
    for name in ('run_alphafold.py', 'af3-python'):
        (runtime / name).write_text('Software-only runtime placeholder; never executed\n')
    for name in ('models', 'databases'):
        (runtime / name).mkdir(exist_ok=True)
    path = root / 'configs/local.inspire.yaml'
    config = json.loads(path.read_text())
    config['alphafold3'] = {key: str(runtime / name) for key, name in (
        ('runner', 'run_alphafold.py'), ('python', 'af3-python'), ('model_dir', 'models'), ('database_dir', 'databases'))}
    write(path, config)


@pytest.fixture
def planned(inputs):
    configure(inputs)
    dispatch(inputs[0], DESIGN, args(inputs, 'check_af3_runtime'))
    return inputs


def approval(pose):
    values = {**args(pose, 'start_af3'), 'human_approved': True,
              'approval_note': 'Software fixture: queue registration only; do not dispatch'}
    scope = approval_scope(pose[0], DESIGN, values)
    proof = signed({'tool': DESIGN, 'arguments_json': json.dumps(values), 'scope': scope,
        'session': 'fixture', 'message': 'fixture', 'approval_request_id': 'per_fixture_af3',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**values, '_native_approval': proof}


def jobs(pose):
    return sorted(path.name for path in (pose[0] / 'state/jobs').iterdir())


def test_missing_runtime_is_recoverable_configuration_wait_not_gpu_queue(inputs):
    before = jobs(inputs)
    response = dispatch(inputs[0], DESIGN, args(inputs, 'check_af3_runtime'))
    assert response['workflow']['stage'] == 'AF3_RUNTIME_REQUIRED'
    assert response['result']['gpu_queue_observed'] is False
    assert response['execution']['state'] == 'not_submitted'
    assert response['result']['runtime_check'] == 'WAITING_RUNTIME_CONFIGURATION'
    assert 'start_af3' not in response['workflow']['tools'][DESIGN]['actions']
    request_id = response['af3_execution']['request_id']
    configure(inputs)
    response = dispatch(inputs[0], DESIGN, args(inputs, 'check_af3_runtime'))
    assert response['workflow']['stage'] == 'AF3_PLAN_READY'
    assert response['af3_execution']['request_id'] == request_id
    assert jobs(inputs) == before


def test_new_runtime_configuration_does_not_rewrite_parent_snapshot(planned):
    manager = JobManager(planned[0])
    state = json.loads(planned[2].read_text())
    parent = manager.registry.load_spec(state['pose_execution']['run_id'])
    frozen = planned[0] / parent.provenance['config_path']
    assert parent.provenance['config_binding'] == 'immutable-runtime-config-v1'
    assert frozen.read_bytes() != (planned[0] / 'configs/local.inspire.yaml').read_bytes()
    assert frozen.stat().st_mode & 0o777 == 0o600
    before = frozen.read_bytes()
    dispatch(planned[0], DESIGN, args(planned, 'check_af3_runtime'))
    assert frozen.read_bytes() == before
    frozen.write_bytes(before + b'\n')
    with pytest.raises(ValueError, match='Pose job frozen input changed'):
        approval(planned)
    assert len(jobs(planned)) == 1


def test_frozen_plan_uses_study_seed_exact_candidates_and_data_pipeline(planned):
    before = json.loads(planned[2].read_text())
    response = dispatch(planned[0], DESIGN, args(planned, 'check_af3_runtime'))
    record = response['af3_execution']
    assert record == before['af3_execution']
    spec = record['spec']
    assert '--run-data-pipeline' in spec['command']
    assert '--resume' not in spec['command']
    assert spec['parameters']['resume'] is False
    assert spec['parameters']['af3_target_seed_check']['cysteine_sequence_index'] == 102
    assert spec['parameters']['expected_input_count'] == before['refinement_input']['input_count']
    assert spec['parameters']['source_pose_run_id'] == before['pose_execution']['run_id']
    assert set(spec['parameters']['refinement_lineage_files']).issubset(spec['input_sha256'])
    assert spec['resource_budget'] == {'gpu_count': 1, 'cpu_count': 20, 'memory_gb': 200,
        'max_runtime_minutes': 90, 'max_candidates': before['refinement_input']['input_count']}
    assert len(jobs(planned)) == 1  # Only the upstream software pose job.


def test_model_boolean_cannot_approve_af3(planned):
    assert needs_approval(DESIGN, {'action': 'start_af3'})
    with pytest.raises(ValueError, match='approval'):
        dispatch(planned[0], DESIGN, {**args(planned, 'start_af3'), 'human_approved': True})
    assert len(jobs(planned)) == 1


def test_actual_registered_queue_is_recoverable_and_completion_is_not_science(planned):
    proof = approval(planned)
    response = dispatch(planned[0], DESIGN, proof)
    run = response['af3_execution']['run_id']
    assert response['workflow']['stage'] == 'AF3_EXECUTION_RUNNING'
    assert response['execution']['state'] == 'submitted'
    assert response['assessment']['state'] == 'not_evaluated'
    assert len(jobs(planned)) == 2
    with pytest.raises(ValueError):
        dispatch(planned[0], DESIGN, approval(planned))
    state = navigate(planned[0], 'status', workflow_id=planned[1])
    assert state['af3_execution']['run_id'] == run
    assert state['workflow']['tools'][DESIGN]['actions'] == ['af3_status']
    with pytest.raises(ValueError, match='execution'):
        navigate(planned[0], 'reconsider', workflow_id=planned[1],
            expected_revision=state['workflow']['revision'], reason='Cannot discard a queued task')
    manager = JobManager(planned[0])
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'COMPLETED', 0  # Software completion, no scientific files.
    manager.registry.write_result(result)
    response = dispatch(planned[0], DESIGN, args(planned, 'af3_status'))
    assert response['workflow']['stage'] == 'AF3_EXECUTION_REVIEW'
    assert response['execution']['state'] == 'completed'
    assert response['assessment']['state'] == 'not_evaluated'
    assert not response['result']['scientific_result_verified']
    assert not response['result']['md_ready']
    assert len(jobs(planned)) == 2


def test_lost_submission_reply_recovers_original_registration(planned, monkeypatch):
    def lose_reply(manager, request):
        SUBMIT(manager, request)
        raise RuntimeError('Software transport failure after registration')
    monkeypatch.setattr(JobManager, 'submit', lose_reply)
    with pytest.raises(RuntimeError, match='transport failure'):
        dispatch(planned[0], DESIGN, approval(planned))
    state = json.loads(planned[2].read_text())
    assert state['pending']['action'] == 'start_af3'
    before = jobs(planned)
    response = navigate(planned[0], 'reconcile', workflow_id=planned[1], expected_revision=state['revision'])
    assert response['pending'] is None
    assert response['af3_execution']['run_id'] in before
    assert jobs(planned) == before


def test_absent_receipt_does_not_allow_a_second_submission(planned, monkeypatch):
    def before_registration(*args):
        raise RuntimeError('Software transport failure before observable registration')
    monkeypatch.setattr(JobManager, 'submit', before_registration)
    with pytest.raises(RuntimeError):
        dispatch(planned[0], DESIGN, approval(planned))
    state = json.loads(planned[2].read_text())
    response = navigate(planned[0], 'reconcile', workflow_id=planned[1], expected_revision=state['revision'])
    assert response['pending']['action'] == 'start_af3'
    assert response['result']['submission_state'] == 'NO_RECEIPT'
    assert response['execution']['state'] == 'outcome_unknown'
    assert response['workflow']['tools'][ENTRY]['actions'] == ['status', 'reconcile']
    assert len(jobs(planned)) == 1


@pytest.mark.parametrize('change', ['seed', 'runtime', 'lineage'])
def test_approval_rejects_plan_drift_without_registration(planned, change):
    proof = approval(planned)
    state = json.loads(planned[2].read_text())
    if change == 'runtime':
        path = planned[0] / 'configs/local.inspire.yaml'
        data = json.loads(path.read_text())
        data['alphafold3']['runner'] = data['alphafold3']['python']
        write(path, data)
    else:
        target = planned[0] / state['refinement_input']['target']['directory']
        path = target / ('af3-source.json' if change == 'seed' else 'target-input.json')
        path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError):
        dispatch(planned[0], DESIGN, proof)
    assert len(jobs(planned)) == 1


def test_input_failure_preserves_registered_task_and_rejects_new_start(planned):
    response = dispatch(planned[0], DESIGN, approval(planned))
    run = response['af3_execution']['run_id']
    path = planned[0] / response['refinement_input']['target']['directory'] / 'af3-source.json'
    path.write_text('{}')
    with pytest.raises(ValueError, match='frozen input changed'):
        dispatch(planned[0], DESIGN, args(planned, 'af3_status'))
    status = navigate(planned[0], 'status', workflow_id=planned[1])
    assert status['af3_execution']['run_id'] == run
    assert status['assessment']['state'] == 'evidence_insufficient'
    assert status['workflow']['tools'][DESIGN]['actions'] == ['af3_status']
    assert len(jobs(planned)) == 2
