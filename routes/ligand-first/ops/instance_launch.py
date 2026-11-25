"""Observe bootstrap failures without treating a stored QUEUED job as running."""
import hashlib
import json
import os
from pathlib import Path

THREAD_ERROR = 'RuntimeError: Instance process supervision requires its original single-threaded worker'


def launch_observation(job: Path, run_id: str, proc: Path = Path('/proc')) -> dict:
    log = job / 'instance-launch.log'
    if not log.exists():
        return {'state': 'NOT_LAUNCHED'}
    if log.is_symlink() or not log.is_file():
        raise ValueError('Invalid instance launch log')
    # Exact argv elements, not substring matching of the embedded launcher code.
    # A failed read is uncertainty, never permission to restart.
    live = []
    for process in proc.iterdir():
        if not process.name.isdigit():
            continue
        try:
            argv = (process / 'cmdline').read_bytes().split(b'\0')
        except FileNotFoundError:
            continue
        if run_id.encode() in argv and b'run-instance' in argv:
            live.append(int(process.name))
    if live:
        return {'state': 'STARTING', 'worker_pids': live}
    if (job / 'instance_worker_claim').exists():
        return {'state': 'CLAIMED'}
    terminal = job / 'instance-bootstrap-terminal.json'
    if terminal.exists():
        if terminal.is_symlink():
            raise ValueError('Invalid bootstrap terminal evidence')
        value = json.loads(terminal.read_text())
        if value.get('run_id') != run_id:
            raise ValueError('Bootstrap terminal identity mismatch')
        checksum = hashlib.sha256(terminal.read_bytes()).hexdigest()
        eligible = (value['error'] == THREAD_ERROR
                    and not (job / 'instance-bootstrap-recoveries' / checksum).exists())
        return {'state': 'LAUNCH_FAILED', 'error': value['error'], 'recovery_eligible': eligible,
                'terminal_sha256': checksum, 'worker_never_claimed': True}
    raw = log.read_bytes()
    # Narrow migration path for the historical launcher, which did not persist
    # process identity. Requires a completed, exact pre-claim exception AND no
    # matching process AND no worker claim. Arbitrary logs/timeouts never qualify.
    text = raw.decode('utf-8', errors='replace').strip()
    if (text.startswith('Traceback (most recent call last):') and text.endswith(THREAD_ERROR)
            and 'instance_vs_claim.py' in text and 'tree = InstanceProcessTree()' in text
            and not (job / 'instance-bootstrap-recovery').exists()):
        return {'state': 'LAUNCH_FAILED', 'error': THREAD_ERROR, 'recovery_eligible': True,
                'log_sha256': hashlib.sha256(raw).hexdigest(), 'worker_never_claimed': True}
    return {'state': 'LAUNCH_UNCERTAIN', 'recovery_eligible': False,
            'error': 'No live worker or terminal bootstrap proof; preserve original task and inspect.'}


def reserve_recovery(job: Path, run_id: str, digest: str) -> Path:
    observation = launch_observation(job, run_id)
    if observation.get('recovery_eligible') is not True:
        raise ValueError('Bootstrap recovery requires verified pre-claim failure')
    if observation.get('terminal_sha256'):
        # A later explicitly selected recovery consumes this exact terminal
        # witness once. Prior recovery evidence and original logs stay intact.
        if os.environ.get('COVALENT_INSTANCE_RECOVER_TERMINAL') != observation['terminal_sha256']:
            raise ValueError('Terminal recovery requires the explicitly selected failure witness')
        folder = job / 'instance-bootstrap-recoveries'
        folder.mkdir(exist_ok=True)
        marker = folder / observation['terminal_sha256']
    else:
        marker = job / 'instance-bootstrap-recovery'
    marker.mkdir()  # Exclusive one-time recovery. Never removed on failure.
    (marker / 'evidence.json').write_text(json.dumps({
        'run_id': run_id, 'spec_sha256': digest, 'observation': observation,
        'original_log_preserved': True,
    }))
    return marker / 'launch.log'
