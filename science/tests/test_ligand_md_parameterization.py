"""Native protocol and artifact verification over explicit software fixtures.

Only the upstream scientific boundary and worker are replaced. No vendor engine
or production approval key is used; passing these tests is not real acceptance.
"""
import json
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from covscreen.approval_scope import approval_scope
from covscreen.execution import JobManager
from covscreen.ligand_workflow import DESIGN, ENTRY, navigate
from covscreen.ligand_md_parameterization import request
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed, needs_approval
from covscreen.workflow_store import load
from test_ligand_md_graph_execution import graph, candidate_state, case, arguments, call, approve as graph_approve
from test_workflow_md_execution import register, completed_graph_output
from test_workflow_md_parameterization import parameterized


@pytest.fixture
def admitted(graph):
    result = dispatch(graph[0], DESIGN, graph_approve(graph))
    run = result['md_graph_execution']['run_id']
    state = load(graph[1])
    completed_graph_output(graph[0], state['md_graph_execution']['selection'], run)
    result = call(graph, 'md_graph_status')
    assert 'plan_md_parameterization' in result['workflow']['tools'][DESIGN]['actions']
    return graph


def approve(fixture):
    values = arguments(fixture, 'start_md_parameterization', human_approved=True,
                       approval_note='Isolated software fixture; no scientific engine execution')
    scope = approval_scope(fixture[0], DESIGN, values)
    proof = signed({'tool': DESIGN, 'arguments_json': json.dumps(values), 'scope': scope,
        'session': 'software-fixture', 'message': 'software-fixture', 'approval_request_id': 'per_parameter_fixture',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**values, '_native_approval': proof}


def complete(fixture):
    call(fixture, 'plan_md_parameterization')
    started = dispatch(fixture[0], DESIGN, approve(fixture))
    run = started['md_parameterization']['run_id']
    parent = load(fixture[1])['md_parameterization']['input']
    output = parameterized(fixture, run, parent=parent)
    final = call(fixture, 'md_parameterization_status')
    assert final['md_parameterization']['verification'] == 'verified'
    return run, output, final


def test_graph_required_and_plan_never_submits(admitted):
    state = load(admitted[1])
    previous = deepcopy(state['md_graph_execution'])
    planned = call(admitted, 'plan_md_parameterization')
    state = load(admitted[1])
    spec = state['md_parameterization']['plan']['spec']
    assert planned['workflow']['stage'] == 'MD_PARAMETERIZATION_PLAN_READY'
    assert spec['parent_run_id'] == previous['run_id']
    assert spec['project_id'] == state['design_id']
    assert spec['resource_budget']['gpu_count'] == 0
    assert state['md_graph_execution'] == previous
    assert len(list((admitted[0] / 'state/jobs').iterdir())) == 1
    assert needs_approval(DESIGN, {'action': 'start_md_parameterization'})
    assert not needs_approval(DESIGN, {'action': 'plan_md_parameterization'})
    with pytest.raises(ValueError, match='approval'):
        call(admitted, 'start_md_parameterization', human_approved=True)
    raw = request(admitted[0], state).model_copy(update={'execution_mode': 'execute', 'human_approved': True})
    with pytest.raises(ValueError, match='approval'):
        JobManager(admitted[0])._prepare_submission(raw)
    with pytest.raises(ValueError, match='bound workflow'):
        JobManager(admitted[0]).build_spec(raw.model_copy(update={
            'resource_budget': raw.resource_budget.model_copy(update={'gpu_count': 1})}))


def test_cannot_plan_before_graph_completion(graph):
    with pytest.raises(ValueError, match='graph review'):
        call(graph, 'plan_md_parameterization')
    assert not load(graph[1]).get('md_parameterization')


def test_frozen_plan_change_requires_release(admitted):
    call(admitted, 'plan_md_parameterization')
    proof = approve(admitted)
    path = admitted[0] / 'configs/local.inspire.yaml'
    original = path.read_bytes()
    path.write_bytes(original + b'\n')
    with pytest.raises(ValueError, match='changed'):
        dispatch(admitted[0], DESIGN, proof)
    assert not load(admitted[1]).get('pending')
    path.write_bytes(original)
    result = call(admitted, 'release_md_parameterization_plan')
    assert result['workflow']['stage'] == 'MD_GRAPH_REVIEW'
    assert len(load(admitted[1])['md_parameterization_plan_history']) == 1


@pytest.mark.parametrize('registered', [True, False])
def test_interrupted_submission_never_resubmits(admitted, monkeypatch, registered):
    call(admitted, 'plan_md_parameterization')
    count = []
    def interrupted(manager, value):
        count.append(value.request_id)
        if registered:
            register(manager, value)
        raise ConnectionError('Dropped worker registration reply')
    monkeypatch.setattr(JobManager, 'submit', interrupted)
    with pytest.raises(ConnectionError):
        dispatch(admitted[0], DESIGN, approve(admitted))
    before = load(admitted[1])
    assert before['pending']['action'] == 'start_md_parameterization'
    result = call(admitted, 'reconcile')
    assert len(count) == 1
    assert len(list((admitted[0] / 'state/jobs').iterdir())) == (2 if registered else 1)
    if registered:
        assert result['pending'] is None
        run = result['md_parameterization']['run_id']
        assert call(admitted, 'md_parameterization_status')['md_parameterization']['run_id'] == run
        assert result['execution']['state'] == 'submitted'
    else:
        assert result['pending'] == before['pending']
        assert result['execution']['state'] == 'outcome_unknown'
        assert DESIGN not in result['workflow']['tools']
    with pytest.raises(ValueError):
        call(admitted, 'release_md_parameterization_plan')
    with pytest.raises(ValueError):
        navigate(admitted[0], 'reconsider', workflow_id=before['workflow_id'],
                 expected_revision=load(admitted[1])['revision'], reason='Must keep original task')


def test_completion_reopens_same_candidate_without_md_readiness(admitted):
    run, _, result = complete(admitted)
    record = result['md_parameterization']
    assert record['parameterization_completed'] and not record['md_system_ready']
    assert not record['simulation_started']
    assert record['parameterization']['candidate_id'] == record['input']['candidate_id']
    assert result['assessment']['scope'] == 'native_covalent_parameterization'
    assert result['execution']['task_ids'] == [run]
    assert any(item['kind'] == 'native_parameterization_receipt' for item in result['artifacts'])
    reopened = navigate(admitted[0], 'resume', workflow_id=result['workflow']['workflow_id'])
    assert reopened['md_parameterization']['parameterization'] == record['parameterization']
    assert reopened['workflow']['stage'] == 'MD_PARAMETERIZATION_REVIEW'
    assert len(list((admitted[0] / 'state/jobs').iterdir())) == 2


@pytest.mark.parametrize('filename', ['parameterization-audit.json', 'finished.json', 'parameterized.cms',
                                    'atom-mapping.json', 'input.mae', 'parent'])
def test_changed_evidence_withdraws_receipt_and_can_restore_same_job(admitted, filename):
    run, output, result = complete(admitted)
    witness = result['md_parameterization']['parameterization']
    parent = result['md_parameterization']['input']['run_id']
    path = (JobManager(admitted[0]).registry.job_dir(parent) / 'artifacts/md-adduct-admission/adduct-input/adduct.mae'
            if filename == 'parent' else output / filename)
    original = path.read_bytes()
    path.write_bytes(original + b'\nchanged')
    reopened = navigate(admitted[0], 'resume', workflow_id=result['workflow']['workflow_id'])
    record = reopened['md_parameterization']
    assert record['run_id'] == run and record['parameterization'] is None
    assert record['historical_parameterization'] == witness
    assert record['verification'] == 'evidence_rejected'
    assert not any(item['kind'] == 'native_parameterization_receipt' for item in reopened['artifacts'])
    path.write_bytes(original)
    restored = call(admitted, 'md_parameterization_status')
    assert restored['md_parameterization']['parameterization'] == witness


def test_failure_reason_is_read_back(admitted):
    call(admitted, 'plan_md_parameterization')
    result = dispatch(admitted[0], DESIGN, approve(admitted))
    run = result['md_parameterization']['run_id']
    manager = JobManager(admitted[0])
    terminal = manager.registry.load_result(run)
    terminal.state = 'FAILED'
    terminal.return_code = 1
    terminal.failure_reason = 'software_fixture_failure'
    terminal.message = 'Native parameterization did not produce a complete system'
    manager.registry.write_result(terminal)
    result = call(admitted, 'md_parameterization_status')
    assert result['execution']['state'] == 'failed'
    assert result['md_parameterization']['failure_reason'] == 'software_fixture_failure'
    assert not result['md_parameterization']['parameterization_completed']


def test_completed_contract_is_consumable_by_system_preparation_plan(admitted):
    from covscreen.tool_api import _openmm_system_preparation_request
    run, _, result = complete(admitted)
    path = admitted[0] / 'configs/local.inspire.yaml'
    config = json.loads(path.read_text())
    workspace = Path(__file__).resolve().parents[2]
    config['openmm_system_preparation'] = {'enabled': True,
        'python': str(workspace / '.tooling/md-interoperability-20261002/bin/python'),
        'pythonpath': str(workspace / '.tooling/md-interoperability-20261002'), 'intermol_source': 'bundled'}
    path.write_text(json.dumps(config))
    witness = result['md_parameterization']['parameterization']
    value = _openmm_system_preparation_request(admitted[0], {'parameterization_run_id': run,
        'parameterization_audit_sha256': witness['manifest_sha256']}, execution_mode='plan')
    spec = JobManager(admitted[0])._prepare_submission(value)
    assert spec.parent_run_id == run
    assert spec.parameters['system_id'] == witness['candidate_id']
    assert len(list((admitted[0] / 'state/jobs').iterdir())) == 2


def test_generic_submit_approval_cannot_bypass_workflow(admitted):
    call(admitted, 'plan_md_parameterization')
    value = request(admitted[0], load(admitted[1])).model_copy(update={
        'execution_mode': 'execute', 'human_approved': True, 'approval_note': 'Isolated refusal test'})
    values = value.model_dump(mode='json')
    scope = approval_scope(admitted[0], 'covalent_submit_job', values)
    proof = signed({'tool': 'covalent_submit_job', 'arguments_json': json.dumps(values), 'scope': scope,
        'session': 'software-fixture', 'message': 'software-fixture', 'approval_request_id': 'per_generic_fixture',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    with pytest.raises(ValueError, match='pending workflow approval'):
        dispatch(admitted[0], 'covalent_submit_job', {**values, '_native_approval': proof})
    assert len(list((admitted[0] / 'state/jobs').iterdir())) == 1


def test_runtime_absence_keeps_plan_unsubmitted(admitted):
    path = admitted[0] / 'runtime/run'
    original = path.read_bytes()
    path.unlink()
    result = call(admitted, 'plan_md_parameterization')
    assert result['workflow']['stage'] == 'MD_PARAMETERIZATION_RUNTIME_REQUIRED'
    assert 'start_md_parameterization' not in result['workflow']['tools'][DESIGN]['actions']
    assert len(list((admitted[0] / 'state/jobs').iterdir())) == 1
    path.write_bytes(original)
    path.chmod(0o700)
    assert call(admitted, 'plan_md_parameterization')['workflow']['stage'] == 'MD_PARAMETERIZATION_PLAN_READY'


def test_parameterization_does_not_use_covdock_only_hpc_dispatcher(admitted):
    path = admitted[0] / 'configs/local.inspire.yaml'
    config = json.loads(path.read_text())
    config['runtime'] = {'worker_backend': 'external-hpc'}
    path.write_text(json.dumps(config))
    planned = call(admitted, 'plan_md_parameterization')
    assert planned['md_parameterization']['plan']['spec']['provenance']['worker_backend'] == 'local'
