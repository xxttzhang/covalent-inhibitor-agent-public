"""Read-only failed-run inventory. No imports from a failed/mutable runtime.

Returns paths, byte counts and hashes only, never log or molecular contents.
This is file identity evidence, not a scientific result or a retry permission.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


def inspect(packet):
    if set(packet) != {'root', 'parent', 'task', 'engine', 'digest', 'plan_sha256'}:
        raise ValueError('Invalid failed-run read request')
    for key, pattern in [('parent', r'screen-[a-f0-9]{24}'), ('task', r'refine-[a-f0-9]{24}'),
                         ('digest', r'[a-f0-9]{64}'), ('plan_sha256', r'[a-f0-9]{64}')]:
        if not isinstance(packet[key], str) or not re.fullmatch(pattern, packet[key]):
            raise ValueError('Invalid frozen identity')
    if packet['engine'] not in ('af3', 'covdock'):
        raise ValueError('Invalid failed-run engine')
    root = Path(packet['root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('Redirected failed-run root')
    directory = root / 'tasks' / packet['parent'] / 'refinements' / packet['task']
    if directory.resolve(strict=True) != directory:
        raise ValueError('Redirected failed-run directory')
    files, total = [], 0

    def read(file, keep=False):
        nonlocal total
        relative = file.relative_to(directory).as_posix()
        if re.search(r'[\\\x00-\x1f\x7f]', relative) or len(relative) > 2048 or file.resolve(strict=True) != file:
            raise ValueError('Invalid failed-run artifact path')
        before = file.lstat()
        if not stat.S_ISREG(before.st_mode) or before.st_size > 128 * 1024 ** 2:
            raise ValueError('Nonregular or oversized failed-run artifact')
        total += before.st_size
        if len(files) >= 2000 or total > 1024 ** 3:
            raise ValueError('Complete failure inventory exceeds bounds')
        checksum, contents, size = hashlib.sha256(), [], 0
        if keep and before.st_size > 1024 * 1024:
            raise ValueError('Oversized frozen plan')
        identity = lambda value: (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns)
        with file.open('rb') as stream:
            if identity(os.fstat(stream.fileno())) != identity(before):
                raise ValueError('Failure evidence changed before reading')
            for block in iter(lambda: stream.read(1024 * 1024), b''):
                size += len(block)
                if size > before.st_size:
                    raise ValueError('Failure evidence grew during reading')
                checksum.update(block)
                if keep:
                    contents.append(block)
        if size != before.st_size or identity(file.lstat()) != identity(before):
            raise ValueError('Failure evidence changed while reading')
        files.append({'name': relative, 'bytes': size, 'sha256': checksum.hexdigest()})
        return b''.join(contents)

    contents = read(directory / 'plan.json', keep=True)
    if hashlib.sha256(contents).hexdigest() != packet['plan_sha256']:
        raise ValueError('Failed-run staged plan differs')
    saved = json.loads(contents)
    plan = saved['plan']
    if (set(saved) != {'plan', 'digest'} or saved['digest'] != packet['digest'] or
            hashlib.sha256(canonical(plan).encode()).hexdigest() != packet['digest'] or
            plan['id'] != packet['task'] or plan['parent_id'] != packet['parent'] or
            plan['engine']['name'] != packet['engine']):
        raise ValueError('Failed-run plan identity differs')
    missing = []
    for name in ('runtime-admission.json', 'execution.log'):
        file = directory / name
        if file.exists() or file.is_symlink():
            read(file)
        else:
            missing.append(name)
    output = directory / packet['engine']
    excluded = []
    if output.exists() or output.is_symlink():
        if output.resolve(strict=True) != output or not output.is_dir():
            raise ValueError('Redirected failed-run output')
        visited = 0
        def denied(error):
            raise error
        for current, dirs, names in os.walk(output, followlinks=False, onerror=denied):
            visited += 1
            if visited > 2000:
                raise ValueError('Failure inventory has too many directories')
            dirs.sort()
            names.sort()
            for name in list(dirs):
                child = Path(current) / name
                if child.is_symlink():
                    raise ValueError('Symbolic link in failed-run output')
                if child == output / 'jax_cache':
                    excluded.append(child.relative_to(directory).as_posix())
                    dirs.remove(name)
            for name in names:
                read(Path(current) / name)
    else:
        missing.append(packet['engine'] + '/')
    # Recheck the identity anchor after enumeration; host repeats and retains the
    # first full inventory to detect additions, deletion or changes between reads.
    anchor = directory / 'plan.json'
    if anchor.resolve(strict=True) != anchor or not stat.S_ISREG(anchor.lstat().st_mode):
        raise ValueError('Failed-run plan redirected during inventory')
    with anchor.open('rb') as stream:
        final = stream.read(1024 * 1024 + 1)
    if len(final) > 1024 * 1024 or hashlib.sha256(final).hexdigest() != packet['plan_sha256']:
        raise ValueError('Failed-run plan changed during inventory')
    return {'version': 'refinement-failure-files-v1', 'id': packet['task'], 'digest': packet['digest'],
            'parent_id': packet['parent'], 'engine': packet['engine'], 'directory': str(directory),
            'plan_sha256': packet['plan_sha256'], 'files': sorted(files, key=lambda item: item['name']),
            'missing': missing, 'excluded_caches': excluded, 'scientific_quality_verified': False,
            'compute_restarted': False}


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > 16384:
            raise ValueError('Bounded failure inventory request required')
        print(canonical(inspect(json.loads(base64.b64decode(sys.argv[1], validate=True)))))
    except Exception:
        # A vendor exception/log may contain credentials; never return it to chat.
        print(canonical({'error': 'FAILURE_INVENTORY_UNAVAILABLE'}))
        raise SystemExit(1)
