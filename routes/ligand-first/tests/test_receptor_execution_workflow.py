"""Real native protocol and local registration, with worker launch isolated.

Only the final run_job_worker process is replaced by a real no-op child; imports,
compilation, signatures, submission journal and task registration remain real.
No receptor repair or simulation is run and no completion is scientific evidence.
"""
import json
from pathlib import Path
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from covscreen.approval_scope import approval_scope
from covscreen.execution import JobManager
from covscreen.ligand_workflow import DESIGN, ENTRY, navigate
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed, needs_approval
from test_receptor_input_workflow import design, graph_design, pose, inputs, bound, configure, args, jobs


SUBMIT = JobManager.submit
POPEN = subprocess.Popen


@pytest.fixture
def planned_receptor(bound):
    configure(bound)
    dispatch(bound[0], DESIGN, args(bound, 'check_receptor_runtime'))
    return bound


@pytest.fixture
def isolated(planned_receptor, monkeypatch):
    launches = []
    def launch(command, *args, **kwargs):
        if len(command) > 1 and Path(command[1]).name == 'run_job_worker.py':
            launches.append(list(command))
            process = POPEN([sys.executable, '-B', '-c', 'pass'], *args, **kwargs)
            process.wait(timeout=10)
            return process
        return POPEN(command, *args, **kwargs)
    monkeypatch.setattr(subprocess, 'Popen', launch)
    return planned_receptor, launches


def approval(pose):
    values = {**args(pose, 'start_receptor_preparation'), 'human_approved': True,
              'approval_note': 'Isolated software test: replace worker launch; no receptor repair'}
    scope = approval_scope(pose[0], DESIGN, values)
    proof = signed({'tool': DESIGN, 'arguments_json': json.dumps(values), 'scope': scope,
        'session': 'fixture', 'message': 'fixture', 'approval_request_id': 'per_fixture_receptor',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**values, '_native_approval': proof}


def record(response):
    return response['refinement_input']['receptor_preparation']


def test_failed_preparation_diagnostics_survive_reopen_and_reject_changed_bytes(isolated):
    """Synthetic failed worker artifacts test readback, never scientific success."""
    from covscreen.jobs import sha256_file
    pose, launches = isolated
    response = dispatch(pose[0], DESIGN, approval(pose))
    run = record(response)['run_id']
    manager = JobManager(pose[0])
    result = manager.registry.load_result(run)
    folder = manager.registry.job_dir(run) / 'artifacts/receptor-preparation'
    folder.mkdir(parents=True)
    geometry = folder / 'geometry-quality.json'
    geometry.write_text(json.dumps({'schema': 'receptor-geometry-audit-v1',
        'status': 'geometry_quality_failed', 'policy': {'schema': 'fixture-policy'},
        'distance_checks': {'internal': {'status': 'fail', 'tested': 200,
            'violation_count': 25, 'modeled_pair_violations': 0,
            'retained_observed_pair_violations': 25,
            'first_100_violations': [{'distance_angstrom': 2.3}] * 25}},
        'vdw_clashes': [{'distance_angstrom': 2.3}] * 25,
        'aromatic_planarity_violations': [],
        'reactive_site_prereaction_state': {'status': 'pass'}}))
    pipeline = folder / 'pipeline.json'
    pipeline.write_text(json.dumps({'schema': 'receptor-preparation-pipeline-v1',
        'target_acquisition_sha256': response['refinement_input']['receptor_input']['manifest_sha256'],
        'status': 'preparation_failed', 'error': 'Synthetic geometry failure',
        'receptor_prepared': False, 'phases': [{'phase': 'geometry_quality', 'status': 'geometry_quality_failed'}],
        'artifacts': {'geometry-quality.json': sha256_file(geometry)}}))
    result.state, result.return_code = 'FAILED', 1
    result.failure_reason = 'engine_return_code_1'
    result.artifacts = [manager.registry.register_file(run, path, 'other') for path in (pipeline, geometry)]
    manager.registry.write_result(result)
    observed = dispatch(pose[0], DESIGN, args(pose, 'receptor_preparation_status'))
    detail = observed['result']['failure_diagnostics']
    assert detail['run_id'] == run and detail['pipeline_available']
    assert detail['error'] == 'Synthetic geometry failure'
    assert detail['geometry_quality']['vdw_clash_count'] == 25
    assert len(detail['geometry_quality']['vdw_clash_examples']) == 10
    assert detail['geometry_quality']['distance_checks']['internal']['violation_count'] == 25
    assert len(detail['sources']) == 2
    assert not detail['receptor_prepared'] and not detail['md_ready']
    assert observed['assessment']['scope'] == 'receptor_preparation_failure'
    assert 'reconsider' in observed['workflow']['tools'][ENTRY]['actions']
    assert observed['workflow']['tools'][DESIGN]['actions'] == ['receptor_preparation_status', 'refresh_receptor_preparation']
    reopened = navigate(pose[0], 'status', workflow_id=pose[1])
    assert record(reopened)['failure_diagnostics'] == detail
    assert reopened['workflow']['revision'] == observed['workflow']['revision']
    raw = geometry.read_bytes()
    geometry.write_bytes(raw + b'\n')
    rejected = navigate(pose[0], 'status', workflow_id=pose[1])
    assert record(rejected)['run_id'] == run and 'failure_diagnostics' not in record(rejected)
    assert rejected['assessment']['state'] == 'evidence_insufficient'
    geometry.write_bytes(raw)
    restored = navigate(pose[0], 'status', workflow_id=pose[1])
    assert record(restored)['failure_diagnostics'] == detail
    assert 'evidence_error' not in record(restored)
    restored = dispatch(pose[0], DESIGN, args(pose, 'refresh_receptor_preparation'))
    revised = navigate(pose[0], 'reconsider', workflow_id=pose[1],
        expected_revision=restored['workflow']['revision'],
        reason='Retain failed preparation while reviewing an alternative deposited source')
    assert revised['workflow']['stage'] == 'PARENT_REVIEW'
    saved = json.loads(pose[2].read_text())
    archived = saved['branches'][-1]['refinement_input']['receptor_preparation']
    assert archived['run_id'] == run and archived['failure_diagnostics'] == detail
    assert len(launches) == 1 and len(jobs(pose)) == 2


def test_failed_preparation_without_registered_pipeline_does_not_read_unregistered_bytes(isolated):
    pose, launches = isolated
    response = dispatch(pose[0], DESIGN, approval(pose))
    run = record(response)['run_id']
    manager = JobManager(pose[0])
    folder = manager.registry.job_dir(run) / 'artifacts/receptor-preparation'
    folder.mkdir(parents=True)
    (folder / 'pipeline.json').write_text('unregistered secret-like content must not be read')
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'FAILED', 1
    manager.registry.write_result(result)
    observed = dispatch(pose[0], DESIGN, args(pose, 'receptor_preparation_status'))
    detail = observed['result']['failure_diagnostics']
    assert detail['pipeline_available'] is False and detail['sources'] == []
    assert 'secret-like' not in json.dumps(observed)
    assert len(launches) == 1


def test_native_boolean_is_not_permission_and_plan_exposes_exact_start(planned_receptor):
    assert needs_approval(DESIGN, {'action': 'start_receptor_preparation'})
    response = navigate(planned_receptor[0], 'status', workflow_id=planned_receptor[1])
    assert 'start_receptor_preparation' in response['workflow']['tools'][DESIGN]['actions']
    with pytest.raises(ValueError, match='approval'):
        dispatch(planned_receptor[0], DESIGN, {**args(planned_receptor, 'start_receptor_preparation'), 'human_approved': True})
    assert len(jobs(planned_receptor)) == 1


def test_one_registered_cpu_task_survives_recovery_and_completion_is_not_acceptance(isolated):
    pose, launches = isolated
    response = dispatch(pose[0], DESIGN, approval(pose))
    run = record(response)['run_id']
    assert response['workflow']['stage'] == 'RECEPTOR_PREPARATION_RUNNING'
    assert response['execution']['task_ids'] == [run]
    assert response['execution']['state'] == 'submitted'
    assert response['assessment']['state'] == 'not_evaluated'
    assert record(response)['approval'] == 'APPROVED'
    assert len(launches) == 1 and len(jobs(pose)) == 2

    restored = navigate(pose[0], 'status', workflow_id=pose[1])
    assert restored['workflow']['tools'][DESIGN]['actions'] == ['receptor_preparation_status', 'refresh_receptor_preparation']
    assert restored['workflow']['tools'][ENTRY]['actions'] == ['status', 'suspend']
    with pytest.raises(ValueError):
        dispatch(pose[0], DESIGN, approval(pose))
    with pytest.raises(ValueError, match='execution'):
        navigate(pose[0], 'reconsider', workflow_id=pose[1], expected_revision=restored['workflow']['revision'], reason='Must preserve active job')
    manager = JobManager(pose[0])
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'COMPLETED', 0  # Software fixture, no prepared files.
    manager.registry.write_result(result)
    response = dispatch(pose[0], DESIGN, args(pose, 'receptor_preparation_status'))
    assert response['workflow']['stage'] == 'RECEPTOR_PREPARATION_REVIEW'
    assert response['execution']['state'] == 'completed'
    assert response['assessment']['state'] == 'not_evaluated'
    assert record(response)['receptor_prepared'] is False
    assert response['result']['md_ready'] is False
    assert len(launches) == 1 and len(jobs(pose)) == 2


@pytest.mark.parametrize('change', ['signed_spec', 'result_identity'])
def test_changed_registered_job_is_rejected_without_losing_identity(isolated, change):
    pose, launches = isolated
    response = dispatch(pose[0], DESIGN, approval(pose))
    run = record(response)['run_id']
    manager = JobManager(pose[0])
    path = manager.registry.job_dir(run) / ('spec.json' if change == 'signed_spec' else 'result.json')
    original = path.read_bytes()
    content = json.loads(original)
    if change == 'signed_spec':
        content['resource_budget']['cpu_count'] = 2
    else:
        content['project_id'] = 'another-study'
    path.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        dispatch(pose[0], DESIGN, args(pose, 'receptor_preparation_status'))
    rejected = navigate(pose[0], 'status', workflow_id=pose[1])
    assert record(rejected)['run_id'] == run
    assert rejected['assessment']['state'] == 'evidence_insufficient'
    assert rejected['workflow']['tools'][DESIGN]['actions'] == ['receptor_preparation_status', 'refresh_receptor_preparation']
    path.write_bytes(original)
    restored = dispatch(pose[0], DESIGN, args(pose, 'receptor_preparation_status'))
    assert 'evidence_error' not in record(restored)
    assert record(restored)['run_id'] == run
    assert len(launches) == 1 and len(jobs(pose)) == 2

def test_lost_reply_reconciles_original_registration_without_launching_again(isolated, monkeypatch):
    pose, launches = isolated
    def lost_reply(manager, request):
        SUBMIT(manager, request)
        raise RuntimeError('Test transport failure after registration')
    monkeypatch.setattr(JobManager, 'submit', lost_reply)
    with pytest.raises(RuntimeError):
        dispatch(pose[0], DESIGN, approval(pose))
    state = json.loads(pose[2].read_text())
    assert state['pending']['action'] == 'start_receptor_preparation'
    response = navigate(pose[0], 'reconcile', workflow_id=pose[1], expected_revision=state['revision'])
    assert response['pending'] is None
    assert record(response)['run_id'] in jobs(pose)
    assert len(launches) == 1 and len(jobs(pose)) == 2


def test_unknown_submission_remains_pending_without_automatic_retry(isolated, monkeypatch):
    pose, launches = isolated
    def before_registration(*args):
        raise RuntimeError('Test transport failure before observable registration')
    monkeypatch.setattr(JobManager, 'submit', before_registration)
    with pytest.raises(RuntimeError):
        dispatch(pose[0], DESIGN, approval(pose))
    state = json.loads(pose[2].read_text())
    response = navigate(pose[0], 'reconcile', workflow_id=pose[1], expected_revision=state['revision'])
    assert response['pending']['action'] == 'start_receptor_preparation'
    assert response['execution']['state'] == 'outcome_unknown'
    assert response['workflow']['tools'][ENTRY]['actions'] == ['status', 'reconcile']
    assert len(launches) == 0 and len(jobs(pose)) == 1


@pytest.mark.parametrize('change', ['source', 'budget', 'runtime'])
def test_approval_rejects_changed_plan_before_launch(isolated, change):
    pose, launches = isolated
    proof = approval(pose)
    state = json.loads(pose[2].read_text())
    if change == 'source':
        path = pose[0] / state['refinement_input']['receptor_input']['directory'] / 'canonical.fasta'
        path.write_bytes(path.read_bytes() + b'\n')
    elif change == 'budget':
        proof['resource_budget'] = {'gpu_count': 1}
    else:
        path = pose[0] / 'configs/local.inspire.yaml'
        path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError):
        dispatch(pose[0], DESIGN, proof)
    assert not launches and len(jobs(pose)) == 1


def test_input_failure_never_discards_the_registered_job_or_reopens_start(isolated):
    pose, launches = isolated
    response = dispatch(pose[0], DESIGN, approval(pose))
    run = record(response)['run_id']
    path = pose[0] / response['refinement_input']['receptor_input']['directory'] / 'structure.pdb'
    path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='frozen input changed'):
        dispatch(pose[0], DESIGN, args(pose, 'receptor_preparation_status'))
    response = navigate(pose[0], 'status', workflow_id=pose[1])
    assert record(response)['run_id'] == run
    assert response['assessment']['state'] == 'evidence_insufficient'
    assert response['workflow']['tools'][DESIGN]['actions'] == ['receptor_preparation_status', 'refresh_receptor_preparation']
    # The same invariant holds after terminal state and normal status refresh.
    manager = JobManager(pose[0])
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'FAILED', 1
    manager.registry.write_result(result)
    with pytest.raises(ValueError):
        dispatch(pose[0], DESIGN, args(pose, 'receptor_preparation_status'))
    response = navigate(pose[0], 'status', workflow_id=pose[1])
    assert record(response)['run_id'] == run
    assert response['workflow']['stage'] == 'RECEPTOR_PREPARATION_REVIEW'
    assert len(launches) == 1 and len(jobs(pose)) == 2


@pytest.mark.parametrize("job_state", ["QUEUED", "COMPLETED", "FAILED"])
def test_repeated_status_does_not_write_workflow_or_jobs(isolated, job_state):
    """A diagnostic read must not commit an observation or launch work."""
    pose, launches = isolated
    started = dispatch(pose[0], DESIGN, approval(pose))
    manager = JobManager(pose[0])
    result = manager.registry.load_result(record(started)['run_id'])
    result.state = job_state
    result.return_code = None if job_state == 'QUEUED' else (0 if job_state == 'COMPLETED' else 1)
    manager.registry.write_result(result)
    from covscreen.ligand_workflow import directory
    location = directory(pose[0], pose[1])
    def snapshot():
        roots = [pose[0] / 'state', pose[0] / 'results']
        return {str(path): path.read_bytes() for root in roots for path in root.rglob('*')
                if path.is_file() and not path.name.endswith('.lock')}
    arguments = args(pose, 'receptor_preparation_status')
    before = snapshot()
    first = dispatch(pose[0], DESIGN, arguments)
    assert snapshot() == before
    second = dispatch(pose[0], DESIGN, arguments)
    assert snapshot() == before
    assert first['workflow']['revision'] == second['workflow']['revision'] == arguments['expected_revision']
    assert first['execution'] == second['execution']
    for _ in range(2):
        reopened = navigate(pose[0], 'status', workflow_id=pose[1])
        assert snapshot() == before
        assert reopened['workflow']['revision'] == arguments['expected_revision']
        assert reopened['execution'] == first['execution']
    assert len(launches) == 1


def test_rejected_status_does_not_persist_rejection(isolated):
    pose, launches = isolated
    started = dispatch(pose[0], DESIGN, approval(pose))
    manager = JobManager(pose[0])
    path = manager.registry.job_dir(record(started)['run_id']) / 'result.json'
    value = json.loads(path.read_text())
    value['project_id'] = 'wrong-study'
    path.write_text(json.dumps(value))
    state_before = pose[2].read_bytes()
    result_before = path.read_bytes()
    values = args(pose, 'receptor_preparation_status')
    for _ in range(2):
        with pytest.raises(ValueError):
            dispatch(pose[0], DESIGN, values)
        assert pose[2].read_bytes() == state_before
        assert path.read_bytes() == result_before
    assert len(launches) == 1


def test_explicit_refresh_only_commits_changed_observation(isolated):
    pose, launches = isolated
    started = dispatch(pose[0], DESIGN, approval(pose))
    before = pose[2].read_bytes()
    unchanged = dispatch(pose[0], DESIGN, args(pose, 'refresh_receptor_preparation'))
    assert pose[2].read_bytes() == before
    manager = JobManager(pose[0])
    result = manager.registry.load_result(record(started)['run_id'])
    result.state, result.return_code = 'FAILED', 1
    manager.registry.write_result(result)
    refreshed = dispatch(pose[0], DESIGN, args(pose, 'refresh_receptor_preparation'))
    assert refreshed['workflow']['revision'] == unchanged['workflow']['revision'] + 1
    assert record(refreshed)['job_state'] == 'FAILED'
    before = pose[2].read_bytes()
    again = dispatch(pose[0], DESIGN, args(pose, 'refresh_receptor_preparation'))
    assert pose[2].read_bytes() == before
    assert again['workflow']['revision'] == refreshed['workflow']['revision']
    assert len(launches) == 1
