#!/usr/bin/env python3
"""Inventory and copy a source baseline. Does not deploy or touch original data."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile

POLICY = Path(__file__).with_name('policy.json')
SECRET = re.compile(rb'(?:sk-[A-Za-z0-9_-]{24,}|-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----\s+[A-Za-z0-9+/=]{40,})')
REFERENCE = re.compile(r'covalent-library-first|/Users/[^/\s]+/|(?:\.\./|[\x22\x27])reports/')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def mapped(name, policy):
    for old, new in policy['mapping'].items():
        if name == old or name.startswith(old + '/'):
            return new + name[len(old):]
    return name


def exclusion(path, relative, policy):
    if path.is_symlink():
        return 'symlink: requires explicit dependency review'
    if any(relative == name or relative.startswith(name + '/') for name in policy['exclude_paths']):
        return 'explicit exclusion'
    if any(part in policy['exclude_components'] or part.startswith('dist-') for part in Path(relative).parts):
        return 'generated/cache/historical directory'
    if ((path.name in policy['exclude_names'] and relative not in policy.get('public_files', []))
            or path.name.startswith('.env') or path.name.endswith('.provider.json')):
        return 'runtime/private file'
    if path.suffix in policy['exclude_suffixes']:
        return 'generated output'
    return None


def inventory(root):
    policy_bytes = POLICY.read_bytes()
    policy = json.loads(policy_bytes)
    paths = set()
    excluded = []
    for folder in policy['roots']:
        base = root / folder
        if not base.is_dir() or base.is_symlink():
            raise ValueError('Missing source root: ' + folder)
        for current, dirs, files in os.walk(base, followlinks=False):
            for name in sorted(dirs.copy()):
                path = Path(current) / name
                relative = path.relative_to(root).as_posix()
                reason = exclusion(path, relative, policy)
                if reason:
                    excluded.append({'path': relative, 'reason': reason})
                    dirs.remove(name)
            for name in files:
                paths.add(Path(current) / name)
    paths.update(root / name for name in policy['files'])
    records = []
    references = []
    links = []
    for path in sorted(paths):
        relative = path.relative_to(root).as_posix()
        reason = exclusion(path, relative, policy)
        if reason:
            if path.is_symlink() and path.is_file():
                links.append(path)
            excluded.append({'path': relative, 'reason': reason})
            continue
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValueError('Missing or outside source: ' + relative)
        data = path.read_bytes()
        if SECRET.search(data):
            raise ValueError('Credential-like content; review filename only: ' + relative)
        records.append({'source': relative, 'destination': mapped(relative, policy),
                        'sha256': sha(data), 'bytes': len(data), 'mode': path.stat().st_mode & 0o777})
        try:
            content = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        lines = [i for i, line in enumerate(content.splitlines(), 1) if REFERENCE.search(line)]
        if lines:
            references.append({'path': mapped(relative, policy), 'lines': lines})
    selected = {record['source'] for record in records}
    for path in links:
        real = path.resolve()
        if not real.is_relative_to(root) or real.relative_to(root).as_posix() not in selected:
            continue
        relative = path.relative_to(root).as_posix()
        destination = mapped(relative, policy)
        target_source = real.relative_to(root).as_posix()
        target = os.path.relpath(mapped(target_source, policy), str(Path(destination).parent))
        records.append({'source': relative, 'destination': destination, 'kind': 'symlink',
                        'target_source': target_source, 'target': target,
                        'source_link': os.readlink(path), 'sha256': sha(os.readlink(path).encode()),
                        'bytes': 0, 'mode': 0o777})
        excluded = [entry for entry in excluded if entry['path'] != relative]
    records.sort(key=lambda record: record['source'])
    if len({r['destination'] for r in records}) != len(records):
        raise ValueError('Colliding destination paths')
    if sum(r['bytes'] for r in records) > 256 * 1024 * 1024:
        raise ValueError('Inventory exceeds 256 MiB source budget')
    return {'schema': 1, 'source_root': str(root), 'policy_sha256': sha(policy_bytes),
            'source_identity': sha(canonical(records)), 'files': records,
            'excluded': excluded, 'references_to_review': references}


def recheck(root, plan):
    changed = []
    for record in plan['files']:
        path = root / record['source']
        if record.get('kind') == 'symlink':
            if not path.is_symlink() or os.readlink(path) != record['source_link']:
                changed.append(record['source'])
            continue
        if path.is_symlink() or not path.is_file() or sha(path.read_bytes()) != record['sha256']:
            changed.append(record['source'])
    if changed:
        raise ValueError('Source changed; regenerate inventory: ' + ', '.join(changed[:15]))


def copy(plan, destination, audit):
    root = Path(plan['source_root'])
    if plan['policy_sha256'] != sha(POLICY.read_bytes()):
        raise ValueError('Policy changed after inventory')
    # Rebuild the selected set as well as checking content, so new source files
    # created during concurrent development cannot be silently omitted.
    current = inventory(root)
    if current['source_identity'] != plan['source_identity']:
        raise ValueError('Source inventory changed; regenerate plan')
    for record in plan['files']:
        target = destination / record['destination']
        if target.exists() or target.is_symlink():
            raise ValueError('Refusing to replace existing file: ' + str(target))
    with tempfile.TemporaryDirectory(prefix='source-stage-', dir=audit) as folder:
        staging = Path(folder)
        for record in plan['files']:
            if record.get('kind') == 'symlink':
                continue
            data = (root / record['source']).read_bytes()
            if sha(data) != record['sha256']:
                raise ValueError('Source changed while copying: ' + record['source'])
            target = staging / record['destination']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            target.chmod(record['mode'])
        recheck(root, plan)
        if inventory(root)['source_identity'] != plan['source_identity']:
            raise ValueError('Source inventory changed during copy')
        for record in plan['files']:
            target = destination / record['destination']
            target.parent.mkdir(parents=True, exist_ok=True)
            if record.get('kind') == 'symlink':
                target.symlink_to(record['target'])
                continue
            # Exclusive publication; never overwrite a concurrently created file.
            with target.open('xb') as stream:
                stream.write((staging / record['destination']).read_bytes())
            target.chmod(record['mode'])
    for record in plan['files']:
        if record.get('kind') == 'symlink':
            target = destination / record['destination']
            if not target.is_file() or not target.resolve().is_relative_to(destination) or os.readlink(target) != record['target']:
                raise ValueError('Invalid internal link: ' + record['destination'])
            continue
        if sha((destination / record['destination']).read_bytes()) != record['sha256']:
            raise ValueError('Copied hash mismatch: ' + record['destination'])
    receipt = {'schema': 1, 'state': 'copied-not-build-verified',
               'source_identity': plan['source_identity'], 'destination': str(destination),
               'files': len(plan['files']), 'bytes': sum(r['bytes'] for r in plan['files']),
               'source_stability_checked': True, 'copied_hashes_verified': True}
    write_json(audit / 'copy-receipt.json', receipt)
    write_json(destination / 'docs/source-manifest.json',
               {**plan, 'source_root': None, 'source_root_redacted': True})
    return receipt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('plan', 'copy'))
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--audit', type=Path, required=True)
    parser.add_argument('--destination', type=Path)
    args = parser.parse_args()
    root, audit = args.source.resolve(), args.audit.resolve()
    audit.mkdir(parents=True, exist_ok=True)
    if args.command == 'plan':
        plan = inventory(root)
        write_json(audit / 'copy-plan.json', plan)
        print(json.dumps({'files': len(plan['files']), 'bytes': sum(r['bytes'] for r in plan['files']),
                          'source_identity': plan['source_identity'],
                          'groups': dict(Counter(r['destination'].split('/')[0] for r in plan['files'])),
                          'excluded': len(plan['excluded']), 'reference_files': len(plan['references_to_review'])}, indent=2))
    else:
        if args.destination is None:
            parser.error('--destination required for copy')
        print(json.dumps(copy(json.loads((audit / 'copy-plan.json').read_text()), args.destination.resolve(), audit), indent=2))


if __name__ == '__main__':
    main()
