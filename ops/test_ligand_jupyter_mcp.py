import base64
import json
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

from ligand_jupyter_mcp import command_for, decode_response


def test_client_deadline_does_not_shorten_remote_call(monkeypatch):
    import ligand_jupyter_mcp as bridge
    import io
    seen = {}
    class Client:
        def __init__(self, **kwargs):
            seen.update(kwargs)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    monkeypatch.setitem(sys.modules, 'inspire', SimpleNamespace(InspireClient=Client))
    monkeypatch.setattr(sys, 'argv', ['bridge', '--binding', 'b', '--connection', 'c', '--runtime', 'r'])
    monkeypatch.setattr(sys, 'stdin', io.StringIO(''))
    monkeypatch.setattr(bridge, 'read_profile', lambda path: {'notebook': 'asgbie4', 'workspace': 'CPU资源空间', 'account': 'default'})
    monkeypatch.setattr(bridge, 'remote_command', lambda *args, **kwargs: 'unused')
    bridge.main()
    assert seen['operation_timeout'] == 600


def test_roundtrip_keeps_request_as_data():
    message = {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
               'params': {'text': "中文 ' $(false) `false`\n"}}
    program = ('import sys,json; m=json.load(sys.stdin); '
               'print(json.dumps(dict(jsonrpc="2.0",id=m["id"],result=m["params"])))')
    command = shlex.join([sys.executable, '-c', program])
    result = subprocess.run(shlex.split(command_for(command, message, 'marker:')),
                            capture_output=True, text=True, check=True)
    response = decode_response(SimpleNamespace(completed=True, truncated=False,
        returncode=0, output=result.stdout), 'marker:', 7)
    assert response['result'] == message['params']


@pytest.mark.parametrize('change', ['truncated', 'incomplete', 'remote_failure', 'wrong_id', 'duplicate'])
def test_uncertain_transport_is_not_accepted(change):
    response = {'jsonrpc': '2.0', 'id': 9 if change == 'wrong_id' else 7, 'result': {}}
    receipt = {'returncode': 1 if change == 'remote_failure' else 0,
               'stdout': base64.b64encode(json.dumps(response).encode()).decode()}
    line = 'marker:' + json.dumps(receipt) + '\n'
    result = SimpleNamespace(completed=change != 'incomplete', truncated=change == 'truncated',
                             returncode=0, output=line * (2 if change == 'duplicate' else 1))
    with pytest.raises((ValueError, RuntimeError)):
        decode_response(result, 'marker:', 7)
