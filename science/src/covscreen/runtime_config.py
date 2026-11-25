"""Immutable local runtime configuration bytes for newly frozen native jobs."""
import hashlib
import os
from uuid import uuid4


def snapshot(root, contents):
    checksum = hashlib.sha256(contents).hexdigest()
    parent = root / 'state/runtime-configs'
    for path in (root / 'state', parent):
        if path.is_symlink():
            raise ValueError('Runtime configuration snapshot cannot traverse symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    path = parent / (checksum + '.yaml')
    temporary = parent / ('.' + checksum + '-' + uuid4().hex)
    try:
        with temporary.open('xb') as stream:
            os.chmod(temporary, 0o600)
            stream.write(contents)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)  # Publish complete bytes without overwriting.
        except FileExistsError:
            pass
    finally:
        temporary.unlink(missing_ok=True)
    if path.is_symlink() or path.read_bytes() != contents:
        raise ValueError('Frozen runtime configuration snapshot changed')
    return path
