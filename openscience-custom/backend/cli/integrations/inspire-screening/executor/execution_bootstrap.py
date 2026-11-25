"""Host-embedded startup guard for an already independently approved job.

Recheck frozen bytes after scheduler waiting, before sourcing release activation.
This is not a resource approval, license check, GPU test or scientific acceptance.
The host constructs the exact argv and authenticates its dispatch reservation.
"""
import base64
import hashlib
import json
import os
import re
import stat
import sys
import time
import types
from pathlib import Path


def checksum(path, limit=None):
    before = path.stat()
    if not stat.S_ISREG(before.st_mode) or limit is not None and before.st_size > limit:
        raise ValueError('Startup evidence must be a bounded regular file')
    value, size = hashlib.sha256(), 0
    with path.open('rb') as stream:
        opened = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise ValueError('Startup evidence changed before reading')
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            size += len(block)
            if size > before.st_size:
                raise ValueError('Startup evidence grew during reading')
            value.update(block)
    after = path.stat()
    if size != before.st_size or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
            after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        raise ValueError('Startup evidence changed during reading')
    return value.hexdigest()


def check(packet):
    if not isinstance(packet, dict) or set(packet) != {'root', 'release', 'manifest', 'checker_sha256',
            'plan_sha256', 'parent', 'task', 'argv'}:
        raise ValueError('Invalid startup guard packet')
    for name in ('release', 'manifest', 'checker_sha256', 'plan_sha256'):
        if not isinstance(packet[name], str) or not re.fullmatch(r'[a-f0-9]{64}', packet[name]):
            raise ValueError('Exact startup checksums required')
    for name, pattern in (('parent', r'screen-[a-f0-9]{24}'), ('task', r'refine-[a-f0-9]{24}')):
        if not isinstance(packet[name], str) or not re.fullmatch(pattern, packet[name]):
            raise ValueError('Invalid startup task identity')
    root = Path(packet['root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('Startup root must be canonical')
    release = root / 'releases' / packet['release']
    if release.resolve(strict=True) != release:
        raise ValueError('Startup release cannot be redirected')
    checker = release / 'code_release.py'
    if checker.is_symlink() or checksum(checker, 1024 * 1024) != packet['checker_sha256']:
        raise ValueError('Startup release checker changed')
    contents = checker.read_bytes()
    if hashlib.sha256(contents).hexdigest() != packet['checker_sha256']:
        raise ValueError('Startup release checker changed during reading')
    module = types.ModuleType('verified_startup_release')
    module.__file__ = str(checker)
    exec(compile(contents, str(checker), 'exec'), module.__dict__)
    verified = module.verify(release, packet['release'], packet['manifest'])
    source = root / 'tasks' / packet['parent'] / 'refinements' / packet['task'] / 'plan.json'
    if source.resolve(strict=True) != source or checksum(source, 1024 * 1024) != packet['plan_sha256']:
        raise ValueError('Staged startup plan changed')
    contents = source.read_bytes()
    if hashlib.sha256(contents).hexdigest() != packet['plan_sha256']:
        raise ValueError('Staged startup plan changed during reading')
    saved = json.loads(contents)
    plan = saved['plan']
    if (plan['id'] != packet['task'] or plan['parent_id'] != packet['parent'] or
            plan['code'] != {'release_id': packet['release'], 'manifest_sha256': packet['manifest']}):
        raise ValueError('Startup plan identity differs')
    deadline = plan.get('finish_before')
    if type(deadline) is not int or deadline <= int(time.time() * 1000):
        raise ValueError('Approved refinement deadline elapsed before engine startup')
    argv = packet['argv']
    engine = plan['engine']['name']
    entry = release / 'science/scripts' / ('run_af3_registered_batch.py' if engine == 'af3' else 'run_covdock_lo_registered.py')
    if (engine not in ('af3', 'covdock') or plan['runtime']['name'] != engine or
            not isinstance(argv, list) or not 10 <= len(argv) <= 100 or
            any(not isinstance(arg, str) or not arg or '\x00' in arg for arg in argv) or
            argv[:3] != [plan['runtime']['python'], '-B', str(entry)]):
        raise ValueError('Startup executable differs from frozen runtime')
    # argv is host-constructed and persisted with the dispatch command digest;
    # additionally bind the exact input identity guards at the execution boundary.
    if len(argv[3:]) % 2 and argv[-1] != '--run-data-pipeline':
        raise ValueError('Invalid startup argument vector')
    options = argv[3:-1] if argv[-1] == '--run-data-pipeline' else argv[3:]
    if len(options) % 2 or len(set(options[::2])) != len(options[::2]):
        raise ValueError('Ambiguous startup options')
    values = dict(zip(options[::2], options[1::2]))
    expected = {'--candidate-table': plan['input']['path'], '--expected-input-sha256': plan['input']['sha256'],
                '--expected-identity-sha256': plan['input']['identity_sha256'],
                '--expected-input-count': str(len(plan['input']['candidates'])),
                '--output-dir': str(source.parent / engine)}
    if any(values.get(name) != value for name, value in expected.items()):
        raise ValueError('Startup input, output or identity differs from approved scope')
    activation = release / 'science/environment/activate_carsidock.sh'
    for path in (checker, activation, entry, Path(plan['runtime']['python'])):
        if str(path) not in plan['assets']:
            raise ValueError('Required startup executable absent from frozen assets')
    for name, expected_hash in plan['assets'].items():
        path = Path(name)
        if not path.is_absolute() or not isinstance(expected_hash, str) or not re.fullmatch(r'[a-f0-9]{64}', expected_hash):
            raise ValueError('Invalid frozen runtime asset')
        if checksum(path) != expected_hash:
            raise ValueError('Runtime asset changed while queued; engine not started')
    if checksum(Path(plan['input']['path'])) != plan['input']['sha256']:
        raise ValueError('Selected input changed while queued; engine not started')
    if not os.access(argv[0], os.X_OK):
        raise ValueError('Frozen runtime interpreter is not executable')
    if (int(time.time() * 1000) >= deadline or checksum(source, 1024 * 1024) != packet['plan_sha256'] or
            module.verify(release, packet['release'], packet['manifest']) != verified):
        raise ValueError('Startup deadline or immutable evidence changed during verification')
    return plan, release, activation


def execute(packet):
    plan, release, activation = check(packet)
    excluded = {'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'BASH_ENV', 'ENV',
                'SCHRODINGER', 'SCHROD_LICENSE_FILE', 'SCHRODINGER_LICENSE_FILE', 'LM_LICENSE_FILE', 'JAX_PLATFORMS'}
    env = {name: value for name, value in os.environ.items() if name not in excluded}
    cpus = plan['resource']['quota'].split(',')[1]
    if not re.fullmatch(r'[1-9][0-9]*', cpus):
        raise ValueError('Invalid approved CPU quota')
    env.update({'PYTHONDONTWRITEBYTECODE': '1', 'OMP_NUM_THREADS': cpus, 'OPENBLAS_NUM_THREADS': cpus, 'MKL_NUM_THREADS': cpus})
    if plan.get('runtime_admission') is not None:
        if plan['runtime_admission'] != 'cpu-before-approval-gpu-at-start-v1':
            raise ValueError('Unsupported runtime admission contract')
        directory = Path(packet['root']) / 'tasks' / plan['parent_id'] / 'refinements' / plan['id']
        retain_engine_admission(plan, env, directory)
        # Model/SDK imports may take time; recheck assets and deadline before launch.
        check(packet)
    os.chdir(release / 'science')
    # No model shell interpolation; script and vector are positional arguments.
    shell = ('set -euo pipefail; source "$1" >/dev/null; shift; '
             'export PYTHONDONTWRITEBYTECODE=1 OMP_NUM_THREADS="$1" OPENBLAS_NUM_THREADS="$1" MKL_NUM_THREADS="$1"; '
             'shift; exec "$@"')
    os.execvpe('bash', ['bash', '-c', shell, 'refinement-execution', str(activation), cpus, *packet['argv']], env)


if __name__ == '__main__':
    if len(sys.argv) != 2 or len(sys.argv[1]) > 2 * 1024 * 1024:
        raise ValueError('Bounded encoded startup packet required')
    execute(json.loads(base64.b64decode(sys.argv[1], validate=True)))
