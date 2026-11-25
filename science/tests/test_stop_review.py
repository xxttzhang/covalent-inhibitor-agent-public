import json
import shutil
import pytest

from covscreen.jobs import sha256_file
from covscreen.mcp.stop_review import issue_observation_review, active_review
from covscreen.mcp.task_guard import guarded_call
from covscreen.mcp.server import dispatch
from covscreen.mcp.registry import TOOLS
from test_result_verification import fixture

ENV = {'COVALENT_ERROR_POLICY': 'classified-v1'}


@pytest.fixture
def reviewed(tmp_path, monkeypatch):
    key = tmp_path / 'maintenance-test.key'
    key.write_bytes(b'isolated-maintenance-key-1234567890')
    key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    folder = tmp_path / 'state/task_guard'; folder.mkdir(parents=True)
    marker = folder / 'stopped.json'
    marker.write_text(json.dumps({'tool': 'covalent_get_job', 'status': 'FAIL',
                                'failure': {'category': 'calculation_failure'}}))
    original = marker.read_bytes()
    review = issue_observation_review(tmp_path, expected_stop_sha256=sha256_file(marker),
        evidence_sha256='a'*64, reason='Reviewed synthetic historical observation false-stop fixture.')
    return tmp_path, marker, original, review


def test_review_only_restores_diagnostics_preserving_stop(reviewed):
    root, marker, original, review = reviewed
    result = guarded_call(root, 'covalent_capabilities', {}, dispatch, environment=ENV)
    assert result['status'] == 'PASS'
    assert result['task_guard']['execution_blocked'] is True
    assert result['task_guard']['maintenance_review_id'] == review['review_id']
    for name in ['covalent_submit_job', 'covalent_retry_job', 'covalent_predict_cysteine_sites', 'covalent_library_first_chain']:
        blocked = guarded_call(root, name, {'execution_mode': 'execute'}, dispatch, environment=ENV)
        assert blocked['status'] == 'FAIL'
        assert blocked['calculation_submitted'] is False
    assert marker.read_bytes() == original
    assert not any('review' in tool['name'] for tool in TOOLS)


def test_new_verification_failure_revokes_review_across_calls(reviewed):
    root, marker, original, review = reviewed
    registry, result, output = fixture(root)
    good = guarded_call(root, 'covalent_verify_job_result', {'run_id': result.run_id}, dispatch, environment=ENV)
    assert good['verified'] is True
    (output / 'stage2_candidates.csv').write_text('tampered')
    bad = guarded_call(root, 'covalent_verify_job_result', {'run_id': result.run_id}, dispatch, environment=ENV)
    assert bad['verified'] is False
    assert active_review(root) is None
    assert (root / 'state/task_guard/reviews' / (review['review_id'] + '.revoked.json')).exists()
    assert guarded_call(root, 'covalent_capabilities', {}, dispatch, environment=ENV)['status'] == 'FAIL'
    assert marker.read_bytes() == original


@pytest.mark.parametrize('change', ['signature', 'marker', 'root'])
def test_review_cannot_be_rebound_or_forged(reviewed, change, tmp_path):
    root, marker, _, _ = reviewed
    path = root / 'state/task_guard/review.json'
    if change == 'signature':
        data = json.loads(path.read_text()); data['proof'] += '0'; path.write_text(json.dumps(data))
    if change == 'marker': marker.write_text(marker.read_text() + '\n')
    if change == 'root':
        other = tmp_path / 'other'
        shutil.copytree(root / 'state/task_guard', other / 'state/task_guard')
        root = other
    assert active_review(root) is None


def test_review_cannot_reclassify_permission_denial(reviewed):
    root, marker, _, _ = reviewed
    marker.write_text(json.dumps({'tool': 'covalent_get_job', 'failure': {'category': 'permission_denied'}}))
    with pytest.raises(ValueError, match='only applies'):
        issue_observation_review(root, expected_stop_sha256=sha256_file(marker),
            evidence_sha256='a'*64, reason='Cannot turn a permission denial into a historical-read review.')


def test_expired_review_and_wrong_expected_stop_are_rejected(reviewed):
    from covscreen.native_approval import decode, signed
    import time
    root, marker, _, _ = reviewed
    with pytest.raises(ValueError, match='changed'):
        issue_observation_review(root, expected_stop_sha256='0'*64, evidence_sha256='a'*64,
            reason='Explicit expected stop identity must match before signing.')
    path = root / 'state/task_guard/review.json'
    value = decode(json.loads(path.read_text())['proof'])
    value['expires'] = time.time() - 1
    path.write_text(json.dumps({'proof': signed(value)}))
    assert active_review(root) is None
