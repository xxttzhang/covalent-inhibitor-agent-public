"""Synthetic scheduler responses: never calls Inspire or launches computation."""
import json
import hashlib
import subprocess
from pathlib import Path
import sys

import pytest

OPS = Path(__file__).resolve().parents[2] / 'routes/ligand-first/ops'
sys.path.insert(0, str(OPS))
import cancel_ligand_hpc as adapter


@pytest.fixture
def boundary(tmp_path, monkeypatch):
    record = {'run_id': 'test-stop-001', 'route': 'ligand-first', 'name': 'vs-one-test',
              'spec_sha256': 'a' * 64, 'dispatch_root': adapter.hpc.ROOT}
    scope = {'kind': 'cancel', 'run_id': record['run_id'], 'spec_sha256': record['spec_sha256'],
             'scheduler': {'backend': 'external-hpc', 'name': record['name']}}
    path = tmp_path / 'receipt.json'
    path.write_text(json.dumps(record))
    observed = {**record, 'cancellation': {'scope': scope}}
    calls = []
    states = ['QUEUEING', 'STOPPING', 'STOPPED', 'STOPPED']
    def call(args):
        calls.append(args)
        if args[1] == 'stop':
            return {'name': record['name']}
        return {'name': record['name'], 'status': states.pop(0)}
    monkeypatch.setattr(adapter, 'management_remote', lambda args: observed if args[0] == 'cancel-inspect'
                        else {'cancellation_confirmed': False, 'reason': 'synthetic_started_worker'})
    monkeypatch.setattr(adapter.hpc, 'call', call)
    return record, path, observed, calls, states


def test_stop_is_once_and_terminal_is_not_vendor_confirmation(boundary):
    record, path, _, calls, _ = boundary
    first = adapter.stop(record['run_id'], path)
    assert first['scheduler_state'] == 'STOPPING'
    assert not first['scheduler_terminal_observed']
    final = adapter.stop(record['run_id'], path)
    assert final['scheduler_terminal_observed']
    assert not final['cancellation_confirmed']
    assert not final['vendor_termination_verified']
    assert sum(args[1] == 'stop' for args in calls) == 1
    assert all(args[1] in {'status', 'stop'} for args in calls)


def test_wrong_scope_never_contacts_scheduler(boundary):
    record, path, observed, calls, _ = boundary
    observed['cancellation']['scope']['scheduler']['name'] = 'other'
    with pytest.raises(ValueError, match='scope differs'):
        adapter.stop(record['run_id'], path)
    assert not calls


def test_uncertain_stop_is_not_replayed(boundary, monkeypatch):
    record, path, _, calls, states = boundary
    original = adapter.hpc.call
    def uncertain(args):
        if args[1] == 'stop':
            calls.append(args)
            raise TimeoutError('synthetic ambiguous transport')
        return original(args)
    monkeypatch.setattr(adapter.hpc, 'call', uncertain)
    with pytest.raises(TimeoutError):
        adapter.stop(record['run_id'], path)
    states[:] = ['QUEUEING', 'QUEUEING']
    adapter.stop(record['run_id'], path)
    assert sum(args[1] == 'stop' for args in calls) == 1


def test_unknown_state_does_not_send_stop(boundary):
    record, path, _, calls, states = boundary
    states[:] = ['UNKNOWN']
    with pytest.raises(ValueError, match='Unknown scheduler state'):
        adapter.stop(record['run_id'], path)
    assert all(args[1] == 'status' for args in calls)


def test_observation_without_stop_marker_never_sends_stop(boundary):
    record, path, _, calls, states = boundary
    states[:] = ['RUNNING', 'RUNNING']
    result = adapter.stop(record['run_id'], path, observe_only=True)
    assert result['observation_only']
    assert not result['cancellation_confirmed']
    assert all(args[1] == 'status' for args in calls)
    assert not path.with_name(path.name + '.stop-request.json').exists()


@pytest.mark.parametrize('mutation', ['manifest', 'file', 'symlink'])
def test_management_launcher_rejects_tampered_release_before_import(tmp_path, mutation):
    # Execute the real launcher locally; a sentinel module must never import.
    sentinel = tmp_path / 'imported'
    content = f'from pathlib import Path; Path({str(sentinel)!r}).touch()\n'
    files = {'science/src/covscreen/__init__.py': hashlib.sha256(content.encode()).hexdigest()}
    manifest = {'version': 'screening-code-release-v1', 'files': files}
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    root = tmp_path / hashlib.sha256(canonical).hexdigest()
    source = root / 'science/src/covscreen/__init__.py'
    source.parent.mkdir(parents=True)
    source.write_text(content)
    raw = canonical + b'\n'
    (root / 'manifest.json').write_bytes(raw)
    binding = {'release_root': str(root), 'release_id': root.name,
               'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'data_root': str(tmp_path / 'data')}
    if mutation == 'manifest':
        (root / 'manifest.json').write_bytes(raw + b' ')
    elif mutation == 'file':
        source.write_text(content + '# changed\n')
    else:
        other = tmp_path / 'other.py'
        other.write_text(content)
        source.unlink()
        source.symlink_to(other)
    result = subprocess.run([sys.executable, '-B', '-c', adapter.MANAGEMENT_LAUNCH,
                             json.dumps(binding), 'test-stop-001'], capture_output=True, text=True)
    assert result.returncode != 0
    assert 'ValueError' in result.stderr
    assert not sentinel.exists()


def test_vs_status_retains_old_worker_release_after_management_upgrade(tmp_path, monkeypatch, capsys):
    import dispatch_ligand_vs as vs
    root = '/inspire/test/data'
    def binding(digest):
        return {'release_id': digest, 'manifest_sha256': 'c' * 64,
                'release_root': '/inspire/test/releases/' + digest, 'data_root': root}
    old, new = binding('a' * 64), binding('b' * 64)
    receipt = tmp_path / 'dispatch.json'
    receipt.write_text(json.dumps({'dispatch_root': root, 'release_binding': old}))
    selected = tmp_path / 'binding.json'
    selected.write_text(json.dumps(new))
    monkeypatch.setattr(vs.hpc, 'ROOT', vs.hpc.ROOT)
    monkeypatch.setattr(vs.hpc, 'RELEASE_BINDING', vs.hpc.RELEASE_BINDING)
    monkeypatch.setattr(sys, 'argv', ['dispatch_ligand_vs.py', 'status', '--run-id', 'test-old-001',
        '--state', str(receipt), '--root', root, '--binding', str(selected)])
    observed = []
    def status(run, path):
        observed.append(dict(vs.hpc.RELEASE_BINDING))
        return {'state': 'synthetic_observation_only'}
    monkeypatch.setattr(vs, 'status', status)
    vs.main()
    assert observed == [old]
    assert json.loads(receipt.read_text())['release_binding'] == old
