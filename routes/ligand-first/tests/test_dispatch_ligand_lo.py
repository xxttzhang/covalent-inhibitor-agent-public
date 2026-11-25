"""Instance dispatch receipt persistence; controlled transport, no remote jobs."""
import importlib
import json
from pathlib import Path
import pytest


@pytest.fixture
def dispatcher(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1]/'ops'))
    module=importlib.import_module('dispatch_ligand_lo')
    monkeypatch.setattr(module.hpc,'RELEASE_BINDING',{'release_id':'a'*64})
    return module


def test_lost_launch_response_is_observed_not_replayed(dispatcher,tmp_path,monkeypatch):
    calls=[]
    def remote(args):
        calls.append(args)
        if args[0]=='inspect-instance':
            return {'run_id':'test-lo-run','engine':'schrodinger-covdock-lo','spec_sha256':'a'*64,
                'execution_instance':'asgbie3','state':'QUEUED','worker_claimed':False}
        raise TimeoutError('response lost')
    monkeypatch.setattr(dispatcher.hpc,'remote',remote)
    path=tmp_path/'receipt.json'
    with pytest.raises(TimeoutError): dispatcher.submit('test-lo-run',path)
    assert json.loads(path.read_text())['engine']=='schrodinger-covdock-lo'
    with pytest.raises(ValueError,match='without replay'): dispatcher.submit('test-lo-run',path)
    assert len(calls)==2


def test_wrong_engine_never_creates_launch_receipt(dispatcher,tmp_path,monkeypatch):
    monkeypatch.setattr(dispatcher.hpc,'remote',lambda args:{'engine':'schrodinger-covdock-vs'})
    path=tmp_path/'receipt.json'
    with pytest.raises(ValueError,match='exact signed'): dispatcher.submit('test-lo-run',path)
    assert not path.exists()


def test_new_instance_requires_exact_signed_receipt(dispatcher, tmp_path, monkeypatch):
    monkeypatch.setenv('COVALENT_LIGAND_VS_INSTANCE', 'asgbie4')
    record = {'run_id': 'new-lo', 'engine': 'schrodinger-covdock-lo',
        'execution_instance': 'asgbie3', 'state': 'QUEUED',
        'worker_claimed': False, 'spec_sha256': 'a' * 64}
    monkeypatch.setattr(dispatcher.hpc, 'remote', lambda args: record if args[0] == 'inspect-instance'
                        else {'submitted': True, 'name': 'asgbie4'})
    path = tmp_path / 'receipt.json'
    with pytest.raises(ValueError, match='exact signed'):
        dispatcher.submit('new-lo', path)
    assert not path.exists()
    record['execution_instance'] = 'asgbie4'
    assert dispatcher.submit('new-lo', path) == {'submitted': True, 'name': 'asgbie4'}
    assert json.loads(path.read_text())['execution_instance'] == 'asgbie4'
