import json
import shutil

import pytest
from covscreen.job_origin import register_origin
from covscreen.result_verification import verify_job
from test_result_verification import fixture


@pytest.fixture
def imported(tmp_path, monkeypatch):
    key = tmp_path / 'key'; key.write_bytes(b'origin-test-signing-key-1234567890123456'); key.chmod(0o600)
    monkeypatch.setenv('COVALENT_APPROVAL_KEY_FILE', str(key))
    source = tmp_path / 'source'; source.mkdir()
    registry, result, output = fixture(source)
    destination = tmp_path / 'destination'; destination.mkdir()
    shutil.copytree(registry.job_dir(result.run_id), destination / 'state/jobs' / result.run_id)
    return source, destination, result.run_id, output


def register(imported):
    source, destination, run, _ = imported
    return register_origin(destination, source, run, reason='Explicitly reuse historical registered output; do not run new computation.')


def test_incomplete_copy_is_verified_at_signed_origin_without_rewriting(imported):
    source, destination, run, _ = imported
    assert verify_job(destination, run)['verified'] is False
    records = destination / 'state/jobs' / run
    before = {p.name: p.read_bytes() for p in records.iterdir() if p.is_file()}
    register(imported)
    result = verify_job(destination, run)
    assert result['verified'] is True
    assert result['historical_reuse'] is True
    assert result['approval_inherited'] is False
    assert result['origin']['source_root'] == str(source)
    assert not (destination / 'source.csv').exists()
    assert all((records / name).read_bytes() == data for name, data in before.items())
    with pytest.raises(ValueError, match='already registered'):
        register(imported)


@pytest.mark.parametrize('change', ['source_input', 'source_output', 'local_copy', 'local_spec', 'signature'])
def test_changes_never_pass_via_origin_reference(imported, change):
    source, destination, run, output = imported
    register(imported)
    if change == 'source_input': (source / 'source.csv').write_text('changed')
    if change == 'source_output': (output / 'stage2_candidates.csv').write_text('changed')
    if change == 'local_copy':
        p = destination / 'output'; p.mkdir(); (p / 'stage2_candidates.csv').write_text('changed')
    if change == 'local_spec':
        p = destination / 'state/jobs' / run / 'spec.json'
        value = json.loads(p.read_text()); value['request_id'] = 'changed-request'; p.write_text(json.dumps(value))
    if change == 'signature':
        p = destination / 'state/job_origins' / (run + '.json')
        value = json.loads(p.read_text()); value['proof'] += '0'; p.write_text(json.dumps(value))
    assert verify_job(destination, run)['verified'] is False


def test_registration_requires_successful_source_and_no_chaining(imported):
    source, destination, run, output = imported
    (source / 'source.csv').write_text('corrupted')
    with pytest.raises(ValueError, match='failed live'):
        register(imported)
    assert not (destination / 'state/job_origins').exists()


def test_origin_receipt_cannot_be_reused_in_another_study_or_chained(imported, tmp_path):
    source, destination, run, _ = imported
    register(imported)
    other = tmp_path / 'other'; other.mkdir()
    shutil.copytree(destination / 'state/jobs', other / 'state/jobs')
    with pytest.raises(ValueError, match='Chained'):
        register_origin(other, destination, run, reason='Attempted chained provenance must not be accepted.')
    shutil.copytree(destination / 'state/job_origins', other / 'state/job_origins')
    report = verify_job(other, run)
    assert report['verified'] is False
    assert any('receipt scope' in e for e in report['errors'])
