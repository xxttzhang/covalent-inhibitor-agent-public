import importlib.util
import io
import json
from pathlib import Path


def test_preparation_is_a_distinct_read_only_mcp_tool(tmp_path, monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location('approval_mcp', Path(__file__).parents[1] / 'scripts/openscience_mcp_server.py')
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    args = {'request_id': 'protocol-test-001', 'project_id': 'new-project', 'route': 'ligand-first',
            'engine': 'rdkit-warhead-enumeration', 'parameters': {'scaffold_smiles': 'CC', 'allowed_warheads': ['acrylamide']},
            'execution_mode': 'execute', 'human_approved': True, 'approval_note': 'not an approval',
            'resource_budget': {'max_candidates': 2, 'max_runtime_minutes': 1}}
    requests = [
        {'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'},
        {'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call', 'params': {'name': 'covalent_prepare_execution',
            'arguments': {'tool': 'covalent_submit_job', 'arguments': args}}},
        {'jsonrpc': '2.0', 'id': 3, 'method': 'tools/call', 'params': {'name': 'covalent_submit_job', 'arguments': args}},
        {'jsonrpc': '2.0', 'id': 4, 'method': 'tools/call', 'params': {'name': 'covalent_ligand_first_campaign',
            'arguments': {'action': 'start', 'human_approved': True, '_native_prepare': True}}},
    ]
    monkeypatch.setattr(server.sys, 'stdin', io.StringIO(''.join(json.dumps(request) + '\n' for request in requests)))
    assert server.main() == 0
    responses = [json.loads(line)['result'] for line in capsys.readouterr().out.splitlines()]
    tool = next(item for item in responses[0]['tools'] if item['name'] == 'covalent_prepare_execution')
    assert tool['annotations']['readOnlyHint'] is True
    assert responses[1]['structuredContent']['status'] == 'NATIVE_APPROVAL_PREPARED'
    assert responses[1]['structuredContent']['compute_submitted'] is False
    assert responses[2]['isError'] and 'receipt required' in responses[2]['structuredContent']['error']
    assert responses[3]['isError'] and 'Legacy preparation flag rejected' in responses[3]['structuredContent']['error']
    assert not list((tmp_path / 'state/jobs').glob('*/spec.json'))
    assert not (tmp_path / 'state/ligand_controllers/runs').exists()
