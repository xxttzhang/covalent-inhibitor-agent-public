import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from covscreen.approval_scope import approval_scope, prepare
from covscreen.execution import JobManager
from covscreen.jobs import JobSubmissionRequest, RunResult, sha256_file, utc_now
from covscreen.native_approval import approval_observation, authorize
from covscreen.workflow_state import persist_chain_observation
from test_native_approval import configured, proof, request


def test_isolated_worker_reports_engine_failure_instead_of_silently_losing_job(configured):
    """A real failed engine must be observed by the separately bound supervisor."""
    import shutil
    import time
    engine = shutil.which('false')
    assert engine
    config = configured / 'configs/local.inspire.yaml'
    config.parent.mkdir()
    config.write_text(f'carsidock:\n  python: {engine}\nruntime:\n  worker_python: {sys.executable}\n')
    args = request()
    scope = approval_scope(configured, 'covalent_submit_job', args)
    assert scope['spec']['provenance']['worker_python_executable'] == sys.executable
    assert scope['spec']['provenance']['worker_python_sha256'] == sha256_file(Path(sys.executable))
    token = proof(args, scope=scope)
    manager = JobManager(configured)
    with authorize(configured, 'covalent_submit_job', {**args, '_native_approval': token}):
        result = manager.submit(JobSubmissionRequest.model_validate(args))
    deadline = time.monotonic() + 15
    while result.state in {'QUEUED', 'RUNNING'} and time.monotonic() < deadline:
        time.sleep(0.1)
        result = manager.registry.load_result(result.run_id)
    assert result.state == 'FAILED', result.model_dump()
    assert result.return_code == 1
    spec = manager.registry.load_spec(result.run_id)
    assert spec.command[0] == engine
    assert spec.provenance['worker_python_executable'] == sys.executable


def approved_spec(root):
    args = request()
    scope = approval_scope(root, 'covalent_submit_job', args)
    token = proof(args, scope=scope)
    with authorize(root, 'covalent_submit_job', {**args, '_native_approval': token}):
        # Compile and bind only. No submit, worker, or scientific engine call.
        spec = JobManager(root)._prepare_submission(JobSubmissionRequest.model_validate(args))
    return spec, scope, token


def test_local_spec_carries_verified_historical_approval_link(configured):
    spec, scope, token = approved_spec(configured)
    observation = approval_observation(spec)
    assert spec.provenance['worker_backend'] == 'local'
    expected = {
        'status': 'SIGNED_JOB_BINDING_VERIFIED', 'session_id': 'session1',
        'message_id': 'message1', 'approval_request_id': 'permission_test',
        'tool': 'covalent_submit_job', 'scope_kind': 'job',
        'scope_sha256': prepare(configured, 'covalent_submit_job', request())['scope_sha256'],
        'execution_authorized': False, 'host_audit_revalidated': False,
    }
    assert {key: observation[key] for key in expected} == expected
    assert observation['native_signature_verified'] is True
    assert observation['current_authorization_evaluated'] is False
    assert observation['boolean_or_note_proves_user_approval'] is False
    assert token not in json.dumps(observation)
    assert 'nonce' not in observation
    assert not JobManager(configured).registry.list_results()


def test_workflow_persists_audit_reference_but_never_approval_token(configured):
    spec, _, token = approved_spec(configured)
    registry = JobManager(configured).registry
    registry.create(spec, RunResult(run_id=spec.run_id, project_id=spec.project_id,
        route=spec.route, engine=spec.engine, state='QUEUED', submitted_at=utc_now(),
        message='Synthetic registry fixture, no execution', stdout_path='', stderr_path=''))
    request_path = registry.job_dir(spec.run_id) / 'request.json'
    registry._write_json(request_path, JobManager._rdkit_request_payload(spec))
    assert sha256_file(request_path) == spec.input_sha256[str(request_path.relative_to(configured))]
    child = subprocess.run([sys.executable, '-c',
        'import json,sys; from pathlib import Path; from covscreen.jobs import ArtifactRegistry; '
        'from covscreen.native_approval import approval_observation; '
        'print(json.dumps(approval_observation(ArtifactRegistry(Path(sys.argv[1])).load_spec(sys.argv[2]))))',
        str(configured), spec.run_id], capture_output=True, text=True, check=True, timeout=15,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
             'PYTHONDONTWRITEBYTECODE': '1'})
    assert json.loads(child.stdout) == approval_observation(spec)
    config = configured / 'workflow.json'
    config.write_text('{}')
    response = {'chain': {'campaign_id': 'approval-link-test', 'terminal_state': 'running',
                         'stages': [{'stage': 'prescreen', 'run_id': spec.run_id}]}}
    first = persist_chain_observation(configured, config, response)
    assert first['approval_observations'] == [
        {'stage': 'prescreen', 'run_id': spec.run_id, **approval_observation(spec)}]
    from covscreen.mcp.server import dispatch
    for tool, arguments in [('covalent_get_job', {'run_id': spec.run_id}),
                            ('covalent_wait_job', {'run_id': spec.run_id, 'timeout_seconds': 0})]:
        observed = dispatch(configured, tool, arguments)
        assert observed['approval_observation'] == approval_observation(spec)
        assert token not in json.dumps(observed)
        assert spec.provenance['native_approval'] not in json.dumps(observed)
        assert observed['job']['state'] == 'QUEUED'
    transport = subprocess.run([sys.executable, '-c',
        'import sys; from pathlib import Path; from covscreen.mcp.server import main; '
        'raise SystemExit(main(Path(sys.argv[1])))', str(configured)],
        input=json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': 'covalent_get_job', 'arguments': {'run_id': spec.run_id}}}) + '\n',
        capture_output=True, text=True, check=True, timeout=15,
        env={**os.environ, 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src'),
             'PYTHONDONTWRITEBYTECODE': '1'})
    wire = json.loads(transport.stdout)['result']
    assert wire['isError'] is False
    assert wire['structuredContent']['approval_observation'] == approval_observation(spec)
    assert json.loads(wire['content'][0]['text']) == wire['structuredContent']
    assert token not in transport.stdout
    assert spec.provenance['native_approval'] not in transport.stdout
    content = (configured / first['path']).read_text()
    snapshot = json.loads(content)
    assert snapshot['inputs'][0]['approval_observation'] == approval_observation(spec)
    assert snapshot['approval_required_for_execution'] is True
    assert token not in content
    assert spec.provenance['native_approval'] not in content
    assert snapshot['resume']['arguments']['submit'] is False
    # A changed spec must not be represented by another valid audit snapshot.
    spec.resource_budget.max_candidates += 1
    registry.write_spec(spec)
    with pytest.raises(ValueError, match='Approved job changed'):
        persist_chain_observation(configured, config, response)
    assert (configured / first['path']).read_text() == content


def test_legacy_missing_approval_is_explicit_and_does_not_inherit_authority(configured):
    spec = JobManager(configured)._prepare_submission(JobSubmissionRequest.model_validate(request()))
    assert spec.human_approved is True and spec.approval_note
    observation = approval_observation(spec)
    assert observation['status'] == 'NOT_RECORDED'
    for key in ('execution_authorized', 'native_signature_verified', 'host_audit_revalidated',
                'current_authorization_evaluated', 'boolean_or_note_proves_user_approval'):
        assert observation[key] is False
    assert observation['evidence_scope'] == 'historical_job_signature_binding'
    assert 'not established' in observation['meaning']
    assert 'not an expiry' in observation['meaning']


def test_signed_link_cannot_be_reported_verified_without_key(configured, monkeypatch):
    spec, _, _ = approved_spec(configured)
    monkeypatch.delenv('COVALENT_APPROVAL_KEY_FILE')
    with pytest.raises(ValueError, match='not configured'):
        approval_observation(spec)


def test_mcp_output_removes_nested_signatures_without_changing_internal_records():
    from covscreen.mcp.server import public_output
    original = {'jobs': [{'provenance': {'native_approval': 'sealed-proof', 'source': 'test'}}],
                'arguments': {'_native_approval': 'host-proof'},
                'approval_observation': {'approval_request_id': 'permission_test'}}
    assert public_output(original) == {'jobs': [{'provenance': {'source': 'test'}}],
        'arguments': {}, 'approval_observation': {'approval_request_id': 'permission_test'}}
    assert original['jobs'][0]['provenance']['native_approval'] == 'sealed-proof'
