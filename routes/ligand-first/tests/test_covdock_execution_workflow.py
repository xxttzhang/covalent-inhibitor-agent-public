"""Actual native protocol/ledger; scientific worker replaced by a no-op child.

Historical receptor and controlled positive upstream judgments are software
fixtures. Inert vendor assets are never executed or used to check out a license.
"""
import json
import time
from uuid import uuid4

import pytest

from covscreen.approval_scope import approval_scope
from covscreen.execution import JobManager
from covscreen.ligand_covdock_input import request_for
from covscreen.ligand_workflow import DESIGN, ENTRY, navigate
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed
from covscreen.receptor_environment_review import CONCERNS
from test_covdock_input_workflow import (design, graph_design, pose, inputs, bound,
    planned_receptor, isolated, completed, reviewed, configure, args, jobs)


SUBMIT = JobManager.submit


@pytest.fixture
def ready(reviewed):
    case, launches, folder, manager, run = reviewed
    dispatch(case[0], DESIGN, args(case, 'prepare_covdock_input'))
    output = dispatch(case[0], DESIGN, args(case, 'read_covdock_environment'))
    inventory = {item['path']: item['sha256'] for item in output['result']['evidence']['documents']}
    path = next(name for name in inventory if name.endswith('environment-decision.json'))
    review = {name: {'decision': 'suitable_for_exploratory_refinement',
        'rationale': 'Controlled positive judgment for workflow protocol testing, not scientific endorsement.',
        'evidence': [{'path': path, 'sha256': inventory[path], 'locator': '/policy',
                      'observation': 'The isolated-protein preparation policy is explicitly recorded.'}],
        'limitations': ['Synthetic judgment: biological suitability is not validated by this test.']}
        for name in CONCERNS}
    dispatch(case[0], DESIGN, {**args(case, 'review_covdock_environment'), 'environment_review': review})
    configure(case)
    dispatch(case[0], DESIGN, args(case, 'check_covdock_runtime'))
    return reviewed


def approval(case):
    arguments = {**args(case, 'start_covdock'), 'human_approved': True,
                 'approval_note': 'Isolated software test: no scientific worker execution'}
    scope = approval_scope(case[0], DESIGN, arguments)
    proof = signed({'tool': DESIGN, 'arguments_json': json.dumps(arguments), 'scope': scope,
        'session': 'fixture', 'message': 'fixture', 'approval_request_id': 'per_fixture_covdock',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**arguments, '_native_approval': proof}


def test_exact_native_task_scope_recovery_and_completion_without_scientific_acceptance(ready):
    case, launches, folder, manager, receptor_run = ready
    state = json.loads(case[2].read_text())
    request = request_for(case[0], state)
    execute = request.model_copy(update={'execution_mode': 'execute', 'human_approved': True})
    with pytest.raises(ValueError, match='approval'):
        JobManager(case[0]).submit(execute)
    with pytest.raises(ValueError, match='approval'):
        dispatch(case[0], DESIGN, {**args(case, 'start_covdock'), 'human_approved': True})
    changed = request.model_copy(update={'parameters': {**request.parameters, 'final_n': 999}})
    with pytest.raises(ValueError, match='bound workflow'):
        JobManager(case[0]).build_spec(changed)
    proof = approval(case)
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, {**proof, 'resource_budget': {'gpu_count': 1}})
    config = case[0] / 'configs/local.inspire.yaml'
    original = config.read_bytes()
    config.write_bytes(original + b'\n')
    with pytest.raises(ValueError, match='changed'):
        dispatch(case[0], DESIGN, proof)
    config.write_bytes(original)
    response = dispatch(case[0], DESIGN, proof)
    record = response['covdock_execution']
    run = record['run_id']
    assert record['approval'] == 'APPROVED'
    assert response['workflow']['stage'] == 'COVDOCK_EXECUTION_RUNNING'
    assert response['execution']['task_ids'] == [run]
    assert response['workflow']['tools'][DESIGN]['actions'] == ['covdock_status']
    assert response['workflow']['tools'][ENTRY]['actions'] == ['status', 'suspend']
    assert len(launches) == 2 and len(jobs(case)) == 3
    resumed = navigate(case[0], 'resume', workflow_id=case[1])
    assert resumed['covdock_execution']['run_id'] == run
    assert resumed['workflow']['stage'] == 'COVDOCK_EXECUTION_RUNNING'
    with pytest.raises(ValueError):
        dispatch(case[0], DESIGN, proof)
    with pytest.raises(ValueError, match='execution'):
        navigate(case[0], 'reconsider', workflow_id=case[1], expected_revision=resumed['workflow']['revision'],
                 reason='Cannot replace a registered active task')
    # Malformed signed spec or mismatched result identity cannot be accepted.
    for name in ('spec.json', 'result.json'):
        path = manager.registry.job_dir(run) / name
        raw = path.read_bytes()
        payload = json.loads(raw)
        payload['project_id'] = 'wrong-study'
        path.write_text(json.dumps(payload))
        with pytest.raises(ValueError):
            dispatch(case[0], DESIGN, args(case, 'covdock_status'))
        failed = navigate(case[0], 'status', workflow_id=case[1])
        assert failed['covdock_execution']['run_id'] == run
        assert failed['assessment']['state'] == 'evidence_insufficient'
        path.write_bytes(raw)
        assert dispatch(case[0], DESIGN, args(case, 'covdock_status'))['covdock_execution']['run_id'] == run
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'COMPLETED', 0  # Controlled ledger result; no molecular outputs.
    manager.registry.write_result(result)
    response = dispatch(case[0], DESIGN, args(case, 'covdock_status'))
    assert response['workflow']['stage'] == 'COVDOCK_EXECUTION_REVIEW'
    assert response['execution']['state'] == 'completed'
    assert response['assessment']['state'] == 'not_evaluated'
    assert not response['result']['md_ready'] and not response['result']['scientific_result_verified']
    path = case[0] / response['refinement_input']['environment_review']['path']
    path.write_bytes(path.read_bytes() + b'\n')
    rejected = navigate(case[0], 'status', workflow_id=case[1])
    assert rejected['covdock_execution']['run_id'] == run
    assert rejected['assessment']['state'] == 'evidence_insufficient'
    assert rejected['refinement_input']['receptor_preparation']['run_id'] == receptor_run
    assert rejected['workflow']['tools'][DESIGN]['actions'] == ['covdock_status']
    assert len(launches) == 2 and len(jobs(case)) == 3


@pytest.mark.parametrize('registered', [False, True])
def test_lost_receipt_reconciles_or_preserves_unknown_without_retry(ready, monkeypatch, registered):
    case, launches, folder, manager, receptor_run = ready
    def lost(manager, request):
        if registered:
            SUBMIT(manager, request)
        raise RuntimeError('Controlled transport failure')
    monkeypatch.setattr(JobManager, 'submit', lost)
    with pytest.raises(RuntimeError):
        dispatch(case[0], DESIGN, approval(case))
    state = json.loads(case[2].read_text())
    assert state['pending']['action'] == 'start_covdock'
    if registered:
        run = next(value for value in jobs(case) if 'schrodinger-covdock' in value)
        path = manager.registry.job_dir(run) / 'spec.json'
        original = path.read_bytes()
        spec = json.loads(original)
        spec['resource_budget']['cpu_count'] = 2  # Still parseable, but no longer correctly signed.
        path.write_text(json.dumps(spec))
        with pytest.raises(ValueError, match='Approved job changed'):
            navigate(case[0], 'reconcile', workflow_id=case[1], expected_revision=state['revision'])
        uncertain = navigate(case[0], 'status', workflow_id=case[1])
        assert uncertain['covdock_execution']['run_id'] == run
        assert uncertain['workflow']['stage'] == 'COVDOCK_EXECUTION_RUNNING'
        assert uncertain['execution']['state'] == 'outcome_unknown'
        assert 'reconsider' not in uncertain['workflow']['tools'][ENTRY]['actions']
        with pytest.raises(ValueError, match='execution'):
            navigate(case[0], 'reconsider', workflow_id=case[1], expected_revision=uncertain['workflow']['revision'],
                     reason='Unverified registration is not a completed task')
        path.write_bytes(original)
        response = dispatch(case[0], DESIGN, args(case, 'covdock_status'))
        assert response['pending'] is None
        assert response['covdock_execution']['run_id'] in jobs(case)
        assert response['workflow']['stage'] == 'COVDOCK_EXECUTION_RUNNING'
    else:
        response = navigate(case[0], 'reconcile', workflow_id=case[1], expected_revision=state['revision'])
        assert response['pending']['action'] == 'start_covdock'
        assert response['execution']['state'] == 'outcome_unknown'
        assert response['workflow']['tools'][ENTRY]['actions'] == ['status', 'reconcile']
    assert len(launches) == 1 + int(registered)
    assert len(jobs(case)) == 2 + int(registered)
