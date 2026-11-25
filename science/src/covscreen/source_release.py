"""Bind both routes' new jobs to the installed, content-addressed code release."""
import hashlib
import json
from pathlib import Path
import re


def installed_release():
    root = Path(__file__).resolve().parents[3]
    return _read_release(root)


def _read_release(root):
    path = root / 'manifest.json'
    if not path.is_file():
        if re.fullmatch(r'[a-f0-9]{64}', root.name):
            raise ValueError('Installed source release manifest missing')
        return None
    if not re.fullmatch(r'[a-f0-9]{64}', root.name) or path.is_symlink():
        raise ValueError('Invalid installed source release identity')
    raw = path.read_bytes()
    manifest = json.loads(raw)
    canonical = json.dumps(manifest, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()
    if hashlib.sha256(canonical).hexdigest() != root.name or raw != canonical + b'\n':
        raise ValueError('Installed source release manifest changed')
    files = manifest.get('files', {})
    if manifest.get('version') != 'screening-code-release-v1' or not files:
        raise ValueError('Unsupported installed source release')
    for name, digest in files.items():
        source = root / name
        if (Path(name).is_absolute() or '..' in Path(name).parts or source.is_symlink()
                or not source.resolve().is_relative_to(root) or not source.is_file()
                or hashlib.sha256(source.read_bytes()).hexdigest() != digest):
            raise ValueError('Installed source release file changed: ' + name)
    observed = set()
    for source in root.rglob('*'):
        if source.is_symlink():
            raise ValueError('Installed source release contains a symbolic link')
        if source.is_file() and source != path:
            observed.add(source.relative_to(root).as_posix())
    if observed != set(files):
        raise ValueError('Installed source release inventory changed')
    return {'root': str(root), 'release_id': root.name,
            'manifest_sha256': hashlib.sha256(raw).hexdigest(), 'files': files}


def historical_release(reference):
    """Verify a signed parent's immutable release before read-only replay.

    This does not authorize running that parent's worker from new code;
    verify_source remains an exact match to the currently executing release.
    """
    root = Path(reference['root'])
    if not root.is_absolute() or root.is_symlink() or root.resolve() != root:
        raise ValueError('Historical release must be an immutable absolute directory')
    value = _read_release(root)
    if not value or {k:v for k,v in value.items() if k != 'files'} != reference:
        raise ValueError('Historical release differs from the signed parent')
    return value


def frozen_release():
    release = installed_release()
    return {key: value for key, value in release.items() if key != 'files'} if release else None


def verify_source(spec):
    expected = spec.provenance.get('source_release')
    if expected and frozen_release() != expected:
        raise ValueError('Worker code differs from the approved source release')


def verify_input(root, name, digest, spec):
    root = Path(root).resolve()
    path = (root / name).resolve()
    # The equilibration compiler takes these exact file hashes only from its
    # registered runtime configuration. The native approval seals that mapping.
    runtime_input = (spec.engine == 'library-first-openmm-equilibration'
        and Path(name).is_absolute() and str(path) == name
        and spec.parameters.get('runtime_artifacts', {}).get(name) == digest)
    if not path.is_relative_to(root) and not runtime_input:
        release = installed_release()
        expected = spec.provenance.get('source_release')
        if not release or {k: v for k, v in release.items() if k != 'files'} != expected:
            raise ValueError('Input outside project lacks approved source release')
        if not path.is_relative_to(Path(release['root'])):
            raise ValueError('Input outside project and approved source release')
        if release['files'].get(path.relative_to(release['root']).as_posix()) != digest:
            raise ValueError('Input is not registered in approved source release')
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
        raise ValueError('Frozen input changed: ' + name)
