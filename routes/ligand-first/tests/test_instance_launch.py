import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / 'ops/instance_launch.py'
spec = importlib.util.spec_from_file_location('instance_launch', path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_recovery_requires_exact_failure_and_no_live_worker_or_claim(tmp_path):
    job, proc = tmp_path/'job', tmp_path/'proc'
    job.mkdir(); proc.mkdir()
    assert module.launch_observation(job, 'original-run', proc)['state'] == 'NOT_LAUNCHED'
    log = job/'instance-launch.log'
    log.write_text('unknown failure')
    assert module.launch_observation(job, 'original-run', proc)['recovery_eligible'] is False
    log.write_text('Traceback (most recent call last):\ninstance_vs_claim.py\n'
                   'tree = InstanceProcessTree()\n' + module.THREAD_ERROR + '\n')
    observed = module.launch_observation(job, 'original-run', proc)
    assert observed['recovery_eligible'] is True
    assert observed['worker_never_claimed'] is True
    process = proc/'123'; process.mkdir()
    (process/'cmdline').write_bytes(b'python\0-c\0code\0run-instance\0original-run\0')
    assert module.launch_observation(job, 'original-run', proc) == {'state':'STARTING','worker_pids':[123]}
    (process/'cmdline').write_bytes(b'python\0-c\0run-instance original-run embedded string\0inspect-instance\0original-run\0')
    assert module.launch_observation(job, 'original-run', proc)['recovery_eligible'] is True
    (job/'instance_worker_claim').mkdir()
    assert module.launch_observation(job, 'original-run', proc)['state'] == 'CLAIMED'
    (job/'instance_worker_claim').rmdir()
    (job/'instance-bootstrap-recovery').mkdir()
    assert module.launch_observation(job, 'original-run', proc)['recovery_eligible'] is False


def test_recovery_marker_is_exclusive_and_preserves_original_log(tmp_path, monkeypatch):
    log = tmp_path/'instance-launch.log'; log.write_text('original evidence')
    monkeypatch.setattr(module, 'launch_observation', lambda *args: {
        'recovery_eligible': True, 'log_sha256': 'a'*64})
    path = module.reserve_recovery(tmp_path, 'original-run', 'b'*64)
    assert path == tmp_path/'instance-bootstrap-recovery/launch.log'
    assert log.read_text() == 'original evidence'
    with pytest.raises(FileExistsError):
        module.reserve_recovery(tmp_path, 'original-run', 'b'*64)


def test_terminal_recovery_requires_exact_selected_evidence(tmp_path, monkeypatch):
    import json
    job, proc = tmp_path/'job', tmp_path/'proc'
    job.mkdir(); proc.mkdir()
    (job/'instance-launch.log').write_text('original retained log')
    (job/'instance-bootstrap-terminal.json').write_text(json.dumps({
        'run_id':'original-run', 'error':module.THREAD_ERROR}))
    observe = module.launch_observation
    proof = observe(job, 'original-run', proc)
    monkeypatch.setattr(module, 'launch_observation', lambda j, r: observe(j, r, proc))
    monkeypatch.delenv('COVALENT_INSTANCE_RECOVER_TERMINAL', raising=False)
    with pytest.raises(ValueError, match='explicitly selected'):
        module.reserve_recovery(job, 'original-run', 'a'*64)
    monkeypatch.setenv('COVALENT_INSTANCE_RECOVER_TERMINAL', proof['terminal_sha256'])
    marker = module.reserve_recovery(job, 'original-run', 'a'*64)
    assert marker.parent.name == proof['terminal_sha256']
    assert observe(job, 'original-run', proc)['recovery_eligible'] is False
    with pytest.raises(ValueError, match='verified pre-claim'):
        module.reserve_recovery(job, 'original-run', 'a'*64)
