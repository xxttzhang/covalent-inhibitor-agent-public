"""Hash-bound, read-only reader repair around an unchanged frozen controller.

The original controller still verifies its release, plan, files, measurements
and retained audit. Only the expensive SDK reader implementation is replaced;
the result explicitly records both reader identities. No engine is invoked.
"""
import hashlib
import json
import sys
from pathlib import Path


def checked_reader(revision, original):
    if set(revision) != {'version', 'plan_digest', 'original_sha256', 'path', 'sha256'}:
        raise ValueError('Invalid readback revision fields')
    if revision['version'] != 'covdock-cached-coordinates-v1':
        raise ValueError('Unsupported readback revision')
    for key in ('plan_digest', 'original_sha256', 'sha256'):
        import re
        if not isinstance(revision[key], str) or not re.fullmatch('[a-f0-9]{64}', revision[key]):
            raise ValueError('Invalid readback revision identity')
    replacement = Path(revision['path'])
    if not replacement.is_absolute() or replacement.resolve(strict=True) != replacement:
        raise ValueError('Reader revision must use a canonical absolute path')
    for path, checksum in ((Path(original), revision['original_sha256']), (replacement, revision['sha256'])):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ValueError('Reader revision is not a bounded regular file')
        if hashlib.sha256(path.read_bytes()).hexdigest() != checksum:
            raise ValueError('Reader revision content changed')
    return replacement


def inspect(original_entry, revision, root, parent, task, expected, witness=None):
    if revision['plan_digest'] != expected:
        raise ValueError('Reader revision belongs to another plan')
    original_entry = Path(original_entry)
    sys.path.insert(0, str(original_entry.parent))
    sys.path.insert(0, str(original_entry.parent / 'science/src'))
    import refinement_readback
    from covscreen import refinement_output
    if Path(refinement_readback.__file__).resolve() != original_entry.resolve(strict=True):
        raise ValueError('Unexpected frozen controller import')
    original_reader = original_entry.parent / 'science/scripts/inspect_covdock_output.py'
    replacement = checked_reader(revision, original_reader)
    original_sdk = refinement_output._covdock_sdk
    calls = []

    def revised_sdk(request, runtime, script, timeout=45):
        if Path(script) != original_reader:
            raise ValueError('Unexpected original SDK reader')
        checked_reader(revision, original_reader)
        value = original_sdk(request, runtime, replacement, timeout=timeout)
        checked_reader(revision, original_reader)
        calls.append(True)
        return value

    refinement_output._covdock_sdk = revised_sdk
    try:
        result = refinement_readback.inspect(root, parent, task, expected, witness)
    finally:
        refinement_output._covdock_sdk = original_sdk
    if result['evidence']['engine'] != 'covdock' or len(calls) != 1:
        raise ValueError('Reader revision requires one real CovDock SDK read')
    checked_reader(revision, original_reader)
    return {**result, 'reader_revision': revision}


if __name__ == '__main__':
    value = json.loads(sys.argv[1])
    print(json.dumps(inspect(**value), ensure_ascii=False, allow_nan=False))
