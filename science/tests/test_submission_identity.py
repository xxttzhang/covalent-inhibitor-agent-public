from concurrent.futures import ThreadPoolExecutor
import pytest

from covscreen.execution import JobManager
from covscreen.jobs import JobSubmissionRequest, ResourceBudget


def request():
    return JobSubmissionRequest(request_id='dedup-test', project_id='test', route='library-first',
        engine='rdkit-warhead-enumeration', execution_mode='plan',
        parameters={'scaffold_smiles': 'CCO', 'attachment_atom_indices': [1], 'allowed_warheads': ['acrylamide']},
        resource_budget=ResourceBudget(max_runtime_minutes=1, max_candidates=2))


def test_repeated_and_concurrent_plan_registration_returns_same_job(tmp_path):
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda _: JobManager(tmp_path).submit(request()), range(3)))
    assert len({result.run_id for result in results}) == 1
    manager = JobManager(tmp_path)
    assert len(manager.registry.list_results()) == 1
    assert manager.submit(request()).run_id == results[0].run_id


def test_same_identity_changed_budget_rejected(tmp_path):
    manager = JobManager(tmp_path)
    manager.submit(request())
    changed = request().model_copy(update={'resource_budget': ResourceBudget(max_runtime_minutes=2)})
    with pytest.raises(ValueError, match='changed inputs, parameters or budget'):
        manager.submit(changed)
    assert len(manager.registry.list_results()) == 1


@pytest.mark.parametrize('mutation', ['receipt_identity', 'both_run_ids', 'result_project', 'attempt', 'parent'])
@pytest.mark.parametrize('entry', ['submit', 'lookup'])
def test_duplicate_submission_rejects_corrupt_registered_identity(tmp_path, mutation, entry):
    import json
    manager = JobManager(tmp_path)
    result = manager.submit(request())
    folder = manager.registry.job_dir(result.run_id)
    receipt = next((tmp_path / 'state/submissions').glob('*.json'))
    if mutation == 'receipt_identity':
        value = json.loads(receipt.read_text())
        value['identity'][1] = 'another-request'
        receipt.write_text(json.dumps(value))
    elif mutation == 'both_run_ids':
        for name in ('spec.json', 'result.json'):
            path = folder / name
            value = json.loads(path.read_text()); value['run_id'] = 'wrong-run-000001'
            path.write_text(json.dumps(value))
    else:
        path = folder / 'result.json'
        value = json.loads(path.read_text())
        value[{'result_project': 'project_id', 'attempt': 'attempt', 'parent': 'parent_run_id'}[mutation]] = (
            2 if mutation == 'attempt' else 'wrong-identity-000001')
        path.write_text(json.dumps(value))
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    with pytest.raises(ValueError, match='identity(?: or state)? mismatch'):
        if entry == 'submit':
            JobManager(tmp_path).submit(request())
        else:
            from covscreen.submission_identity import lookup_submission
            lookup_submission(tmp_path, {'project_id': 'test', 'request_id': 'dedup-test', 'execution_mode': 'plan'})
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


def test_registration_failure_cannot_silently_retry(tmp_path):
    from covscreen.submission_identity import submit_once
    manager = JobManager(tmp_path)
    def interrupted(_):
        raise ConnectionError('lost response after boundary')
    with pytest.raises(ConnectionError):
        submit_once(manager, request(), interrupted)
    with pytest.raises(ValueError, match='uncertain or failed'):
        submit_once(JobManager(tmp_path), request(), interrupted)


def test_lookup_registered_request_via_mcp_does_not_create_job(tmp_path):
    from covscreen.mcp.server import dispatch
    manager = JobManager(tmp_path)
    result = manager.submit(request())
    before = sorted(str(p) for p in tmp_path.rglob('*'))
    lookup = dispatch(tmp_path, 'covalent_lookup_submission', {
        'project_id': 'test', 'request_id': 'dedup-test', 'execution_mode': 'plan'})
    assert lookup['run_id'] == result.run_id
    assert lookup['job_state'] == 'PLANNED'
    assert lookup['scientific_result_verified'] is False
    assert lookup['automatic_resubmission_allowed'] is False
    assert before == sorted(str(p) for p in tmp_path.rglob('*'))


def test_lost_response_keeps_bound_run_and_never_unlocks_submission(tmp_path):
    from covscreen.submission_identity import submit_once, lookup_submission
    from covscreen.mcp.server import dispatch
    from covscreen.mcp.task_guard import guarded_call
    manager = JobManager(tmp_path)
    spec = manager._prepare_submission(request())
    def lose_response(value):
        manager._submit_registered(value, spec)
        raise ConnectionError('response lost after registry persisted')
    with pytest.raises(ConnectionError):
        submit_once(manager, request(), lose_response, planned_run_id=spec.run_id)
    args = {'project_id': 'test', 'request_id': 'dedup-test', 'execution_mode': 'plan'}
    env = {'COVALENT_ERROR_POLICY': 'classified-v1'}
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-job'}, dispatch, environment=env)
    marker = tmp_path / 'state/task_guard/stopped.json'
    original = marker.read_bytes()
    result = guarded_call(tmp_path, 'covalent_lookup_submission', args, dispatch, environment=env)
    assert result['submission_state'] == 'UNCERTAIN_OR_FAILED'
    assert result['run_id'] == spec.run_id
    assert marker.read_bytes() == original
    with pytest.raises(ValueError, match='uncertain'):
        manager.submit(request())
    assert len(manager.registry.list_results()) == 1


def test_missing_receipt_and_incomplete_registration_never_authorize_retry(tmp_path):
    from covscreen.submission_identity import lookup_submission, submit_once
    args = {'project_id': 'test', 'request_id': 'dedup-test', 'execution_mode': 'plan'}
    missing = lookup_submission(tmp_path, args)
    assert missing['submission_state'] == 'NO_RECEIPT'
    assert not missing['automatic_resubmission_allowed']
    assert list(tmp_path.iterdir()) == []
    manager = JobManager(tmp_path)
    def interrupted(_):
        raise ConnectionError('before registration')
    with pytest.raises(ConnectionError):
        submit_once(manager, request(), interrupted, planned_run_id='reserved-test-000001')
    result = lookup_submission(tmp_path, args)
    assert result['registered_job_observed'] is False
    assert result['reserved_run_id'] == 'reserved-test-000001'
    assert 'run_id' not in result


def test_lookup_rejects_receipt_pointing_to_different_request(tmp_path):
    import json
    from covscreen.submission_identity import lookup_submission
    manager = JobManager(tmp_path)
    manager.submit(request())
    other = request().model_copy(update={'request_id': 'other-request'})
    other_result = manager.submit(other)
    receipt = next(p for p in (tmp_path / 'state/submissions').glob('*.json')
                   if json.loads(p.read_text())['identity'][1] == 'dedup-test')
    payload = json.loads(receipt.read_text()); payload['run_id'] = other_result.run_id
    receipt.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match='identity mismatch'):
        lookup_submission(tmp_path, {'project_id': 'test', 'request_id': 'dedup-test', 'execution_mode': 'plan'})


@pytest.mark.parametrize('mutation', ['changed', 'missing'])
def test_lookup_rejects_changed_or_missing_frozen_input(tmp_path, mutation):
    from covscreen.submission_identity import lookup_submission
    manager = JobManager(tmp_path)
    result = manager.submit(request())
    spec = manager.registry.load_spec(result.run_id)
    assert spec.input_sha256
    source = tmp_path / next(iter(spec.input_sha256))
    if mutation == 'changed':
        source.write_text('changed after registration')
    else:
        source.unlink()
    receipts = {p: p.read_bytes() for p in (tmp_path / 'state/submissions').glob('*.json')}
    with pytest.raises(ValueError, match='frozen input changed or missing'):
        lookup_submission(tmp_path, {'project_id': 'test', 'request_id': 'dedup-test', 'execution_mode': 'plan'})
    assert all(p.read_bytes() == data for p, data in receipts.items())
    assert len(manager.registry.list_results()) == 1


def test_lookup_input_conflict_quarantines_only_verified_branch(tmp_path):
    from covscreen.mcp.server import dispatch
    from covscreen.mcp.task_guard import guarded_call
    manager = JobManager(tmp_path)
    failed = manager.submit(request())
    other = manager.submit(request().model_copy(update={'request_id': 'independent-request'}))
    spec = manager.registry.load_spec(failed.run_id)
    (tmp_path / next(iter(spec.input_sha256))).write_text('changed')
    env = {'COVALENT_ERROR_POLICY': 'classified-v1'}
    response = guarded_call(tmp_path, 'covalent_lookup_submission', {
        'project_id': 'test', 'request_id': 'dedup-test', 'execution_mode': 'plan'}, dispatch, environment=env)
    assert response['status'] == 'FAIL'
    assert response['failure']['attempts'] == 1
    assert response['task_guard']['dependent_execution_blocked']
    assert response['task_guard']['study_stopped'] is False
    assert response['guard_status']['total_quarantined_jobs'] == 1
    assert response['guard_status']['quarantined_jobs'][0]['run_id'] == failed.run_id
    assert (tmp_path / 'state/branch_guard' / (failed.run_id + '.json')).is_file()
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()
    independent = guarded_call(tmp_path, 'covalent_lookup_submission', {
        'project_id': 'test', 'request_id': 'independent-request', 'execution_mode': 'plan'}, dispatch, environment=env)
    assert independent['status'] == 'PASS'
    assert independent['run_id'] == other.run_id
    assert independent['automatic_resubmission_allowed'] is False
