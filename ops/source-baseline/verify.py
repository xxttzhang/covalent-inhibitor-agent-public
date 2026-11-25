#!/usr/bin/env python3
"""Verify selected source, local links and complete release inventory; no compute."""
import argparse
import hashlib
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output.is_relative_to(ROOT):
        raise ValueError('Validation evidence must be outside the source tree')
    manifest = json.loads((ROOT / 'docs/source-manifest.json').read_text())
    missing, outside, modifications = [], [], []
    for entry in manifest['files']:
        path = ROOT / entry['destination']
        if not path.is_file():
            missing.append(entry['destination'])
            continue
        if path.is_symlink():
            if not path.resolve().is_relative_to(ROOT):
                outside.append(entry['destination'])
            continue
        digest = sha(path.read_bytes())
        if digest != entry['sha256']:
            modifications.append({'path': entry['destination'], 'source_sha256': entry['sha256'],
                                  'current_sha256': digest})
    registry = json.loads((ROOT / 'ops/release/components.json').read_text())
    for name, target in registry.get('source_links', {}).items():
        path = ROOT / name
        if not path.is_file():
            missing.append(name)
        elif path.resolve() != (ROOT / target).resolve() or not path.resolve().is_relative_to(ROOT):
            outside.append(name)
    if missing or outside:
        raise ValueError(json.dumps({'missing': missing, 'outside_links': outside}))
    spec = importlib.util.spec_from_file_location('baseline_release', ROOT / 'ops/release/release.py')
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    files = release.collect(ROOT, registry)
    current = {name: sha(data) for name, data in sorted(files.items())}
    result = {'schema': 1, 'status': 'source_inventory_verified',
              'scope': 'Source presence, registered internal links, release collection and content hashes only. Not build or web acceptance.',
              'original_source_identity': manifest['source_identity'],
              'current_content_identity': sha(json.dumps(current, sort_keys=True, separators=(',', ':')).encode()),
              'file_count': len(files), 'bytes': sum(map(len, files.values())),
              'modified_original_files': modifications, 'current_files': current}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({k: v for k, v in result.items() if k not in ('modified_original_files', 'current_files')}, indent=2))


if __name__ == '__main__':
    main()
