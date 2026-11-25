import pytest

from covscreen.tool_api import get_job_tool, wait_job_tool
from test_wait_job import register


@pytest.mark.parametrize('lock_exists', [False, True])
def test_orphan_running_record_requests_reconciliation_without_mutation(tmp_path, lock_exists):
    registry, result = register(tmp_path, 'RUNNING')
    spec = registry.load_spec(result.run_id)
    spec.provenance['worker_backend'] = 'local'
    registry.write_spec(spec)
    lock = registry.job_dir(result.run_id) / 'worker.lock'
    if lock_exists:
        lock.write_text('')
    before = {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}
    for response in (get_job_tool(tmp_path, result.run_id),
                     wait_job_tool(tmp_path, result.run_id, timeout_seconds=0)):
        assert response['job']['state'] == 'RUNNING'
        assert response['worker_observation']['status'] == 'NO_WORKER_LOCK_OBSERVED'
        assert response['worker_observation']['reconciliation_required'] is True
        assert response['worker_observation']['engine_liveness_verified'] is False
        assert response['worker_observation']['automatic_resubmission_allowed'] is False
    assert response['next_action'] == 'reconcile_original_job_do_not_resubmit'
    assert not response['terminal']
    assert before == {str(p): p.read_bytes() for p in tmp_path.rglob('*') if p.is_file()}


@pytest.mark.parametrize('state,backend', [('QUEUED', 'local'), ('RUNNING', 'external-inspire'),
    ('RUNNING', 'external-hpc'), ('COMPLETED', 'local')])
def test_no_local_claim_probe_for_queued_external_or_terminal(tmp_path, state, backend):
    registry, result = register(tmp_path, state)
    spec = registry.load_spec(result.run_id)
    spec.provenance['worker_backend'] = backend
    registry.write_spec(spec)
    response = get_job_tool(tmp_path, result.run_id)
    assert response['worker_observation']['status'] == 'NOT_CHECKED'
    assert not (registry.job_dir(result.run_id) / 'worker.lock').exists()


def test_worker_exit_does_not_leave_wait_claiming_normal_running(tmp_path):
    import subprocess
    import sys
    import select
    registry, result = register(tmp_path, 'RUNNING')
    spec = registry.load_spec(result.run_id)
    spec.provenance['worker_backend'] = 'local'
    registry.write_spec(spec)
    lock = registry.job_dir(result.run_id) / 'worker.lock'
    code = "import fcntl,sys; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('ready',flush=True); sys.stdin.read(1)"
    worker = subprocess.Popen([sys.executable, '-c', code, str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([worker.stdout], [], [], 3)[0]
        assert worker.stdout.readline().strip() == 'ready'
        active = wait_job_tool(tmp_path, result.run_id, timeout_seconds=0)
        assert active['worker_observation']['status'] == 'LOCK_HELD_OWNER_UNVERIFIED'
        worker.communicate(input='x', timeout=3)
        assert worker.returncode == 0
        stopped = wait_job_tool(tmp_path, result.run_id, timeout_seconds=0)
        assert stopped['worker_observation']['status'] == 'NO_WORKER_LOCK_OBSERVED'
        assert stopped['next_action'] == 'reconcile_original_job_do_not_resubmit'
        assert stopped['job']['state'] == 'RUNNING'
        assert stopped['terminal'] is False
        assert registry.load_result(result.run_id).state == 'RUNNING'
    finally:
        if worker.poll() is None:
            worker.kill()
        worker.communicate(timeout=3)
