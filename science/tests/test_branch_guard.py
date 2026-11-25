import json
from types import SimpleNamespace

import pytest

from covscreen.branch_guard import BranchStopped, require_unquarantined
from covscreen.mcp.server import dispatch
from covscreen.mcp.task_guard import guarded_call
from test_result_verification import fixture

ENV = {'COVALENT_ERROR_POLICY': 'classified-v1'}


def quarantine(root):
    registry, result, output = fixture(root)
    (output / 'stage2_candidates.csv').write_text('tampered')
    response = guarded_call(root, 'covalent_verify_job_result',
                            {'run_id': result.run_id}, dispatch, environment=ENV)
    assert response['verified'] is False
    assert response['task_guard']['dependent_execution_blocked'] is True
    assert not (root / 'state/task_guard/stopped.json').exists()
    return registry, result, output


def test_failed_verification_quarantines_dependencies_not_independent_work(tmp_path):
    from covscreen.execution import JobManager
    from test_submission_identity import request
    registry, failed, _ = quarantine(tmp_path)
    dependent = request().model_copy(update={'parent_run_id': failed.run_id,
                                            'project_id': 'renamed-project'})
    result = guarded_call(tmp_path, 'covalent_submit_job', dependent.model_dump(),
                          dispatch, environment=ENV)
    assert result['failure']['category'] == 'dependency_quarantined'
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()
    assert len(registry.list_results()) == 1
    independent = JobManager(tmp_path).submit(request())
    assert independent.state == 'PLANNED'
    assert len(registry.list_results()) == 2
    observed = guarded_call(tmp_path, 'covalent_get_job', {'run_id': failed.run_id},
                            dispatch, environment=ENV)
    assert observed['status'] == 'PASS'


def test_relabelled_or_copied_outputs_remain_blocked(tmp_path):
    registry, failed, output = quarantine(tmp_path)
    spec = registry.load_spec(failed.run_id)
    candidate = next(a for a in failed.artifacts if a.path.endswith('stage2_candidates.sdf'))
    for inputs in ({'renamed-copy.sdf': candidate.sha256},
                   {str(output / 'new-name.sdf'): 'different-hash'}):
        with pytest.raises(BranchStopped):
            require_unquarantined(tmp_path, SimpleNamespace(parent_run_id=None,
                input_sha256=inputs, input_paths=[], project_id='different'))
    # Shared upstream inputs are not outputs of the failed branch.
    require_unquarantined(tmp_path, SimpleNamespace(parent_run_id=None,
        input_sha256=spec.input_sha256, input_paths=[], project_id=spec.project_id))


def test_worker_rechecks_quarantine_before_launch(tmp_path):
    from test_job_cancellation import queued
    from covscreen.execution import execute_persisted_job
    registry, failed, _ = quarantine(tmp_path)
    _, run = queued(tmp_path)
    spec = registry.load_spec(run)
    spec.parent_run_id = failed.run_id
    registry.write_spec(spec)
    with pytest.raises(BranchStopped):
        execute_persisted_job(tmp_path, run)
    assert not (tmp_path / 'started').exists()
    assert registry.load_result(run).state == 'QUEUED'


def test_missing_identity_keeps_global_stop(tmp_path):
    response = guarded_call(tmp_path, 'covalent_verify_job_result',
                            {'run_id': 'missing-run'}, dispatch, environment=ENV)
    assert response['status'] == 'FAIL'
    assert (tmp_path / 'state/task_guard/stopped.json').exists()


def test_reverification_cannot_clear_original_quarantine(tmp_path):
    from covscreen.branch_guard import quarantine_job
    registry, failed, _ = quarantine(tmp_path)
    marker = tmp_path / 'state/branch_guard' / (failed.run_id + '.json')
    before = marker.read_bytes()
    quarantine_job(tmp_path, failed.run_id, 'another-tool', 'another-error')
    assert marker.read_bytes() == before
    with pytest.raises(BranchStopped):
        require_unquarantined(tmp_path, SimpleNamespace(parent_run_id=failed.run_id))


def test_corrupt_quarantine_fails_closed(tmp_path):
    _, failed, _ = quarantine(tmp_path)
    marker = tmp_path / 'state/branch_guard' / (failed.run_id + '.json')
    marker.write_text(json.dumps({'schema': 'unknown', 'run_id': failed.run_id}))
    with pytest.raises(ValueError):
        require_unquarantined(tmp_path, SimpleNamespace(parent_run_id=None))


def test_transitive_artifact_dependency_without_parent_link_is_blocked(tmp_path):
    registry, failed, _ = quarantine(tmp_path)
    spec = registry.load_spec(failed.run_id)
    upstream = next(a for a in failed.artifacts if a.path.endswith('stage2_candidates.sdf'))
    output = tmp_path / 'intermediate-output'
    output.mkdir()
    downstream = output / 'derived.sdf'
    downstream.write_text('synthetic derived output, no scientific computation')
    child = spec.model_copy(update={'run_id': 'intermediate-001',
        'project_id': 'different-project', 'parent_run_id': None,
        'input_sha256': {'copied-parent.sdf': upstream.sha256},
        'output_directory': str(output), 'expected_output_paths': [str(output)]})
    result = failed.model_copy(update={'run_id': child.run_id, 'project_id': child.project_id,
                                       'artifacts': []})
    registry.create(child, result)
    artifact = registry.register_file(child.run_id, downstream, 'other')
    result.artifacts = [artifact]
    registry.write_result(result)
    with pytest.raises(BranchStopped):
        require_unquarantined(tmp_path, SimpleNamespace(parent_run_id=None,
            input_sha256={'copied-grandchild.sdf': artifact.sha256}, input_paths=[]))


def test_quarantine_survives_new_process(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    _, failed, _ = quarantine(tmp_path)
    script = '''
import sys
from types import SimpleNamespace
from covscreen.branch_guard import BranchStopped, require_unquarantined
try:
    require_unquarantined(sys.argv[1], SimpleNamespace(parent_run_id=sys.argv[2]))
except BranchStopped:
    print('blocked')
else:
    raise AssertionError('quarantine lost')
'''
    process = subprocess.run([sys.executable, '-c', script, str(tmp_path), failed.run_id],
        check=True, text=True, capture_output=True,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
    assert process.stdout.strip() == 'blocked'


@pytest.mark.parametrize('state,blocked', [('RUNNING', False), ('QUEUED', False),
    ('FAILED', True), ('TIMED_OUT', True), ('CANCELLED', True)])
def test_verifying_pending_job_does_not_quarantine_but_terminal_failure_does(tmp_path, state, blocked):
    registry, job, _ = fixture(tmp_path)
    job.state = state
    job.return_code = None if state in {'RUNNING', 'QUEUED'} else 2
    registry.write_result(job)
    response = guarded_call(tmp_path, 'covalent_verify_job_result',
        {'run_id': job.run_id}, dispatch, environment=ENV)
    assert response['verified'] is False
    assert response['status'] == 'BLOCKED'
    assert (tmp_path / 'state/branch_guard' / (job.run_id + '.json')).exists() is blocked
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()


def test_compiled_inputs_checked_even_without_declared_parent(tmp_path, monkeypatch):
    from covscreen.execution import JobManager
    from test_submission_identity import request
    registry, failed, output = quarantine(tmp_path)
    copied = tmp_path / 'copied.sdf'
    copied.write_bytes((output / 'stage2_candidates.sdf').read_bytes())
    manager = JobManager(tmp_path)
    # Exercise the real compiler boundary with a synthetic registered builder;
    # no executable is launched and no scientific result is supplied.
    monkeypatch.setattr(manager, '_build_rdkit', lambda *_:
        (['never-executed'], [str(copied)], [str(tmp_path / 'new-output')]))
    with pytest.raises(BranchStopped):
        manager.submit(request())
    assert len(registry.list_results()) == 1


def test_guard_status_is_read_only_and_available_under_study_stop(tmp_path):
    registry, failed, _ = quarantine(tmp_path)
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-run'}, dispatch, environment=ENV)
    original = {str(p): p.read_bytes() for p in (tmp_path / 'state').rglob('*.json')}
    result = guarded_call(tmp_path, 'covalent_get_guard_status', {}, dispatch, environment=ENV)
    assert result['status'] == 'PASS'
    assert result['study_stop']['category'] == 'invalid_input_or_identity'
    assert result['quarantined_jobs'][0]['run_id'] == failed.run_id
    assert result['execution_authorized'] is False
    assert result['calculation_submitted'] is False
    assert result['task_guard']['execution_blocked'] is True
    assert original == {str(p): p.read_bytes() for p in (tmp_path / 'state').rglob('*.json')}


@pytest.mark.parametrize('arguments', [{'limit': True}, {'limit': '10'}, {'limit': 0},
    {'limit': 101}, {'clear': True}, {'human_approved': True}])
def test_guard_status_rejects_invalid_arguments(tmp_path, arguments):
    with pytest.raises(ValueError):
        dispatch(tmp_path, 'covalent_get_guard_status', arguments)
    assert not (tmp_path / 'state').exists()


def test_guard_status_empty_does_not_create_registry_or_authorize(tmp_path):
    result = dispatch(tmp_path, 'covalent_get_guard_status', {})
    assert result['quarantined_jobs'] == []
    assert result['study_stop'] is None
    assert result['execution_authorized'] is False
    assert not (tmp_path / 'state').exists()


def test_quarantine_between_compile_and_submit_prevents_registration(tmp_path, monkeypatch):
    from covscreen.branch_guard import quarantine_job
    from covscreen.execution import JobManager
    from test_submission_identity import request
    registry, failed, _ = fixture(tmp_path)
    manager = JobManager(tmp_path)
    original = manager._prepare_submission
    def compile_then_quarantine(value):
        spec = original(value)
        quarantine_job(tmp_path, failed.run_id, 'verification', 'test-failure')
        return spec
    monkeypatch.setattr(manager, '_prepare_submission', compile_then_quarantine)
    with pytest.raises(BranchStopped):
        manager.submit(request().model_copy(update={'parent_run_id': failed.run_id}))
    assert len(registry.list_results()) == 1


def test_quarantine_after_worker_initial_check_prevents_process_launch(tmp_path, monkeypatch):
    from covscreen.branch_guard import quarantine_job
    from covscreen.execution import execute_persisted_job
    from covscreen import dashboard
    from test_job_cancellation import queued
    registry, failed, _ = fixture(tmp_path)
    _, run = queued(tmp_path)
    spec = registry.load_spec(run)
    spec.parent_run_id = failed.run_id
    registry.write_spec(spec)
    monkeypatch.setattr(dashboard, 'try_refresh_dashboard', lambda *_:
        quarantine_job(tmp_path, failed.run_id, 'verification', 'test-failure'))
    result = execute_persisted_job(tmp_path, run)
    assert result.state == 'FAILED'
    assert 'BranchStopped' in result.failure_reason
    assert result.engine_pid is None
    assert not (tmp_path / 'started').exists()


def test_quarantine_writer_waits_for_execution_boundary(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from covscreen.branch_guard import execution_barrier, quarantine_job
    _, failed, _ = fixture(tmp_path)
    entered = Event()
    def record():
        entered.set()
        return quarantine_job(tmp_path, failed.run_id, 'verification', 'test-failure')
    with ThreadPoolExecutor(1) as executor:
        with execution_barrier(tmp_path):
            pending = executor.submit(record)
            assert entered.wait(3)
            assert not pending.done()
            assert not (tmp_path / 'state/branch_guard' / (failed.run_id + '.json')).exists()
        assert pending.result(timeout=3)


@pytest.mark.parametrize('fresh_process', [False, True])
def test_workflow_resume_persists_quarantine_instead_of_reoffering_plan(tmp_path, fresh_process):
    from covscreen.branch_guard import quarantine_job
    from covscreen.library_chain import LibraryFirstChainConfig
    registry, job, output = fixture(tmp_path)
    artifacts = registry.job_dir(job.run_id) / 'artifacts'
    output.rename(artifacts)
    import pyarrow.parquet as pq
    table = pq.read_table(artifacts / 'stage2_candidates.parquet')
    table = table.append_column('input_sdf_index', [[0]])
    pq.write_table(table, artifacts / 'stage2_candidates.parquet')
    spec = registry.load_spec(job.run_id)
    spec.output_directory = str(artifacts)
    spec.expected_output_paths = [str(artifacts)]
    registry.write_spec(spec)
    from test_result_verification import refresh
    refresh(registry, job, artifacts)
    for filename in ('target.yaml', 'protein.pdb', 'reference.sdf'):
        (tmp_path / filename).write_text('synthetic planning fixture')
    config = LibraryFirstChainConfig(campaign_id='guarded-chain', project_id='test',
        terminal_stage='rtmscore', target_config='target.yaml', pdb='protein.pdb',
        reference_ligand='reference.sdf', attachment_residue='A:773', covres='A:CYS:773',
        seed_run_ids={'prescreen': job.run_id})
    path = tmp_path / 'chain.json'
    path.write_text(config.model_dump_json())
    first = dispatch(tmp_path, 'covalent_library_first_chain', {'config_path': str(path)})
    assert first['chain']['terminal_state'] == 'ready'
    assert first['next_request'] is not None
    assert first['execution_readiness']['resources']['availability'] == 'NOT_VERIFIED_FOR_THIS_PLAN'
    identity = first['workflow_state']['workflow_id']
    quarantine_job(tmp_path, job.run_id, 'verification', 'test-failure')
    if fresh_process:
        import os
        import subprocess
        import sys
        import covscreen
        from pathlib import Path
        source_root = str(Path(covscreen.__file__).resolve().parents[1])
        script = '''
import json, sys
from pathlib import Path
from covscreen.mcp.server import dispatch
from covscreen.mcp.task_guard import guarded_call
response = guarded_call(Path(sys.argv[1]), 'covalent_resume_workflow',
    {'workflow_id': sys.argv[2]}, dispatch,
    environment={'COVALENT_ERROR_POLICY': 'classified-v1'})
print(json.dumps(response))
'''
        # A new interpreter has no model/chat memory or parent Python objects.
        process = subprocess.run([sys.executable, '-c', script, str(tmp_path), identity],
            env={**os.environ, 'PYTHONPATH': source_root, 'PYTHONDONTWRITEBYTECODE': '1'},
            capture_output=True, text=True, check=True, timeout=30)
        resumed = json.loads(process.stdout)
    else:
        resumed = guarded_call(tmp_path, 'covalent_resume_workflow', {'workflow_id': identity},
            dispatch, environment=ENV)
    assert resumed['status'] == 'BLOCKED'
    assert resumed['chain']['terminal_state'] == 'dependency_quarantined'
    assert resumed['next_request'] is None
    assert resumed['chain']['next_stage'] is None
    assert resumed['submitted'] is False
    assert resumed['workflow_state']['revision'] == 2
    snapshot = json.loads((tmp_path / resumed['workflow_state']['path']).read_text())
    assert snapshot['approval_required_for_execution'] is True
    assert snapshot['next_request'] is None
    assert snapshot['execution_readiness_observation']['plan_available'] is False
    listed = dispatch(tmp_path, 'covalent_list_workflows', {})
    assert listed['workflows'][0]['last_observed_state'] == 'dependency_quarantined'
    assert len(registry.list_results()) == 1
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()


def test_unresolved_registered_identity_cannot_be_quarantined_as_known_branch(tmp_path):
    registry, result, _ = fixture(tmp_path)
    for name in ('spec.json', 'result.json'):
        path = registry.job_dir(result.run_id) / name
        payload = json.loads(path.read_text())
        payload['run_id'] = 'different-valid-run-001'
        path.write_text(json.dumps(payload))
    response = guarded_call(tmp_path, 'covalent_verify_job_result',
        {'run_id': result.run_id}, dispatch, environment=ENV)
    assert response['status'] == 'FAIL'
    assert response['failure']['category'] == 'invalid_input_or_identity'
    assert (tmp_path / 'state/task_guard/stopped.json').is_file()
    assert not list((tmp_path / 'state/branch_guard').glob('*.json'))
