"""Real native signing, ledger registration and recovery; external worker never runs."""
import json
import time
from uuid import uuid4

import pytest

from covscreen.approval_scope import approval_scope
from covscreen.docking_policy import POLICY
from covscreen.execution import JobManager
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed, needs_approval
from test_vs_input_workflow import study, design, call, configure, assessment


@pytest.fixture
def planned(study, monkeypatch):
    root, path = study
    configure(root)
    output = call(study, 'prepare_vs_input')
    call(study, 'review_vs_environment', environment_review=assessment(output, 'suitable_for_exploratory_vs'))
    call(study, 'check_vs_runtime')
    secret = root/'fixture-approval.key'
    secret.write_bytes(b'software-only-approval-key-not-a-real-key'*2)
    secret.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(secret))
    # Exercise the not-yet-released native boundary without changing production
    # policy or calling any scheduler/worker/vendor placeholder.
    monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', True)
    return study


def arguments(study):
    state = json.loads(study[1].read_text())
    return {'action':'start_vs', 'workflow_id':state['workflow_id'], 'expected_revision':state['revision'],
        'human_approved':True, 'approval_note':'Software-only registry test; no scientific execution'}


def approval(study):
    values = arguments(study)
    scope = approval_scope(study[0], DESIGN, values)
    proof = signed({'tool':DESIGN, 'arguments_json':json.dumps(values), 'scope':scope,
        'session':'fixture', 'message':'fixture', 'approval_request_id':'permission_fixture',
        'nonce':uuid4().hex, 'expires':time.time()+300})
    return {**values, '_native_approval':proof}


def test_boolean_does_not_authorize_vs(planned):
    assert needs_approval(DESIGN, {'action':'start_vs'})
    assert not needs_approval(DESIGN, {'action':'check_vs_runtime'})
    with pytest.raises(ValueError, match='approval'):
        dispatch(planned[0], DESIGN, arguments(planned))
    assert not list(planned[0].glob('state/jobs/*/spec.json'))


def test_native_signed_registration_preserves_private_plan_and_never_launches(planned):
    root, path = planned
    output = dispatch(root, DESIGN, approval(planned))
    record = output['vs_execution']
    assert record['job_state'] == 'QUEUED' and record['job_identity_verified']
    assert output['assessment']['scope'] == 'covdock_vs_structures'
    assert output['assessment']['state'] == 'not_evaluated'
    manager = JobManager(root)
    run = record['run_id']
    assert manager.registry.load_spec(run).engine == 'schrodinger-covdock-vs'
    assert not (manager.registry.job_dir(run)/'external_hpc.json').exists()
    assert not (manager.registry.job_dir(run)/'vs_worker_claim').exists()
    assert str(root/'vendor') not in json.dumps(output)
    assert 'approved_scope' not in record
    observed = call(planned, 'vs_status')
    assert observed['vs_execution']['run_id'] == run
    assert len(list(root.glob('state/jobs/*/spec.json'))) == 1
    with pytest.raises(ValueError):
        approval_scope(root, DESIGN, arguments(planned))


def test_lost_registration_response_recovers_original_job_without_replay(planned, monkeypatch):
    root, path = planned
    original = JobManager.submit
    calls = []
    def interrupted(manager, request):
        result = original(manager, request)
        calls.append(result.run_id)
        raise OSError('Controlled lost registration response')
    monkeypatch.setattr(JobManager, 'submit', interrupted)
    with pytest.raises(OSError, match='lost registration'):
        dispatch(root, DESIGN, approval(planned))
    state = json.loads(path.read_text())
    assert state['pending']['action'] == 'start_vs'
    result = navigate(root, 'reconcile', workflow_id=state['workflow_id'], expected_revision=state['revision'])
    assert result['vs_execution']['run_id'] == calls[0]
    assert result['pending'] is None
    assert len(calls) == 1 and len(list(root.glob('state/jobs/*/spec.json'))) == 1


def test_changed_private_plan_cannot_receive_native_approval(planned):
    root, path = planned
    state = json.loads(path.read_text())
    (root/state['vs_plan']['path']).write_text('{}')
    with pytest.raises(ValueError, match='private plan changed'):
        approval(planned)
    assert not list(root.glob('state/jobs/*/spec.json'))


def test_release_policy_still_disables_execution_even_with_positive_review(planned, monkeypatch):
    monkeypatch.setitem(POLICY, 'native_vs_workflow_ready', False)
    with pytest.raises(ValueError, match='migration is not yet complete'):
        approval(planned)


def test_completed_task_status_does_not_grant_scientific_acceptance(planned):
    root, path = planned
    output = dispatch(root, DESIGN, approval(planned))
    manager = JobManager(root)
    run = output['vs_execution']['run_id']
    result = manager.registry.load_result(run)
    result.state, result.return_code = 'COMPLETED', 0
    manager.registry.write_result(result)
    observed = call(planned, 'vs_status')
    assert observed['workflow']['stage'] == 'VS_EXECUTION_REVIEW'
    assert observed['vs_execution']['output_validation'] == 'NOT_EVALUATED'
    assert not observed['vs_execution']['scientific_acceptance'] and not observed['vs_execution']['md_ready']
