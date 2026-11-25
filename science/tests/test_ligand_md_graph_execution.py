"""Real MCP/approval/ledger over synthetic upstream reviews and graph outputs.

These focused tests replace the already-tested upstream CovDock revalidation
boundary only. The full route integration separately uses actual revalidation.
No scientific worker or native vendor engine is run.
"""
import json
import time
from uuid import uuid4

import pytest
from covscreen.approval_scope import approval_scope
from covscreen.execution import JobManager
from covscreen.ligand_workflow import DESIGN, ENTRY, start, directory, navigate
from covscreen.ligand_md_selection import graph_request, plan_graph, release, select
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed, needs_approval
from covscreen.workflow_store import load, save
from test_ligand_md_selection import state as candidate_state, choose, runtime
from test_md_review_delivery import case
from test_workflow_md_execution import register, completed_graph_output


def arguments(fixture, action, **values):
    root, folder = fixture
    state = load(folder)
    return {'action': action, 'workflow_id': state['workflow_id'], 'expected_revision': state['revision'], **values}


def call(fixture, action, **values):
    return dispatch(fixture[0], ENTRY if action == 'reconcile' else DESIGN, arguments(fixture, action, **values))


def approve(fixture):
    values = arguments(fixture, 'start_md_graph', human_approved=True,
                       approval_note='Isolated artificial graph lifecycle test; no scientific worker')
    scope = approval_scope(fixture[0], DESIGN, values)
    proof = signed({'tool': DESIGN, 'arguments_json': json.dumps(values), 'scope': scope,
        'session': 'software-fixture', 'message': 'software-fixture', 'approval_request_id': 'per_graph_fixture',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**values, '_native_approval': proof}


@pytest.fixture
def graph(tmp_path, candidate_state, monkeypatch):
    key = tmp_path / 'isolated-test.key'
    key.write_text('isolated-graph-lifecycle-key-not-for-production-12345')
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    initial = start(tmp_path, request_id='graph-fixture', target='EGFR', site='C797', taxon=9606)
    folder = directory(tmp_path, initial['workflow']['workflow_id'])
    state = load(folder)
    state.update(design_id=candidate_state['design_id'], covdock_execution=candidate_state['covdock_execution'],
                 stage='COVDOCK_EXECUTION_REVIEW', evidence_id='artificial-parent-evidence')
    parent = state['covdock_execution']
    parent.update(job_state='COMPLETED', return_code=0,
        evaluation={'assessment_scope': 'covdock_registered_outputs', 'assessment': 'review_required',
                    'measured_candidate_count': 1})
    parent['md_handoff']['artifact_id'] = 'artificial-shared-delivery'
    choose(tmp_path, state)
    runtime(tmp_path)
    plan_graph(tmp_path, state)
    save(folder, state)
    monkeypatch.setattr('covscreen.ligand_covdock_execution.revalidate_saved', lambda root, state: None)
    monkeypatch.setattr(JobManager, 'submit', register)
    return tmp_path, folder


def test_bound_plan_native_approval_and_no_permission_from_boolean(graph):
    root, folder = graph
    state = load(folder)
    assert needs_approval(DESIGN, {'action': 'start_md_graph'})
    assert not needs_approval(DESIGN, {'action': 'md_graph_status'})
    with pytest.raises(ValueError, match='approval'):
        call(graph, 'start_md_graph', human_approved=True)
    request = graph_request(root, state).model_copy(update={'execution_mode': 'execute',
        'human_approved': True, 'approval_note': 'Software test, no actual engine'})
    with pytest.raises(ValueError, match='approval'):
        JobManager(root)._prepare_submission(request)
    with pytest.raises(ValueError, match='bound workflow'):
        JobManager(root).build_spec(request.model_copy(update={'resource_budget': request.resource_budget.model_copy(update={'gpu_count': 1})}))
    proof = approve(graph)
    config = root / 'configs/local.inspire.yaml'
    original = config.read_bytes()
    config.write_bytes(original + b'\n')
    with pytest.raises(ValueError, match='changed'):
        dispatch(root, DESIGN, proof)
    config.write_bytes(original)
    observed = dispatch(root, DESIGN, proof)
    run = observed['md_graph_execution']['run_id']
    assert observed['workflow']['stage'] == 'MD_GRAPH_RUNNING'
    assert observed['execution']['task_ids'] == [run]
    assert observed['workflow']['tools'][DESIGN]['actions'] == ['md_graph_status']
    assert observed['workflow']['tools'][ENTRY]['actions'] == ['status', 'suspend']
    assert observed['md_admission']['state'] == 'not_evaluated'
    assert observed['compute_submitted'] is True
    with pytest.raises(ValueError):
        dispatch(root, DESIGN, proof)
    for action in ('release_md_candidate', 'check_md_graph_runtime'):
        with pytest.raises(ValueError):
            call(graph, action, **({'reason': 'Cannot replace registered candidate'} if action.startswith('release') else {}))
    with pytest.raises(ValueError, match='execution'):
        navigate(root, 'reconsider', workflow_id=state['workflow_id'], expected_revision=load(folder)['revision'],
                 reason='Cannot replace a running graph task')


def test_generic_submit_approval_cannot_bypass_workflow_pending_record(graph):
    root, folder = graph
    request = graph_request(root, load(folder)).model_copy(update={'execution_mode': 'execute',
        'human_approved': True, 'approval_note': 'Isolated generic-call refusal test'})
    values = request.model_dump(mode='json')
    scope = approval_scope(root, 'covalent_submit_job', values)
    proof = signed({'tool': 'covalent_submit_job', 'arguments_json': json.dumps(values), 'scope': scope,
        'session': 'software-fixture', 'message': 'software-fixture', 'approval_request_id': 'per_generic_fixture',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    with pytest.raises(ValueError, match='pending workflow approval'):
        dispatch(root, 'covalent_submit_job', {**values, '_native_approval': proof})
    assert not load(folder).get('md_graph_execution')
    assert not list((root / 'state/jobs').glob('*/spec.json'))
    # An authorized workflow request remains possible after the refused bypass.
    assert dispatch(root, DESIGN, approve(graph))['md_graph_execution']['job_identity_verified']


def test_old_frozen_plan_is_retained_and_never_silently_upgraded(graph):
    state = load(graph[1])
    plan = state['covdock_execution']['md_graph_plan']
    plan['spec']['parameters'].pop('workflow_id')
    save(graph[1], state)
    with pytest.raises(ValueError, match='Graph plan changed'):
        approve(graph)
    assert load(graph[1])['covdock_execution']['md_graph_plan'] == plan
    assert not load(graph[1]).get('md_graph_execution')


@pytest.mark.parametrize('registered', [True, False])
def test_submission_interruption_reconciles_only_existing_registration(graph, monkeypatch, registered):
    def interrupt(manager, request):
        if registered:
            register(manager, request)
        raise ConnectionError('Interrupted graph registration reply')
    monkeypatch.setattr(JobManager, 'submit', interrupt)
    with pytest.raises(ConnectionError):
        dispatch(graph[0], DESIGN, approve(graph))
    before = load(graph[1])
    assert before['pending']['action'] == 'start_md_graph'
    observed = call(graph, 'reconcile')
    if registered:
        assert observed['pending'] is None
        run = observed['md_graph_execution']['run_id']
        assert call(graph, 'md_graph_status')['md_graph_execution']['run_id'] == run
        assert len(list((graph[0] / 'state/jobs').iterdir())) == 1
    else:
        assert observed['pending'] == before['pending']
        assert observed['execution']['state'] == 'outcome_unknown'
        assert DESIGN not in observed['workflow']['tools']
        assert observed['result']['submission_state'] == 'NO_RECEIPT'
    with pytest.raises(ValueError):
        release(load(graph[1]), 'Cannot release uncertain or registered task')


@pytest.mark.parametrize('corruption', ['input', 'spec', 'missing_output', 'failed', 'identity'])
def test_unverified_evidence_never_becomes_md_readiness(graph, corruption):
    observed = dispatch(graph[0], DESIGN, approve(graph))
    run = observed['md_graph_execution']['run_id']
    manager = JobManager(graph[0])
    state = load(graph[1])
    if corruption == 'input':
        selected = state['md_graph_execution']['selection']
        path = graph[0] / 'state/candidate_pose_input' / selected['artifact_id'][21:] / 'covdock-source.maegz'
        path.write_bytes(b'Changed source')
    elif corruption == 'spec':
        spec = manager.registry.load_spec(run)
        spec.request_id = 'different-task'
        (manager.registry.job_dir(run) / 'spec.json').write_text(spec.model_dump_json())
    else:
        result = manager.registry.load_result(run)
        result.state = 'FAILED' if corruption == 'failed' else 'COMPLETED'
        result.return_code = 1 if corruption == 'failed' else 0
        if corruption == 'identity':
            result.project_id = 'different-study'
        manager.registry.write_result(result)
    response = navigate(graph[0], 'resume', workflow_id=state['workflow_id'])
    record = response['md_graph_execution']
    assert record['run_id'] == run
    assert record['admission'] is None and not record['native_graph_qc_verified']
    assert not record['md_system_ready']
    assert not any(item['kind'] == 'native_graph_admission_receipt' for item in response['artifacts'])
    assert response['md_admission']['state'] == 'not_evaluated'
    if corruption in {'identity', 'spec'}:
        assert response['execution']['state'] == 'outcome_unknown'
        assert 'reconsider' not in response['workflow']['tools'][ENTRY]['actions']


def test_completed_graph_replays_then_withdraws_and_restores_same_receipt(graph):
    response = dispatch(graph[0], DESIGN, approve(graph))
    run = response['md_graph_execution']['run_id']
    state = load(graph[1])
    folder = completed_graph_output(graph[0], state['md_graph_execution']['selection'], run)
    response = call(graph, 'md_graph_status')
    receipt = response['md_graph_execution']['admission']
    assert receipt['native_graph_qc_completed'] and not receipt['md_system_ready']
    assert response['workflow']['stage'] == 'MD_GRAPH_REVIEW'
    assert response['assessment']['scope'] == 'native_covalent_graph_check'
    assert any(item['kind'] == 'native_graph_admission_receipt' for item in response['artifacts'])
    path = folder / 'md-adduct-admission.json'
    original = path.read_bytes()
    path.write_bytes(original + b'\n')
    with pytest.raises(ValueError):
        call(graph, 'md_graph_status')
    response = navigate(graph[0], 'resume', workflow_id=state['workflow_id'])
    assert response['md_graph_execution']['admission'] is None
    assert response['md_graph_execution']['historical_admission'] == receipt
    path.write_bytes(original)
    response = call(graph, 'md_graph_status')
    assert response['md_graph_execution']['admission'] == receipt
    assert len(list((graph[0] / 'state/jobs').iterdir())) == 1


def test_upstream_delivery_withdrawal_replays_original_witness(graph, monkeypatch):
    dispatch(graph[0], DESIGN, approve(graph))
    state = load(graph[1])
    original = state['covdock_execution']['md_handoff']
    state['covdock_execution'].pop('md_handoff')
    save(graph[1], state)
    def review(root, state):
        assert state['covdock_execution']['md_handoff'] == original
        return None
    monkeypatch.setattr('covscreen.ligand_covdock_execution.revalidate_saved', review)
    response = call(graph, 'md_graph_status')
    assert response['covdock_execution']['md_handoff'] == original
    assert response['md_graph_execution']['verification'] == 'not_ready'
