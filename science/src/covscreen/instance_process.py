"""Linux process cleanup for a dedicated, single-threaded instance worker."""

import ctypes
import os
from pathlib import Path
import signal
import subprocess
import time


class InstanceProcessCancelled(Exception):
    """Cancellation requested and the worker-owned tree was reaped."""


class InstanceRuntimeExceeded(TimeoutError):
    """The approved runtime elapsed and cleanup succeeded."""


class InstanceProcessTree:
    """Adopt and reap detached descendants of a worker-owned job server."""

    def __init__(self):
        pid = os.getpid()
        self._pid = pid
        self._tasks = Path(f"/proc/{pid}/task")
        self._children = self._tasks / str(pid) / "children"
        self._check_owner()
        if self._children.read_text().split():
            raise RuntimeError("Instance process supervision requires a dedicated worker")
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.prctl(36, 1, 0, 0, 0) != 0:  # PR_SET_CHILD_SUBREAPER
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))

    def _check_owner(self) -> None:
        if os.getpid() != self._pid or {task.name for task in self._tasks.iterdir()} != {
            str(self._pid)
        }:
            raise RuntimeError("Instance process supervision requires its original single-threaded worker")

    def run(self, argv, *, deadline, cancelled, cpu_count=None, **popen_options) -> int:
        """Run one worker under a monotonic deadline and a durable cancel check."""
        self._check_owner()
        if self._children.read_text().split():
            raise RuntimeError("Instance worker has outstanding children")
        if cpu_count is not None:
            available = sorted(os.sched_getaffinity(0))
            if type(cpu_count) is not int or not 1 <= cpu_count <= len(available):
                raise ValueError("Instance CPU budget exceeds available affinity")
            cores = ",".join(str(core) for core in available[:cpu_count])
            argv = ["/usr/bin/taskset", "-c", cores, *argv]

        stopping = False

        def request_stop(_signum, _frame):
            nonlocal stopping
            stopping = True

        def check_budget():
            if stopping or cancelled():
                raise InstanceProcessCancelled()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstanceRuntimeExceeded("Instance worker budget exhausted")
            return remaining

        previous_handler = signal.signal(signal.SIGTERM, request_stop)
        try:
            check_budget()
            process = subprocess.Popen(argv, **popen_options)
            while True:
                remaining = check_budget()
                try:
                    returncode = process.wait(timeout=min(remaining, 0.2))
                    break
                except subprocess.TimeoutExpired:
                    continue
        finally:
            try:
                self.stop()
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
        check_budget()
        return returncode

    def stop(self, timeout: float = 5.0) -> None:
        """Kill direct children, repeating as their descendants are adopted."""
        self._check_owner()
        deadline = time.monotonic() + timeout
        while True:
            # Unreaped direct children retain their PID until waitpid below.
            for child in self._children.read_text().split():
                try:
                    os.kill(int(child), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            try:
                while os.waitpid(-1, os.WNOHANG)[0] != 0:
                    pass
            except ChildProcessError:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError("Instance worker still has unreaped children")
            time.sleep(0.05)
