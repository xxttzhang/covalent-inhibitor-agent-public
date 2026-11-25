"""Trusted stdlib bootstrap for a bounded CPU-only staging command.

Run using isolated system Python. Verify the release checker before executing its
bytes, then verify the entire release before sourcing its activation script.
"""
import base64
import hashlib
import json
import os
import subprocess
import sys
import types
from pathlib import Path


def execute(packet):
    keys = {'root', 'release', 'manifest', 'checker_sha256', 'python', 'python_sha256',
            'action', 'digest', 'parent', 'task', 'payload'}
    if packet.get('action') == 'outputs':
        keys.add('evidence_sha256')
        if 'reader_revision' in packet:
            keys.update(('reader_revision', 'revision_controller'))
    if set(packet) != keys or packet['action'] not in ('freeze', 'read', 'outputs'):
        raise ValueError('Unsupported staging bootstrap packet')
    root = Path(packet['root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('Canonical execution root required')
    import re
    for field in ('release', 'manifest', 'checker_sha256', 'python_sha256', 'digest'):
        if not isinstance(packet[field], str) or not re.fullmatch(r'[a-f0-9]{64}', packet[field]):
            raise ValueError('Missing frozen checksum')
    if not re.fullmatch(r'screen-[a-f0-9]{24}', packet['parent']) or not re.fullmatch(r'refine-[a-f0-9]{24}', packet['task']):
        raise ValueError('Invalid frozen task identity')
    release = root / 'releases' / packet['release']
    if release.resolve(strict=True) != release:
        raise ValueError('Release must remain inside the canonical execution root')
    checker = release / 'code_release.py'
    if release.is_symlink() or checker.is_symlink() or not checker.is_file() or checker.stat().st_size > 1024 * 1024:
        raise ValueError('Release checker is not a bounded regular file')
    contents = checker.read_bytes()
    if hashlib.sha256(contents).hexdigest() != packet['checker_sha256']:
        raise ValueError('Release checker changed; no release code executed')
    module = types.ModuleType('verified_code_release')
    module.__file__ = str(checker)
    exec(compile(contents, str(checker), 'exec'), module.__dict__)
    verified = module.verify(release, packet['release'], packet['manifest'])
    if 'refinement_stage.py' not in verified['files']:
        raise ValueError('Release has no staging controller')
    python = Path(packet['python'])
    if not python.is_absolute() or not python.is_file() or not os.access(python, os.X_OK) or python.stat().st_size > 128 * 1024 * 1024:
        raise ValueError('Invalid frozen staging interpreter')
    if hashlib.sha256(python.read_bytes()).hexdigest() != packet['python_sha256']:
        raise ValueError('Staging interpreter changed')
    entry = 'refinement_readback.py' if packet['action'] == 'outputs' else 'refinement_stage.py'
    if entry not in verified['files']:
        raise ValueError('Release has no requested verified controller')
    args = [str(python), '-B', str(release / entry), packet['action'],
            '--root', str(root), '--digest', packet['digest']]
    if packet['action'] == 'freeze':
        if not isinstance(packet['payload'], str) or not 1 <= len(packet['payload']) <= 1024 * 1024:
            raise ValueError('Bounded frozen plan payload required')
        decoded = json.loads(base64.b64decode(packet['payload'], validate=True))
        if decoded.get('id') != packet['task'] or decoded.get('parent_id') != packet['parent']:
            raise ValueError('Bootstrap payload identity differs')
        args.extend(['--payload', packet['payload']])
    else:
        if packet['payload'] is not None:
            raise ValueError('Read-only staging must not carry a replacement plan')
        args.extend(['--parent', packet['parent'], '--task', packet['task']])
    if packet['action'] == 'outputs' and packet['evidence_sha256'] is not None:
        if not isinstance(packet['evidence_sha256'], str) or not re.fullmatch(r'[a-f0-9]{64}', packet['evidence_sha256']):
            raise ValueError('Invalid independently retained result checksum')
        args.extend(['--manifest-sha256', packet['evidence_sha256']])
    if 'reader_revision' in packet:
        controller = base64.b64decode(packet['revision_controller'], validate=True)
        if hashlib.sha256(controller).hexdigest() != '55a5af608175f64fb974f5a9fbcdfcbcd6f0fb8b9de65b766fa467ee90136390':
            raise ValueError('Unrecognized readback revision controller')
        invocation = dict(original_entry=str(release / entry), revision=packet['reader_revision'],
                          root=str(root), parent=packet['parent'], task=packet['task'],
                          expected=packet['digest'], witness=packet['evidence_sha256'])
        args = [str(python), '-B', '-c', controller.decode(), json.dumps(invocation)]
    excluded = {'PYTHONPATH', 'PYTHONHOME', 'LD_PRELOAD', 'LD_LIBRARY_PATH', 'SCHRODINGER',
                'SCHROD_LICENSE_FILE', 'SCHRODINGER_LICENSE_FILE', 'LM_LICENSE_FILE'}
    env = {name: value for name, value in os.environ.items() if name not in excluded}
    # Reapply CPU/thread settings after activation, without evaluating user shell.
    shell = ('set -euo pipefail; source "$1" >/dev/null; shift; '
             'export PYTHONDONTWRITEBYTECODE=1 CUDA_VISIBLE_DEVICES="" JAX_PLATFORMS=cpu '
             'OMP_NUM_THREADS=1 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1; exec "$@"')
    result = subprocess.run(['bash', '-c', shell, 'refinement-staging',
        str(release / 'science/environment/activate_carsidock.sh'), *args],
        cwd=release / 'science', env=env, stdin=subprocess.DEVNULL,
        capture_output=True, text=True, timeout=70)
    if result.returncode:
        raise RuntimeError('Verified staging worker failed; no compute submitted')
    if len(result.stdout.encode()) > (512 if packet['action'] == 'outputs' else 64) * 1024:
        raise ValueError('Staging response exceeds bounded size')
    return json.loads(result.stdout)


if __name__ == '__main__':
    if len(sys.argv) != 2 or len(sys.argv[1]) > 2 * 1024 * 1024:
        raise ValueError('Bounded encoded bootstrap packet required')
    print(json.dumps(execute(json.loads(base64.b64decode(sys.argv[1], validate=True))), ensure_ascii=False))
