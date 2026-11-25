import json
import os
from pathlib import Path
import subprocess
import sys

from covscreen.mcp.task_guard import guarded_call


def test_failure_blocks_a_new_process_without_executing_next_tool(tmp_path):
    result = guarded_call(tmp_path, "health", {}, lambda *_: {"status": "FAIL"},
                          environment={"COVALENT_STOP_ON_TOOL_FAILURE": "1"})
    assert result["status"] == "FAIL"
    script = """
import json, sys
from pathlib import Path
from covscreen.mcp.task_guard import guarded_call
root = Path(sys.argv[1])
def execute(*args):
    (root / 'executed').write_text('bad')
    return {'status': 'PASS'}
print(json.dumps(guarded_call(root, 'next', {}, execute,
    environment={'COVALENT_STOP_ON_TOOL_FAILURE':'1'})))
"""
    run = subprocess.run([sys.executable, "-c", script, str(tmp_path)],
                         check=True, text=True, capture_output=True,
                         env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")})
    assert json.loads(run.stdout)["status"] == "FAIL"
    assert not (tmp_path / "executed").exists()


def test_exception_stops_but_success_does_not(tmp_path):
    env = {"COVALENT_STOP_ON_TOOL_FAILURE": "1"}
    assert guarded_call(tmp_path, "plan", {}, lambda *_: {"status": "PLANNED"}, environment=env)["status"] == "PLANNED"
    def broken(*args):
        raise ValueError("invalid input")
    assert guarded_call(tmp_path, "broken", {}, broken, environment=env)["status"] == "FAIL"
    assert (tmp_path / "state/task_guard/stopped.json").is_file()


def test_existing_workflows_are_unchanged_without_opt_in(tmp_path):
    assert guarded_call(tmp_path, "health", {}, lambda *_: {"status": "FAIL"}, environment={})["status"] == "FAIL"
    assert not (tmp_path / "state/task_guard").exists()


def test_classified_reconnection_preserves_stop_but_can_inspect(tmp_path):
    from covscreen.mcp.server import dispatch
    from test_covalent_site_discovery import prediction
    run_id, _ = prediction(tmp_path)
    env = {'COVALENT_ERROR_POLICY': 'classified-v1'}
    guarded_call(tmp_path, 'covalent_get_job', {'run_id': 'missing-run'}, dispatch, environment=env)
    marker = tmp_path / 'state/task_guard/stopped.json'
    original = marker.read_bytes()
    script = '''
import json, sys
from pathlib import Path
from covscreen.mcp.server import dispatch
from covscreen.mcp.task_guard import guarded_call
root = Path(sys.argv[1])
env = {'COVALENT_ERROR_POLICY': 'classified-v1'}
observed = guarded_call(root, 'covalent_get_job', {'run_id': sys.argv[2]}, dispatch, environment=env)
blocked = guarded_call(root, 'covalent_submit_job', {}, dispatch, environment=env)
print(json.dumps({'observed': observed, 'blocked': blocked}))
'''
    run = subprocess.run([sys.executable, '-c', script, str(tmp_path), run_id],
                         check=True, text=True, capture_output=True,
                         env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')})
    result = json.loads(run.stdout)
    assert result['observed']['job']['run_id'] == run_id
    assert result['observed']['task_guard']['execution_blocked'] is True
    assert result['blocked']['status'] == 'FAIL'
    assert marker.read_bytes() == original
