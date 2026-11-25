import json
import os
from pathlib import Path
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from covscreen.execution import execute_persisted_job
from covscreen.tool_api import cancel_job_tool, wait_job_tool
from test_job_cancellation import queued


def test_reconnected_worker_does_not_launch_again_and_cancelled_run_stays_terminal(tmp_path):
    registry, run = queued(tmp_path)
    spec = registry.load_spec(run)
    spec.provenance['worker_backend'] = 'local'
    spec.command = [sys.executable, '-c',
        "from pathlib import Path; import time; "
        "f=Path('launches').open('a'); f.write('launch\\n'); f.close(); "
        "Path('started').write_text('yes'); time.sleep(20)"]
    registry.write_spec(spec)
    with ThreadPoolExecutor(1) as pool:
        worker = pool.submit(execute_persisted_job, tmp_path, run)
        try:
            deadline = time.monotonic() + 5
            while not (tmp_path / 'started').exists():
                assert time.monotonic() < deadline
                time.sleep(.02)
            # A distinct process models host/dispatcher reconnection, not a
            # mocked registry or a second call hidden inside the same worker.
            code = ('import json,sys; from covscreen.execution import execute_persisted_job; '
                    'print(execute_persisted_job(sys.argv[1],sys.argv[2]).model_dump_json())')
            observed = subprocess.run([sys.executable, '-c', code, str(tmp_path), run],
                env={**os.environ, 'PYTHONPATH': str(Path(__file__).parents[1] / 'src')},
                capture_output=True, text=True, check=True, timeout=5)
            assert json.loads(observed.stdout)['state'] == 'RUNNING'
            observation = wait_job_tool(tmp_path, run, timeout_seconds=0)
            assert observation['terminal'] is False
            assert observation['worker_observation']['status'] == 'LOCK_HELD_OWNER_UNVERIFIED'
            assert observation['worker_observation']['engine_liveness_verified'] is False
            assert observation['next_action'] == 'wait_again_on_same_run_id'
        finally:
            cancel_job_tool(tmp_path, run, human_approved=True)
            final = worker.result(timeout=10)
    assert final.state == 'CANCELLED'
    before = (registry.job_dir(run) / 'result.json').read_bytes()
    assert execute_persisted_job(tmp_path, run).state == 'CANCELLED'
    assert (registry.job_dir(run) / 'result.json').read_bytes() == before
    assert (tmp_path / 'launches').read_text().splitlines() == ['launch']


@pytest.mark.parametrize('state', ['RUNNING', 'COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED'])
def test_stale_running_or_terminal_record_never_authorizes_relaunch(tmp_path, state):
    registry, run = queued(tmp_path)
    result = registry.load_result(run)
    result.state = state
    registry.write_result(result)
    before = (registry.job_dir(run) / 'result.json').read_bytes()
    observed = execute_persisted_job(tmp_path, run)
    assert observed.state == state
    assert (registry.job_dir(run) / 'result.json').read_bytes() == before
    assert not (tmp_path / 'started').exists()


def test_existing_terminal_state_with_wrong_identity_is_not_reused(tmp_path):
    registry, run = queued(tmp_path)
    result = registry.load_result(run)
    result.state = 'COMPLETED'
    result.project_id = 'different-project'
    registry.write_result(result)
    with pytest.raises(ValueError, match='identity'):
        execute_persisted_job(tmp_path, run)
    assert not (tmp_path / 'started').exists()
