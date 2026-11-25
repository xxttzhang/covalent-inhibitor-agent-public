"""Real one-minute budget test; no simulated clocks or scientific engine."""
import os
import signal
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from covscreen.execution import execute_persisted_job
from test_job_cancellation import queued


def test_timeout_stops_child_that_ignores_sigterm_after_wrapper_exits(tmp_path):
    registry, run = queued(tmp_path)
    child = (
        "import os,signal,time; from pathlib import Path; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "Path('child.pid').write_text(str(os.getpid())); "
        "f=Path('heartbeat').open('a', buffering=1); "
        "[(f.write('tick\\n'), time.sleep(.05)) for _ in range(3600)]"
    )
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable,'-c',{child!r}]).wait()"
    spec = registry.load_spec(run)
    spec.command = [sys.executable, '-c', parent]
    registry.write_spec(spec)
    with ThreadPoolExecutor(1) as pool:
        worker = pool.submit(execute_persisted_job, tmp_path, run)
        try:
            deadline = time.monotonic() + 10
            while not (tmp_path / 'heartbeat').exists():
                assert time.monotonic() < deadline, 'synthetic child did not start'
                time.sleep(.02)
            final = worker.result(timeout=80)
            assert final.state == 'TIMED_OUT'
            assert final.return_code is not None and final.return_code != 0
            assert final.failure_reason == 'resource_budget_runtime_exceeded'
            assert registry.load_result(run).state == 'TIMED_OUT'
            # A killed orphan may remain a zombie until the host reaps it.
            # Verify that it has stopped executing, without mistaking a zombie
            # PID for a still-running engine or relying on sandbox-blocked ps.
            before = (tmp_path / 'heartbeat').stat().st_size
            time.sleep(.3)
            assert (tmp_path / 'heartbeat').stat().st_size == before
        finally:
            # Clean up the isolated test group even against the unfixed worker.
            pid = registry.load_result(run).engine_pid
            if pid is not None and pid != os.getpgrp():
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
