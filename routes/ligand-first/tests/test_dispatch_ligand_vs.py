"""Exercise at-most-once VS dispatch with controlled CLI replies, no remote calls."""
import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def dispatch(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'ops'))
    module = importlib.import_module('dispatch_ligand_vs')
    monkeypatch.setattr(module.hpc, 'ROOT', '/inspire/test/data')
    monkeypatch.setattr(module.hpc, 'RELEASE_BINDING', {
        'data_root':'/inspire/test/data', 'release_root':'/inspire/test/releases/'+'a'*64,
        'release_id':'a'*64, 'manifest_sha256':'b'*64})
    record = {'run_id':'software-vs-task', 'name':'vs-one-'+'c'*20, 'spec_sha256':'c'*64,
        'engine':module.ENGINE, 'route':'ligand-first', 'state':'QUEUED', 'receipt':None, 'minutes':60,
        'budget':{'gpu_count':0,'cpu_count':4,'memory_gb':16,'max_runtime_minutes':60,'max_candidates':3}}
    return module, record


def test_vs_dispatch_keeps_cpu_quota_and_fixed_release(dispatch):
    module, record = dispatch
    checked = module.check(record)
    argv = module.hpc.create_args(checked, 'software-image')
    assert argv[argv.index('--quota')+1] == '0,4,16'
    assert argv[argv.index('--max-time')+1] == '1.0'
    assert 'ligand_external_hpc.py' in argv[-1]
    assert 'ligand_external_inspire.py' not in argv[-1]
    assert checked['release_binding']['data_root'] == '/inspire/test/data'
    for engine in ('carsidock-cov','schrodinger-covdock-lo'):
        with pytest.raises(ValueError, match='only native Ligand VS'):
            module.check({**record,'engine':engine})
    with pytest.raises(ValueError, match='budget'):
        module.check({**record,'budget':{**record['budget'],'cpu_count':8}})


def test_preflight_error_does_not_reserve_or_create(dispatch, tmp_path, monkeypatch):
    module, record = dispatch
    observations = []
    def remote(args):
        observations.append(args)
        assert args == ['inspect',record['run_id']]
        return record
    def cli(args):
        assert args[-1] == '--dry-run'
        raise RuntimeError('Controlled unavailable quota')
    monkeypatch.setattr(module.hpc,'remote',remote)
    monkeypatch.setattr(module.hpc,'call',cli)
    path = tmp_path/'receipt.json'
    with pytest.raises(RuntimeError, match='unavailable quota'):
        module.submit(record['run_id'],path,'software-image')
    assert len(observations) == 1 and not path.exists()


def test_uncertain_create_keeps_receipt_and_forbids_replay(dispatch, tmp_path, monkeypatch):
    module, record = dispatch
    remote_calls, cli_calls = [], []
    def remote(args):
        remote_calls.append(args)
        return record if args[0] == 'inspect' else {'reserved':True}
    def cli(args):
        cli_calls.append(args)
        if args[-1] == '--dry-run': return {'dry_run':True}
        raise TimeoutError('Controlled lost scheduler reply')
    monkeypatch.setattr(module.hpc,'remote',remote)
    monkeypatch.setattr(module.hpc,'call',cli)
    path = tmp_path/'receipt.json'
    with pytest.raises(TimeoutError):
        module.submit(record['run_id'],path,'software-image')
    saved = json.loads(path.read_text())
    assert saved['spec_sha256'] == record['spec_sha256']
    assert saved['scheduler_image'] == 'software-image'
    assert [args[0] for args in remote_calls] == ['inspect','reserve']
    with pytest.raises(ValueError, match='without replay'):
        module.submit(record['run_id'],path,'software-image')
    assert len(cli_calls) == 2


def test_status_only_observes_original_identity(dispatch, tmp_path, monkeypatch):
    module, record = dispatch
    path = tmp_path/'receipt.json'
    path.write_text(json.dumps(module.check(record)))
    remote_calls, cli_calls = [], []
    def remote(args):
        remote_calls.append(args)
        return record
    def cli(args):
        cli_calls.append(args)
        return {'name':record['name'],'status':'RUNNING'}
    monkeypatch.setattr(module.hpc,'remote',remote)
    monkeypatch.setattr(module.hpc,'call',cli)
    value = module.status(record['run_id'],path)
    assert value['scheduler_state'] == 'RUNNING' and not value['scientific_acceptance']
    assert all(args[0] == 'inspect' for args in remote_calls)
    assert cli_calls == [['hpc','status',record['name'],'--workspace','CPU资源空间']]
    record['spec_sha256'] = 'd'*64
    with pytest.raises(ValueError, match='identity differs'):
        module.status(record['run_id'],path)
    assert len(cli_calls) == 1


def test_instance_lost_launch_reply_observes_original_task(dispatch, tmp_path, monkeypatch):
    module, original = dispatch
    monkeypatch.setenv('COVALENT_LIGAND_VS_INSTANCE', 'asgbie3')
    run_id = original['run_id']
    digest = original['spec_sha256']
    receipt = tmp_path / 'receipt.json'
    requests = []

    def remote(args):
        requests.append(args)
        if args[0] == 'launch-instance':
            assert json.loads(receipt.read_text())['spec_sha256'] == digest
            raise TimeoutError('reply lost after launch')
        return {'run_id': run_id, 'spec_sha256': digest, 'execution_instance': 'asgbie3',
                'state': 'RUNNING' if receipt.exists() else 'QUEUED',
                'worker_claimed': receipt.exists()}

    monkeypatch.setattr(module.hpc, 'remote', remote)
    with pytest.raises(TimeoutError, match='reply lost'):
        module.submit(run_id, receipt, 'unused')
    with pytest.raises(ValueError, match='without replay'):
        module.submit(run_id, receipt, 'unused')
    assert module.status(run_id, receipt)['platform_state'] == 'RUNNING'
    assert requests == [
        ['inspect-instance', run_id],
        ['launch-instance', run_id, '--digest', digest],
        ['inspect-instance', run_id],
    ]
