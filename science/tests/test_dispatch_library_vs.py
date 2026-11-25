"""CPU dispatcher contract with bounded fake CLI replies, never remote calls."""
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def dispatcher(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'routes/library-first/ops'))
    module = importlib.import_module('dispatch_library_vs')
    monkeypatch.setattr(module.bridge.hpc, 'ROOT', '/inspire/test/data')
    monkeypatch.setattr(module.bridge.hpc, 'RELEASE_BINDING', {
        'data_root': '/inspire/test/data', 'release_root': '/inspire/test/releases/' + 'a' * 64,
        'release_id': 'a' * 64, 'manifest_sha256': 'b' * 64})
    record = {'run_id': 'software-prescreen-task', 'name': 'prescreen-one-' + 'c' * 20, 'spec_sha256': 'c' * 64,
        'engine': 'covindb-prescreen', 'route': 'library-first', 'state': 'QUEUED', 'receipt': None, 'minutes': 15,
        'budget': {'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16, 'max_runtime_minutes': 15, 'max_candidates': 5}}
    return module, record


def test_library_exact_cpu_budget_engine_route_and_release(dispatcher):
    module, record = dispatcher
    argv = module.bridge.hpc.create_args(module.check(record), 'fixture-image')
    assert argv[argv.index('--quota') + 1] == '0,4,16'
    assert argv[argv.index('--max-time') + 1] == '0.25'
    for changes in ({'route': 'ligand-first'}, {'engine': 'carsidock-cov'}, {'engine': 'rtmscore'},
        {'budget': {**record['budget'], 'cpu_count': 8}}, {'budget': {**record['budget'], 'max_candidates': 4}}):
        with pytest.raises(ValueError):
            module.check({**record, **changes})
    vs = {**record, 'engine': 'schrodinger-covdock-vs', 'minutes': 60,
        'budget': {**record['budget'], 'max_runtime_minutes': 60}}
    assert module.check(vs)['budget']['max_candidates'] == 5
    lo = {**record, 'engine': 'schrodinger-covdock-lo', 'minutes': 180,
        'budget': {**record['budget'], 'max_runtime_minutes': 180, 'max_candidates': 1}}
    argv = module.bridge.hpc.create_args(module.check(lo), 'fixture-image')
    assert float(argv[argv.index('--max-time') + 1]) == 3.0
    with pytest.raises(ValueError):
        module.check({**lo, 'minutes': 60})


def test_library_uncertain_creation_is_observed_without_replay(dispatcher, tmp_path, monkeypatch):
    module, record = dispatcher
    calls = []
    def remote(args):
        calls.append(args)
        return dict(record)
    def cli(args):
        calls.append(args)
        if args[1] == 'create' and '--dry-run' not in args:
            raise OSError('Controlled uncertain create response')
        return {'name': record['name'], 'status': 'RUNNING'}
    monkeypatch.setattr(module.bridge.hpc, 'remote', remote)
    monkeypatch.setattr(module.bridge.hpc, 'call', cli)
    path = tmp_path / 'receipt.json'
    with pytest.raises(OSError, match='uncertain create'):
        module.submit(record['run_id'], path, 'fixture-image')
    assert json.loads(path.read_text())['engine'] == 'covindb-prescreen'
    created = len(calls)
    with pytest.raises(ValueError, match='record exists'):
        module.submit(record['run_id'], path, 'fixture-image')
    assert len(calls) == created
    assert module.status(record['run_id'], path)['scheduler_state'] == 'RUNNING'
    assert all(item[:2] != ['hpc', 'create'] for item in calls[created:])


def test_library_instance_uses_jupyter_and_never_replays_uncertain_launch(dispatcher, tmp_path, monkeypatch):
    module, _ = dispatcher
    calls = []
    record = {'run_id': 'library-instance', 'spec_sha256': 'c' * 64, 'execution_instance': 'asgbie2',
              'state': 'QUEUED', 'worker_claimed': False}
    def remote(args, **kwargs):
        calls.append((args, kwargs))
        assert kwargs == {'instance': 'asgbie2', 'transport': 'jupyter'}
        if args[0] == 'launch-instance':
            raise OSError('Uncertain instance response')
        return dict(record)
    monkeypatch.setattr(module.bridge.hpc, 'remote', remote)
    path = tmp_path / 'receipt.json'
    with pytest.raises(OSError, match='Uncertain'):
        module.submit(record['run_id'], path, 'unused', instance='asgbie2')
    assert json.loads(path.read_text())['execution_instance'] == 'asgbie2'
    before = len(calls)
    with pytest.raises(ValueError, match='record exists'):
        module.submit(record['run_id'], path, 'unused', instance='asgbie2')
    assert len(calls) == before
    assert module.status(record['run_id'], path)['name'] == 'asgbie2'
    assert calls[-1][0][0] == 'inspect-instance'
    assert len([args for args, _ in calls if args[0] == 'launch-instance']) == 1
