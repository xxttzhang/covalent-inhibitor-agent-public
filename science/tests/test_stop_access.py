import json

from covscreen.jobs import sha256_file
from covscreen.mcp.server import dispatch
from covscreen.mcp.stop_review import issue_observation_review, revoke_review
from covscreen.mcp.stop_access import current_access
from covscreen.mcp.task_guard import guarded_call
from test_report_claims import supported


def reviewed(root, monkeypatch):
    monkeypatch.setenv('COVALENT_ERROR_POLICY', 'classified-v1')
    key = root / 'maintenance.key'
    key.write_bytes(b'isolated-stop-access-maintenance-key-1234')
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    folder = root / 'state/task_guard'
    folder.mkdir(parents=True)
    marker = folder / 'stopped.json'
    marker.write_text(json.dumps({'tool': 'covalent_get_job', 'stopped_at': 1,
                                 'failure': {'category': 'calculation_failure'}}))
    review = issue_observation_review(root, expected_stop_sha256=sha256_file(marker),
        evidence_sha256='a' * 64, reason='Synthetic historical observation defect review for access contract tests.')
    return marker, review


def test_denied_status_explains_stop_gate_without_granting_cancel(tmp_path, monkeypatch):
    marker, _ = reviewed(tmp_path, monkeypatch)
    before = marker.read_bytes()
    response = guarded_call(tmp_path, 'covalent_predict_cysteine_sites',
                            {'action': 'status', 'run_id': 'historical'}, dispatch)
    access = response['stop_gate_access']
    assert response['status'] == 'FAIL'
    assert response['calculation_submitted'] is False
    assert access['tools'] == response['allowed_tools']
    assert access['tool_count'] == 16
    assert access['all_tools_read_only'] is False
    assert access['cancellation_tools'] == ['covalent_cancel_job']
    assert access['cancellation_requires_native_approval'] is True
    assert access['grants_native_permission'] is False
    assert access['authorizes_scientific_execution'] is False
    assert access['historical_job_status_tool'] == 'covalent_get_job'
    assert 'covalent_resume_workflow' in access['planning_and_workflow_tools']
    assert marker.read_bytes() == before
    assert not (tmp_path / 'state/jobs').exists()


def test_report_verifier_catches_actual_v1247_access_misstatement(tmp_path, monkeypatch):
    marker, _ = reviewed(tmp_path, monkeypatch)
    before = marker.read_bytes()
    request = supported(True)
    request['claims'].update(stop_gate_tool_count=15, stop_gate_all_tools_read_only=True,
                             cancellation_requires_native_approval=False)
    response = guarded_call(tmp_path, 'covalent_verify_report_claims', request, dispatch)
    assert response['status'] == 'CORRECTION_REQUIRED'
    assert {entry['field'] for entry in response['discrepancies']} == {
        'stop_gate_tool_count', 'stop_gate_all_tools_read_only', 'cancellation_requires_native_approval'}
    request['claims'].update(stop_gate_tool_count=16, stop_gate_all_tools_read_only=False,
                             cancellation_requires_native_approval=True)
    response = guarded_call(tmp_path, 'covalent_verify_report_claims', request, dispatch)
    assert response['claims_verified'] is True
    assert len(response['validated_fields']) == 8
    assert response['execution_authorized'] is False
    assert marker.read_bytes() == before


def test_revoked_review_reduces_current_access_and_old_count_is_not_valid(tmp_path, monkeypatch):
    _, review = reviewed(tmp_path, monkeypatch)
    assert current_access(tmp_path)['tool_count'] == 16
    revoke_review(tmp_path, review['review_id'], 'covalent_verify_job_result', 'verification_failure')
    assert current_access(tmp_path)['tool_count'] == 9
    request = supported(True)
    request['claims']['stop_gate_tool_count'] = 16
    response = guarded_call(tmp_path, 'covalent_verify_report_claims', request, dispatch)
    assert response['status'] == 'CORRECTION_REQUIRED'
    assert response['supported_facts']['stop_gate_tool_count'] == 9


def test_inactive_gate_is_not_an_empty_permission_grant(tmp_path, monkeypatch):
    monkeypatch.delenv('COVALENT_ERROR_POLICY', raising=False)
    monkeypatch.delenv('COVALENT_STOP_ON_TOOL_FAILURE', raising=False)
    access = current_access(tmp_path)
    assert access['gate_active'] is False
    assert access['tool_count'] is None
    assert access['grants_native_permission'] is False
    assert not (tmp_path / 'state').exists()
