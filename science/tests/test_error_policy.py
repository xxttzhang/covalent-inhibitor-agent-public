import json
import pytest

from covscreen.mcp.task_guard import guarded_call

ENV = {'COVALENT_ERROR_POLICY': 'classified-v1'}


def test_transient_read_recovers_with_bounded_attempts(tmp_path):
    calls = []
    def flaky(*args):
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionResetError('temporary')
        return {'status': 'PASS', 'job': {'state': 'RUNNING'}}
    result = guarded_call(tmp_path, 'covalent_get_job', {}, flaky, environment=ENV)
    assert len(calls) == 3
    assert result['recovery']['attempts'] == 3
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()


@pytest.mark.parametrize('name,error,attempts', [
    ('covalent_submit_job', TimeoutError('response lost'), 1),
    ('covalent_get_job', PermissionError('denied'), 1),
    ('covalent_get_job', ValueError('hash mismatch'), 1),
    ('covalent_get_job', FileNotFoundError('missing handle'), 1),
])
def test_exhaustion_and_critical_failures_stop_across_calls(tmp_path, name, error, attempts):
    calls = []
    def broken(*args):
        calls.append(1)
        raise error
    response = guarded_call(tmp_path, name, {}, broken, environment=ENV)
    assert response['status'] == 'FAIL'
    assert len(calls) == attempts
    if name == 'covalent_submit_job':
        assert response['execution_outcome'] == 'unknown_do_not_resubmit'
        assert 'calculation_submitted' not in response
    guarded_call(tmp_path, 'covalent_submit_job', {}, broken, environment=ENV)
    assert len(calls) == attempts
    record = json.loads((tmp_path / 'state/task_guard/stopped.json').read_text())
    assert record['failure']['attempts'] == attempts


def test_observing_failed_job_does_not_stop_unrelated_workflows(tmp_path):
    response = guarded_call(tmp_path, 'covalent_get_job', {},
        lambda *_: {'status': 'PASS', 'job': {'state': 'FAILED'}}, environment=ENV)
    assert response['job_failure']['category'] == 'calculation_failure'
    assert response['job_failure']['automatic_retry_allowed'] is False
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()


def test_unstructured_failure_never_retried_and_no_arguments_in_marker(tmp_path):
    calls = []
    def fail(*args):
        calls.append(1)
        return {'status': 'FAIL', 'error': 'timeout'}
    guarded_call(tmp_path, 'covalent_get_job', {'secret': 'not-for-audit'}, fail, environment=ENV)
    assert len(calls) == 1
    assert 'not-for-audit' not in (tmp_path / 'state/task_guard/stopped.json').read_text()


def test_nested_chain_quality_failure_is_not_hidden_by_pass(tmp_path):
    result = guarded_call(tmp_path, 'covalent_resume_workflow', {},
        lambda *_: {'status': 'PASS', 'chain': {'terminal_state': 'quality_gate_failed'}}, environment=ENV)
    assert result['status'] == 'FAIL'
    assert result['failure']['category'] == 'scientific_quality_failure'
    assert (tmp_path / 'state/task_guard/stopped.json').is_file()


def test_stopped_study_keeps_real_diagnostics_and_cancellation_permission(tmp_path):
    from covscreen.mcp.server import dispatch
    from test_covalent_site_discovery import prediction
    run_id, _ = prediction(tmp_path)
    # A real missing-job lookup persists the initial failure.
    first = guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-job'}, dispatch, environment=ENV)
    assert first['status'] == 'FAIL'
    marker = tmp_path / 'state/task_guard/stopped.json'
    original = marker.read_bytes()
    for tool, arguments in [
        ('covalent_get_job', {'run_id': run_id}),
        ('covalent_wait_job', {'run_id': run_id, 'timeout_seconds': 0}),
        ('covalent_list_jobs', {}),
        ('covalent_list_workflows', {}),
    ]:
        result = guarded_call(tmp_path, tool, arguments, dispatch, environment=ENV)
        assert result['status'] == 'PASS'
        assert result['task_guard']['execution_blocked'] is True
        assert marker.read_bytes() == original
    cancel = guarded_call(tmp_path, 'covalent_cancel_job',
                          {'run_id': run_id, 'human_approved': False}, dispatch, environment=ENV)
    assert cancel['status'] == 'FAIL'
    assert cancel['task_guard']['original_failure_preserved'] is True
    assert marker.read_bytes() == original
    for tool in ('covalent_submit_job', 'covalent_retry_job', 'covalent_resume_workflow'):
        blocked = guarded_call(tmp_path, tool, {}, dispatch, environment=ENV)
        assert blocked['status'] == 'FAIL'
        assert blocked['calculation_submitted'] is False
        assert 'covalent_cancel_job' in blocked['allowed_tools']
    assert marker.read_bytes() == original


def test_strict_policy_still_stops_all_calls(tmp_path):
    from covscreen.mcp.server import dispatch
    strict = {'COVALENT_STOP_ON_TOOL_FAILURE': '1'}
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-job'}, dispatch, environment=strict)
    result = guarded_call(tmp_path, 'covalent_list_jobs', {}, dispatch, environment=strict)
    assert result['status'] == 'FAIL'
    assert result['allowed_tools'] == []


def test_historical_failure_observation_remains_available_but_not_verified(tmp_path):
    from covscreen.mcp.server import dispatch
    from test_result_verification import fixture
    registry, result, _ = fixture(tmp_path)
    result.state = 'FAILED'
    result.return_code = 2
    registry.write_result(result)
    observed = guarded_call(tmp_path, 'covalent_get_job', {'run_id': result.run_id}, dispatch, environment=ENV)
    assert observed['job']['state'] == 'FAILED'
    assert observed['job_failure']['automatic_retry_allowed'] is False
    verified = guarded_call(tmp_path, 'covalent_verify_job_result', {'run_id': result.run_id}, dispatch, environment=ENV)
    assert verified['verified'] is False
    assert verified['status'] == 'BLOCKED'
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()


def test_exhausted_observation_preserves_job_and_recovers_after_reconnect(tmp_path):
    from covscreen.mcp.server import dispatch
    from test_result_verification import fixture
    registry, job, _ = fixture(tmp_path)
    before = (registry.job_dir(job.run_id) / 'result.json').read_bytes()
    calls = []
    def unavailable(*args):
        calls.append(1)
        raise TimeoutError('transport unavailable')
    response = guarded_call(tmp_path, 'covalent_get_job',
                            {'run_id': job.run_id, 'secret': 'not-for-audit'},
                            unavailable, environment=ENV)
    assert response['status'] == 'FAIL'
    assert len(calls) == 3
    assert response['task_guard']['study_stopped'] is False
    assert response['failure']['next_action'] == 'observe_original_job_later_do_not_resubmit'
    assert not (tmp_path / 'state/task_guard/stopped.json').exists()
    incidents = list((tmp_path / 'state/task_guard/observation_failures').glob('*.json'))
    assert len(incidents) == 1
    assert 'not-for-audit' not in incidents[0].read_text()
    assert json.loads(incidents[0].read_text())['job_state'] == 'UNKNOWN'
    # A fresh process can still observe the original handle, without submission.
    import os
    import subprocess
    import sys
    from pathlib import Path
    script = '''
import json, sys
from pathlib import Path
from covscreen.mcp.server import dispatch
from covscreen.mcp.task_guard import guarded_call
print(json.dumps(guarded_call(Path(sys.argv[1]), 'covalent_get_job',
    {'run_id': sys.argv[2]}, dispatch,
    environment={'COVALENT_ERROR_POLICY': 'classified-v1'})))
'''
    observed = subprocess.run([sys.executable, '-c', script, str(tmp_path), job.run_id],
                              check=True, capture_output=True, text=True,
                              env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
    assert json.loads(observed.stdout)['job']['run_id'] == job.run_id
    assert (registry.job_dir(job.run_id) / 'result.json').read_bytes() == before


def test_retry_does_not_start_after_scheduling_delay(tmp_path, monkeypatch):
    from covscreen.mcp import error_policy
    clock = [0.0]
    monkeypatch.setattr(error_policy.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(error_policy.time, 'sleep', lambda _: clock.__setitem__(0, 6.0))
    calls = []
    def unavailable(*args):
        calls.append(1)
        raise TimeoutError('transport unavailable')
    result = guarded_call(tmp_path, 'covalent_wait_job', {}, unavailable, environment=ENV)
    assert len(calls) == 1
    assert result['failure']['attempts'] == 1
    assert result['task_guard']['study_stopped'] is False


def test_observation_timeout_does_not_clear_existing_stop(tmp_path):
    guarded_call(tmp_path, 'covalent_submit_job', {},
                 lambda *_: {'status': 'FAIL'}, environment=ENV)
    marker = tmp_path / 'state/task_guard/stopped.json'
    before = marker.read_bytes()
    def unavailable(*args):
        raise TimeoutError('transport unavailable')
    response = guarded_call(tmp_path, 'covalent_get_job', {}, unavailable, environment=ENV)
    assert response['task_guard']['execution_blocked'] is True
    assert marker.read_bytes() == before
