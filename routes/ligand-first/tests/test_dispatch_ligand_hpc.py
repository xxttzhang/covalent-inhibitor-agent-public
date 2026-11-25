import importlib.util
from pathlib import Path
import pytest
import shlex


@pytest.fixture(autouse=True)
def legacy_test_paths(monkeypatch):
    monkeypatch.setenv('COVSCREEN_LEGACY_BASE', '/inspire/test/legacy')
    monkeypatch.setenv('COVSCREEN_LEGACY_ROOT', '/inspire/test/legacy/study')


def test_missing_legacy_configuration_fails_closed(monkeypatch):
    for key in ('COVSCREEN_LEGACY_BASE', 'COVSCREEN_LEGACY_ROOT',
                'COVALENT_SCIENCE_RUNTIME_PROFILE'):
        monkeypatch.delenv(key, raising=False)
    path = Path(__file__).resolve().parents[1] / 'ops/dispatch_ligand_hpc.py'
    spec = importlib.util.spec_from_file_location('dispatch_no_legacy', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with pytest.raises(ValueError, match='normalized absolute'):
        module.configure_root(record={'run_id': 'historical'})
    module.configure_root('/inspire/test/explicit', required=True)
    with pytest.raises(ValueError, match='COVSCREEN_LEGACY_BASE'):
        module.command(['inspect', 'historical'])


def test_instance_requires_fixed_host_runtime_without_legacy_dependencies(monkeypatch, tmp_path):
    import json
    path = Path(__file__).resolve().parents[1] / 'ops/dispatch_ligand_hpc.py'
    spec = importlib.util.spec_from_file_location('dispatch_runtime', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delenv('COVALENT_SCIENCE_RUNTIME_PROFILE', raising=False)
    with pytest.raises(ValueError, match='host runtime profile'):
        module.command(['inspect-instance', 'approved-run'])
    profile = tmp_path / 'runtime.json'
    runtime = {'python': '/inspire/test/environment/host/bin/python', 'sha256': 'a'*64,
               'lock': '/inspire/test/requirements.lock', 'lock_sha256': 'b'*64}
    profile.write_text(json.dumps(runtime))
    profile.chmod(0o600)
    monkeypatch.setenv('COVALENT_SCIENCE_RUNTIME_PROFILE', str(profile))
    tokens = shlex.split(module.command(['inspect-instance', 'approved-run']))
    assert runtime['python'] in tokens
    assert not any('carsidock_gpu_env' in token or 'python311_runtime' in token for token in tokens)
    assert 'PYTHONNOUSERSITE=1' in tokens
    assert 'OPENBLAS_NUM_THREADS=1' in tokens
    assert 'OMP_NUM_THREADS=1' in tokens
    assert 'MKL_NUM_THREADS=1' in tokens
    assert 'COVALENT_DISPATCH_RUNTIME=' + json.dumps(runtime, separators=(',', ':')) in tokens
    monkeypatch.setenv('COVALENT_LIGAND_VS_INSTANCE', 'asgbie4')
    migrated = shlex.split(module.command(['inspect-instance', 'approved-run']))
    assert 'COVALENT_DISPATCH_INSTANCE=asgbie4' in migrated
    assert 'COVALENT_LIGAND_VS_INSTANCE=asgbie4' in migrated
    monkeypatch.setenv('COVALENT_LIGAND_VS_INSTANCE', 'asgbie2')
    with pytest.raises(ValueError, match='Unregistered Ligand'):
        module.command(['inspect-instance', 'approved-run'])
    monkeypatch.delenv('COVALENT_LIGAND_VS_INSTANCE')
    profile.chmod(0o644)
    with pytest.raises(ValueError, match='owner-only'):
        module.command(['inspect-instance', 'approved-run'])


@pytest.mark.parametrize('action,seconds', [('inspect-instance', 300), ('launch-instance', 300),
    ('recover-instance', 300), ('inspect', 60)])
def test_remote_failure_retains_actual_stderr(monkeypatch, action, seconds):
    import json
    import subprocess
    path = Path(__file__).resolve().parents[1] / 'ops/dispatch_ligand_hpc.py'
    spec = importlib.util.spec_from_file_location('dispatch_diagnostics', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, 'command', lambda args, *, instance_name: shlex.join([
        __import__('sys').executable, '-c', 'import sys; sys.stderr.write("runtime version mismatch"); sys.exit(7)']))
    def cli(args, *, timeout):
        assert args[args.index('--timeout') + 1] == str(seconds)
        assert timeout == seconds + 50
        # Execute the actual envelope locally; no remote service or scientific job.
        result = subprocess.run(shlex.split(args[-1]), capture_output=True, text=True, check=True)
        return {'returncode': 0, 'output': result.stdout}
    monkeypatch.setattr(module, 'call', cli)
    with pytest.raises(RuntimeError, match='runtime version mismatch') as error:
        module.remote([action, 'approved-run'])
    assert json.loads(str(error.value))['returncode'] == 7


def test_dispatch_uses_signed_record_budget_without_starting_cli():
    path = (Path(__file__).resolve().parents[3] / "science") / 'ops/dispatch_ligand_hpc.py'
    spec = importlib.util.spec_from_file_location('dispatch', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = module.create_args({'name': 'test-no-submit', 'run_id': 'new-run-001', 'spec_sha256': 'a' * 64,
        'minutes': 30, 'budget': {'cpu_count': 8, 'memory_gb': 32}}, 'test-image')
    assert command[command.index('--quota') + 1] == '0,8,32'
    assert command[command.index('--cpus-per-task') + 1] == '8'
    assert command[command.index('--memory-per-cpu') + 1] == '4'
    assert 'OMP_NUM_THREADS=8' in command[-1]
    record = {'name': 'large-test', 'run_id': 'new-run-002', 'spec_sha256': 'b' * 64,
              'minutes': 30, 'budget': {'cpu_count': 55, 'memory_gb': 128}}
    command = module.create_args(record, 'test-image')
    assert command[command.index('--memory-per-cpu') + 1] == '2'
    assert command[command.index('--quota') + 1] == '0,55,128'
    for memory in (16, 128.5):
        record['budget']['memory_gb'] = memory
        with pytest.raises(ValueError, match='compatible budget'):
            module.create_args(record, 'test-image')


def test_explicit_roots_survive_receipts_and_never_replace_historical_default():
    path = (Path(__file__).resolve().parents[3] / "science") / 'ops/dispatch_ligand_hpc.py'
    spec = importlib.util.spec_from_file_location('dispatch_roots', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = '/inspire/hdd/project/test/new-science'
    with pytest.raises(ValueError, match='explicit --root'):
        module.configure_root(required=True)
    module.configure_root(root, required=True)
    record = module.bind_record({'run_id': 'new', 'spec_sha256': 'a' * 64})
    assert record['dispatch_root'] == root
    tokens = shlex.split(module.command(['inspect', 'new']))
    assert root + '/scripts/ligand_external_hpc.py' in tokens
    assert any(item.startswith('PYTHONPATH=' + root + '/src:') for item in tokens)
    assert module.configure_root(record=record) == root
    with pytest.raises(ValueError, match='frozen receipt'):
        module.configure_root('/inspire/hdd/project/test/other', record)
    assert module.configure_root(record={'run_id': 'historical'}) == module.LEGACY_ROOT
    with pytest.raises(ValueError, match='frozen receipt'):
        module.bind_record(record)
    for invalid in ['/tmp/science', '/inspire/a/../b', '/inspire//a', '/inspire/a\n']:
        with pytest.raises(ValueError, match='normalized'):
            module.configure_root(invalid)
    identity = {'run_id': 'new', 'spec_sha256': 'a' * 64, 'name': 'approved-job'}
    module.verify_record(identity, dict(identity))
    for key in identity:
        with pytest.raises(ValueError, match='identity differs'):
            module.verify_record(identity, {**identity, key: 'different'})


def test_isolated_binding_is_exact_and_persisted(monkeypatch, tmp_path):
    import json
    import os
    path = Path(__file__).resolve().parents[1] / 'ops/dispatch_ligand_hpc.py'
    spec = importlib.util.spec_from_file_location('dispatch_bound', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    root = '/inspire/validation/science'
    binding = dict(data_root=root, release_root='/inspire/validation/releases/' + 'a'*64,
                   release_id='a'*64, manifest_sha256='b'*64)
    module.configure_root(root)
    module.configure_release_binding(binding)
    receipt = module.bind_record({'run_id':'approved-run'})
    assert receipt['release_binding'] == binding
    assert module.configure_release_binding(record=receipt) == binding
    with pytest.raises(ValueError, match='frozen receipt'):
        module.configure_release_binding({**binding, 'manifest_sha256':'c'*64}, receipt)
    with pytest.raises(ValueError, match='mismatch'):
        module.configure_release_binding({**binding, 'data_root':'/inspire/another'})
    tokens = shlex.split(module.command(['inspect','approved-run']))
    encoded = next(t.split('=',1)[1] for t in tokens if t.startswith('COVALENT_DISPATCH_BINDING='))
    assert json.loads(encoded) == binding
    # Execute the real launcher with an existing spec but a different bound release.
    # It must reject before loading any release code or invoking a worker.
    project = tmp_path / 'science'
    job = project / 'state/jobs/approved-run'
    job.mkdir(parents=True)
    (job/'spec.json').write_text(json.dumps({'provenance':{'source_release':{
        'root':binding['release_root'], 'release_id':'a'*64, 'manifest_sha256':'c'*64}}}))
    monkeypatch.setenv('COVALENT_DISPATCH_BINDING', json.dumps({**binding, 'data_root':str(project)}))
    monkeypatch.setattr('sys.argv', ['launcher',str(project/'scripts/ligand_external_inspire.py'),'inspect','approved-run'])
    with pytest.raises(ValueError, match='source differs'):
        exec(compile(module.RELEASE_LAUNCH, '<launcher>', 'exec'), {})


def test_launch_preflight_validates_once_and_checks_digest(monkeypatch, tmp_path):
    import ast
    import subprocess
    import sys
    import types
    path = Path(__file__).resolve().parents[1] / 'ops/dispatch_ligand_hpc.py'
    spec = importlib.util.spec_from_file_location('dispatch_once', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    # Execute both real preflight/launch blocks with an inert worker launcher.
    tree = ast.parse(module.RELEASE_LAUNCH)
    blocks = [node for node in tree.body if isinstance(node, ast.If)
              and ast.unparse(node.test).startswith('args[:1] in')]
    assert len(blocks) == 2
    program = compile(ast.Module(body=blocks, type_ignores=[]), '<launch-preflight>', 'exec')
    inspections, launches = [], []
    def inspect(*args):
        inspections.append(args)
        return {'spec_sha256': 'signed', 'state': 'QUEUED', 'worker_claimed': False}
    monkeypatch.setitem(sys.modules, 'covscreen.instance_claim', types.SimpleNamespace(inspect=inspect))
    monkeypatch.setitem(sys.modules, 'covscreen.execution', types.SimpleNamespace(_execute_claimed_job=None))
    monkeypatch.setitem(sys.modules, 'covscreen.instance_process', types.SimpleNamespace(InstanceProcessTree=lambda: None))
    monkeypatch.setattr(subprocess, 'Popen', lambda *args, **kwargs: launches.append(args))
    monkeypatch.setattr(sys, 'orig_argv', ['python', '-c', 'inert-worker'])
    monkeypatch.setattr(sys, 'argv', ['python', 'entry'])
    job = tmp_path / 'state/jobs/run'
    job.mkdir(parents=True)
    values = {'project': tmp_path, 'sys': sys, 'json': __import__('json'), 'instance': 'asgbie4',
              'launch_observation': lambda *args: {}}
    with pytest.raises(ValueError, match='exact unclaimed'):
        exec(program, {**values, 'args': ['launch-instance', 'run', '--digest', 'wrong']})
    assert len(inspections) == 1 and not launches
    assert not (job / 'instance-launch.log').exists()
    inspections.clear()
    with pytest.raises(SystemExit) as stopped:
        exec(program, {**values, 'args': ['launch-instance', 'run', '--digest', 'signed']})
    assert stopped.value.code == 0
    assert len(inspections) == 1 and len(launches) == 1
    assert (job / 'instance-launch.log').exists()
