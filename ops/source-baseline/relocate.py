#!/usr/bin/env python3
"""Apply the reviewed local directory rename; preserve external task identities."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OLD = 'covalent-library-first'


def sha(data):
    return hashlib.sha256(data).hexdigest()


def main():
    record = ROOT / 'docs/path-migration.json'
    if record.exists():
        raise ValueError('Migration already recorded; do not overwrite provenance')
    manifest = json.loads((ROOT / 'docs/source-manifest.json').read_text())
    changed, deferred = [], []
    for entry in manifest['files']:
        if entry.get('kind') == 'symlink':
            continue
        path = ROOT / entry['destination']
        data = path.read_bytes()
        try:
            original = data.decode('utf-8')
        except UnicodeDecodeError:
            continue
        if OLD not in original:
            continue
        lines, numbers = [], []
        for number, line in enumerate(original.splitlines(keepends=True), 1):
            # These are external scientific data addresses, Python distribution
            # identities, or old-checkout references needing semantic review.
            preserve = ('/inspire/' in line or '/Users/' in line or
                        (path.name == 'pyproject.toml' and
                         (line.startswith('name =') or line.startswith('dependencies ='))))
            if OLD in line and preserve:
                deferred.append({'path': entry['destination'], 'line': number})
            replacement = line if preserve else line.replace(OLD, 'science')
            if replacement != line:
                numbers.append(number)
            lines.append(replacement)
        output = ''.join(lines).encode()
        if output != data:
            path.write_bytes(output)
            changed.append({'path': entry['destination'], 'before_sha256': sha(data),
                            'after_sha256': sha(output), 'lines': numbers})
    receipt = {'schema': 1, 'operation': 'local-directory-rename',
               'from': OLD, 'to': 'science', 'changed': changed,
               'external_or_distribution_references_retained': deferred}
    record.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'changed_files': len(changed), 'deferred_references': len(deferred)}))


if __name__ == '__main__':
    main()
