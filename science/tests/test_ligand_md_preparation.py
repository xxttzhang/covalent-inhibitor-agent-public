"""Actual approval, registry and evidence readers; explicit artificial science files.

Workers are replaced by task registration. These cases do not establish molecular
validity or count as a real system-preparation/MD acceptance.
"""
import json
import time
from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from covscreen.approval_scope import approval_scope
from covscreen.execution import JobManager
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.ligand_md_preparation import request
from covscreen.mcp.server import dispatch
from covscreen.native_approval import signed, needs_approval
from covscreen.workflow_store import load, save
from test_ligand_md_parameterization import admitted, complete
from test_ligand_md_graph_execution import graph, candidate_state, case, arguments, call
from test_workflow_md_execution import register
from test_workflow_md_system_preparation import prepared
from test_workflow_md_short_qc import completed
from test_workflow_md_equilibration import completed as equilibrated


def approve(fixture, name, tool=DESIGN):
    values = arguments(fixture, 'start_md_' + name, human_approved=True,
                       approval_note='Isolated software fixture; no scientific engine execution')
    if tool != DESIGN:
        values = request(fixture[0], load(fixture[1]), name).model_copy(update={
            'execution_mode': 'execute', 'human_approved': True, 'approval_note': 'Isolated generic dispatch refusal test'}).model_dump(mode='json')
    scope = approval_scope(fixture[0], tool, values)
    proof = signed({'tool': tool, 'arguments_json': json.dumps(values), 'scope': scope,
        'session': 'software-fixture', 'message': 'software-fixture', 'approval_request_id': 'per_md_fixture',
        'nonce': uuid4().hex, 'expires': time.time() + 300})
    return {**values, '_native_approval': proof}


def finish(fixture, name):
    call(fixture, 'plan_md_' + name)
    result = dispatch(fixture[0], DESIGN, approve(fixture, name))
    record = result['md_' + name]
    output = {'system_preparation': prepared, 'short_qc': completed, 'equilibration': equilibrated}[name](fixture, record['run_id'], parent=record['input'])
    result = call(fixture, 'md_' + name + '_status')
    assert result['md_' + name]['verification'] == 'verified'
    return output, result


@pytest.fixture(params=['system_preparation', 'short_qc', 'equilibration'])
def ready(admitted, request):
    complete(admitted)
    path = admitted[0] / 'configs/local.inspire.yaml'
    config = json.loads(path.read_text())
    workspace = Path(__file__).resolve().parents[2]
    config['openmm_system_preparation'] = {'enabled': True,
        'python': str(workspace / '.tooling/md-interoperability-20261002/bin/python'),
        'pythonpath': str(workspace / '.tooling/md-interoperability-20261002'), 'intermol_source': 'bundled'}
    config['openmm_equilibration'] = {**config['openmm_system_preparation'], 'platform': 'CPU', 'platform_properties': {'Threads': '2'}}
    path.write_text(json.dumps(config))
    if request.param in {'short_qc', 'equilibration'}:
        finish(admitted, 'system_preparation')
    if request.param == 'equilibration':
        finish(admitted, 'short_qc')
    return admitted, request.param


def test_frozen_parent_separate_approval_and_release(ready):
    fixture, name = ready
    before = load(fixture[1])
    count = len(list((fixture[0] / 'state/jobs').iterdir()))
    planned = call(fixture, 'plan_md_' + name)
    record = planned['md_' + name]
    assert planned['workflow']['stage'] == f'MD_{name.upper()}_PLAN_READY'
    assert record['plan']['spec']['parent_run_id'] == record['input']['run_id']
    assert record['selection'] == before['md_parameterization']['selection']
    assert record['plan']['spec']['resource_budget']['gpu_count'] == 0
    assert len(list((fixture[0] / 'state/jobs').iterdir())) == count
    assert needs_approval(DESIGN, {'action': 'start_md_' + name})
    with pytest.raises(ValueError, match='approval'):
        call(fixture, 'start_md_' + name, human_approved=True)
    with pytest.raises(ValueError, match='pending workflow approval'):
        dispatch(fixture[0], 'covalent_submit_job', approve(fixture, name, 'covalent_submit_job'))
    released = call(fixture, f'release_md_{name}_plan')
    assert released['workflow']['stage'] == before['stage']
    assert len(load(fixture[1])['md_preparation_plan_history']) == 1


def test_exact_budget_protocol_and_config_cannot_change(ready):
    fixture, name = ready
    call(fixture, 'plan_md_' + name)
    proof = approve(fixture, name)
    value = request(fixture[0], load(fixture[1]), name).model_copy(update={'execution_mode': 'execute'})
    with pytest.raises(ValueError, match='bound workflow'):
        JobManager(fixture[0]).build_spec(value.model_copy(update={
            'resource_budget': value.resource_budget.model_copy(update={'gpu_count': 1})}))
    path = fixture[0] / 'configs/local.inspire.yaml'
    path.write_bytes(path.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='changed'):
        dispatch(fixture[0], DESIGN, proof)
    assert not load(fixture[1]).get('pending')


@pytest.mark.parametrize('registered', [True, False])
def test_lost_submission_is_reconciled_without_duplicate(ready, monkeypatch, registered):
    fixture, name = ready
    call(fixture, 'plan_md_' + name)
    count = []
    def interrupted(manager, value):
        count.append(value.request_id)
        if registered:
            register(manager, value)
        raise ConnectionError('Lost registration response')
    monkeypatch.setattr(JobManager, 'submit', interrupted)
    with pytest.raises(ConnectionError):
        dispatch(fixture[0], DESIGN, approve(fixture, name))
    result = call(fixture, 'reconcile')
    assert len(count) == 1
    if registered:
        assert result['pending'] is None
        run = result['md_' + name]['run_id']
        assert call(fixture, 'md_' + name + '_status')['md_' + name]['run_id'] == run
    else:
        assert result['execution']['state'] == 'outcome_unknown'
        assert DESIGN not in result['workflow']['tools']
    with pytest.raises(ValueError):
        call(fixture, f'release_md_{name}_plan')
    with pytest.raises(ValueError):
        navigate(fixture[0], 'reconsider', workflow_id=load(fixture[1])['workflow_id'],
                 expected_revision=load(fixture[1])['revision'], reason='Keep original task')


def test_completion_and_reopen_preserve_receipt_and_next_stage(ready):
    fixture, name = ready
    _, result = finish(fixture, name)
    record = result['md_' + name]
    assert record['equilibration_protocol_completed' if name == 'equilibration' else name + '_completed'] and not record['md_system_ready']
    assert not record['equilibration_completed'] and not record['production_started']
    assert result['execution']['task_ids'] == [record['run_id']]
    assert any(item['kind'] == 'openmm_' + name + '_receipt' for item in result['artifacts'])
    count = len(list((fixture[0] / 'state/jobs').iterdir()))
    reopened = navigate(fixture[0], 'resume', workflow_id=result['workflow']['workflow_id'])
    assert reopened['md_' + name][name] == record[name]
    assert reopened['workflow']['stage'] == f'MD_{name.upper()}_REVIEW'
    assert len(list((fixture[0] / 'state/jobs').iterdir())) == count
    if name == 'system_preparation':
        assert 'plan_md_short_qc' in reopened['workflow']['tools'][DESIGN]['actions']
    elif name == 'short_qc':
        assert record['integration_stability_passed'] and record['next_stage'] == 'equilibration_review'
        assert 'plan_md_equilibration' in reopened['workflow']['tools'][DESIGN]['actions']
    else:
        assert record['production_review_ready'] and record['next_stage'] == 'production_review'


@pytest.mark.parametrize('target', ['output', 'parent', 'parameterization'])
def test_changed_artifacts_withdraw_then_restore_same_receipt(ready, target):
    fixture, name = ready
    output, result = finish(fixture, name)
    state = load(fixture[1])
    record = result['md_' + name]
    if target == 'output':
        path = output / ('minimization/system.xml' if name == 'system_preparation' else 'frames.jsonl')
    else:
        run = state['md_parameterization']['run_id'] if target == 'parameterization' else record['input']['run_id']
        base = JobManager(fixture[0]).registry.job_dir(run) / 'artifacts'
        path = (base / 'openmm-system-preparation/minimization/system.xml'
                if name == 'short_qc' and target == 'parent' else
                base / 'openmm-short-qc/final-positions-nm.npy' if name == 'equilibration' and target == 'parent' else
                base / 'adduct-parameterization/parameterized.cms')
    original = path.read_bytes()
    path.write_bytes(original + b'changed')
    reopened = navigate(fixture[0], 'resume', workflow_id=state['workflow_id'])
    current = reopened['md_' + name]
    assert current['verification'] == 'evidence_rejected' and current[name] is None
    assert current['historical_' + name] == record[name]
    assert current['next_stage'] is None
    path.write_bytes(original)
    restored = call(fixture, 'md_' + name + '_status')
    assert restored['md_' + name][name] == record[name]


def test_failure_reason_and_missing_outputs_are_not_success(ready):
    fixture, name = ready
    call(fixture, 'plan_md_' + name)
    result = dispatch(fixture[0], DESIGN, approve(fixture, name))
    run = result['md_' + name]['run_id']
    manager = JobManager(fixture[0])
    terminal = manager.registry.load_result(run)
    terminal.state, terminal.return_code = 'FAILED', 1
    terminal.failure_reason, terminal.message = 'software_fixture_failure', 'Explicit failed worker fixture'
    manager.registry.write_result(terminal)
    result = call(fixture, 'md_' + name + '_status')
    assert result['execution']['state'] == 'failed'
    assert result['md_' + name]['failure_reason'] == 'software_fixture_failure'
    assert not result['md_' + name][name + '_completed']
    terminal.state, terminal.return_code = 'COMPLETED', 0
    manager.registry.write_result(terminal)
    reopened = navigate(fixture[0], 'resume', workflow_id=load(fixture[1])['workflow_id'])
    assert reopened['md_' + name]['verification'] == 'evidence_rejected'


def test_short_qc_cannot_skip_preparation(admitted):
    complete(admitted)
    with pytest.raises(ValueError, match='parent review'):
        call(admitted, 'plan_md_short_qc')


@pytest.mark.parametrize('ready', ['equilibration'], indirect=True)
def test_equilibration_failed_quality_and_progress_never_grant_admission(ready):
    fixture, name = ready
    call(fixture, 'plan_md_' + name)
    started = dispatch(fixture[0], DESIGN, approve(fixture, name))
    record = started['md_' + name]
    folder = equilibrated(fixture, record['run_id'], admitted=False, parent=record['input'])
    # Progress can claim completion; admission must still come from final frames.
    (folder / 'progress.json').write_text(json.dumps(dict(schema='openmm-progress-v1',
        system_id=record['input']['candidate_id'], total_steps=250000, completed_steps=250000,
        elapsed_seconds=1, stage='completed', scientific_acceptance=False)))
    result = call(fixture, 'md_equilibration_status')
    current = result['md_equilibration']
    assert current['verification'] == 'verified' and current['equilibration_protocol_completed']
    assert current['progress']['stage'] == 'completed'
    assert current['checks']['temperature_window'] is False
    assert current['production_review_ready'] is False and current['next_stage'] is None
    assert result['assessment']['state'] == 'rejected'
    assert 'plan_md_production' not in result['workflow']['tools'].get(DESIGN, {}).get('actions', [])


@pytest.mark.parametrize('ready', ['equilibration'], indirect=True)
def test_equilibration_runtime_binary_is_bound_to_approval(ready, tmp_path_factory):
    from covscreen.jobs import sha256_file
    from covscreen.source_release import verify_input
    fixture, name = ready
    binary = tmp_path_factory.mktemp('external-runtime') / 'runtime-plugin.so'
    binary.write_bytes(b'ARTIFICIAL BINARY IDENTITY TEST; NEVER LOADED')
    path = fixture[0] / 'configs/local.inspire.yaml'
    config = json.loads(path.read_text())
    config['openmm_equilibration']['runtime_artifacts'] = {str(binary): sha256_file(binary)}
    path.write_text(json.dumps(config))
    result = call(fixture, 'plan_md_equilibration')
    assert str(binary) in result['md_equilibration']['plan']['spec']['input_paths']
    assert result['md_equilibration']['plan']['spec']['input_sha256'][str(binary)] == sha256_file(binary)
    spec = JobManager(fixture[0]).build_spec(request(fixture[0], load(fixture[1]), name))
    digest = sha256_file(binary)
    verify_input(fixture[0], str(binary), digest, spec)
    with pytest.raises(ValueError, match='outside project'):
        verify_input(fixture[0], str(binary), digest, spec.model_copy(update={
            'parameters': {**spec.parameters, 'runtime_artifacts': {}}}))
    proof = approve(fixture, name)
    binary.write_bytes(b'CHANGED')
    with pytest.raises(ValueError, match='Frozen input changed'):
        verify_input(fixture[0], str(binary), digest, spec)
    with pytest.raises(ValueError, match='runtime artifact changed'):
        dispatch(fixture[0], DESIGN, proof)
    assert not load(fixture[1]).get('pending')


def test_cpu_stage_avoids_covdock_hpc_dispatcher_and_freezes_config(ready):
    fixture, name = ready
    path = fixture[0] / 'configs/local.inspire.yaml'
    config = json.loads(path.read_text())
    config['runtime'] = {'worker_backend': 'external-hpc'}
    path.write_text(json.dumps(config))
    result = call(fixture, 'plan_md_' + name)
    spec = result['md_' + name]['plan']['spec']
    assert spec['provenance']['worker_backend'] == 'local'
    assert spec['provenance']['config_binding'] == 'immutable-runtime-config-v1'
    frozen = spec['provenance']['config_path']
    assert spec['input_sha256'][frozen] == spec['provenance']['config_sha256']
    assert (fixture[0] / frozen).read_bytes() == path.read_bytes()


def test_failed_attempt_gets_new_identity_and_approval_without_repeating_parent(ready):
    fixture, name = ready
    # Existing branch invalidation uses null for removed optional fields.
    prior = load(fixture[1])
    prior['md_preparation_attempt_history'] = None
    save(fixture[1], prior)
    call(fixture, 'plan_md_' + name)
    approval = approve(fixture, name)
    started = dispatch(fixture[0], DESIGN, approval)
    run = started['md_' + name]['run_id']
    with pytest.raises(ValueError, match='confirmed failed terminal'):
        call(fixture, 'retry_md_' + name + '_plan')
    manager = JobManager(fixture[0])
    terminal = manager.registry.load_result(run)
    terminal.state, terminal.return_code = 'FAILED', 1
    terminal.failure_reason = 'entrypoint_import_failure'
    manager.registry.write_result(terminal)
    call(fixture, 'md_' + name + '_status')
    before = load(fixture[1])
    count = len(list((fixture[0] / 'state/jobs').iterdir()))
    planned = call(fixture, 'retry_md_' + name + '_plan')
    state = load(fixture[1])
    record = planned['md_' + name]
    assert state['md_parameterization'] == before['md_parameterization']
    assert record['input'] == before['md_' + name]['input']
    assert state['md_preparation_attempt_history'][0]['run_id'] == run
    assert planned['md_preparation_attempt_history'][0]['failure_reason'] == 'entrypoint_import_failure'
    assert record['plan']['spec']['request_id'] != before['md_' + name]['request_id']
    assert record['plan']['spec']['attempt'] == 2
    assert not record.get('approved_scope') and not record.get('run_id')
    assert len(list((fixture[0] / 'state/jobs').iterdir())) == count
    with pytest.raises(ValueError):
        dispatch(fixture[0], DESIGN, approval)
    result = dispatch(fixture[0], DESIGN, approve(fixture, name))
    assert result['md_' + name]['run_id'] != run
    assert len(list((fixture[0] / 'state/jobs').iterdir())) == count + 1


def test_nullable_archived_plan_history_can_be_reused(ready):
    fixture, name = ready
    prior = load(fixture[1])
    prior['md_preparation_plan_history'] = None
    save(fixture[1], prior)
    call(fixture, 'plan_md_' + name)
    call(fixture, 'release_md_' + name + '_plan')
    assert len(load(fixture[1])['md_preparation_plan_history']) == 1


@pytest.mark.parametrize('filename', ['run_openmm_system_preparation_job.py', 'run_openmm_short_qc.py', 'run_openmm_equilibration.py'])
def test_packaged_entrypoint_bootstraps_without_science_pythonpath(tmp_path, filename):
    import os
    import subprocess
    import sys
    from covscreen.layout import library_script
    # Use the same installed route script that JobManager selects. --help exits
    # before accessing a molecule or performing any scientific calculation.
    script = library_script(tmp_path, filename)
    env = {key: value for key, value in os.environ.items() if key != 'PYTHONPATH'}
    result = subprocess.run([sys.executable, '-B', str(script), '--help'], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert '--output' in result.stdout
