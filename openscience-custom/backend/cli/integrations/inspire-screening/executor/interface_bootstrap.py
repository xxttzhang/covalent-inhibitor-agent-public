"""Host-embedded source-interface inspection, never engine execution."""
import base64
import hashlib
import json
import re
import sys
import types
from pathlib import Path


def inspect_release(packet):
    if not isinstance(packet, dict) or set(packet) != {'root', 'engine', 'release_id', 'manifest_sha256', 'checker_sha256'}:
        raise ValueError('Invalid interface inspection packet')
    if packet['engine'] not in ('af3', 'covdock'):
        raise ValueError('Unknown interface engine')
    for name in ('release_id', 'manifest_sha256', 'checker_sha256'):
        if not isinstance(packet[name], str) or not re.fullmatch(r'[a-f0-9]{64}', packet[name]):
            raise ValueError('Exact interface source checksums required')
    root = Path(packet['root'])
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise ValueError('Interface root cannot be redirected')
    release = root / 'releases' / packet['release_id']
    if release.resolve(strict=True) != release:
        raise ValueError('Interface release cannot be redirected')
    checker = release / 'code_release.py'
    if checker.is_symlink() or checker.stat().st_size > 1024 * 1024:
        raise ValueError('Invalid interface release checker')
    contents = checker.read_bytes()
    if hashlib.sha256(contents).hexdigest() != packet['checker_sha256']:
        raise ValueError('Interface release checker changed')
    module = types.ModuleType('verified_interface_release')
    module.__file__ = str(checker)
    exec(compile(contents, str(checker), 'exec'), module.__dict__)
    verified = module.verify(release, packet['release_id'], packet['manifest_sha256'])
    source = release / 'engine_interface.py'
    if source.stat().st_size > 1024 * 1024:
        raise ValueError('Interface reader exceeds source bound')
    code = source.read_bytes()
    if hashlib.sha256(code).hexdigest() != verified['files'].get('engine_interface.py'):
        raise ValueError('Interface reader differs from frozen release')
    reader = types.ModuleType('verified_engine_interface')
    reader.__file__ = str(source)
    exec(compile(code, str(source), 'exec'), reader.__dict__)
    result = reader.inspect(release / 'science', packet['engine'])
    if module.verify(release, packet['release_id'], packet['manifest_sha256']) != verified:
        raise ValueError('Interface source changed during inspection')
    result['code'] = {key: packet[key] for key in ('release_id', 'manifest_sha256')}
    return result


if __name__ == '__main__':
    try:
        if len(sys.argv) != 2 or len(sys.argv[1]) > 16384:
            raise ValueError('Bounded interface packet required')
        print(json.dumps(inspect_release(json.loads(base64.b64decode(sys.argv[1], validate=True)))))
    except Exception:
        print(json.dumps({'error': 'REFINEMENT_INTERFACE_INSPECTION_FAILED'}))
        raise SystemExit(1)
