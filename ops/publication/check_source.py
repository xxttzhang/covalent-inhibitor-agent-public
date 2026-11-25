#!/usr/bin/env python3
"""Bounded offline source audit. Never prints matched credential values."""
import ast
import argparse
import json
import re
import subprocess
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[2])
root = parser.parse_args().root.resolve()
git_root = Path(subprocess.check_output(['git', 'rev-parse', '--show-toplevel'], cwd=root).decode().strip()).resolve()
if git_root != root:
    parser.error('--root must be the repository root')
files = subprocess.check_output(['git', 'ls-files', '-z', '--cached', '--others', '--exclude-standard'], cwd=root).decode().split('\0')
rules = {'github_token': r'gh[pousr]_[A-Za-z0-9]{30,}', 'api_key': r'sk-[A-Za-z0-9_-]{28,}', 'aws_key': r'AKIA[0-9A-Z]{16}', 'private_key': r'-----BEGIN (?:RSA |OPENSSH |EC )?PRIVATE KEY-----'}
forbidden_prefixes = (
    'science/vendor/drug-idea-generator/',
    'science/tests/fixtures/',
    'routes/library-first/tests/fixtures/',
    'routes/ligand-first/tests/fixtures/',
    'routes/de-novo/registry/egfr_c797_v1/assets/',
)
forbidden_files = {
    'science/src/covscreen/data/covpdb_noncovalent_near_cys.tsv',
    'science/src/covscreen/data/covpdb_noncovalent_near_cys.manifest.json',
}
errors = []; portability = []; python_count = 0; checked = 0
for name in sorted(set(files) - {''}):
    path = root / name
    if not path.exists():
        continue  # tracked deletion, not part of the candidate
    checked += 1
    if name in forbidden_files or name.startswith(forbidden_prefixes):
        errors.append({'file': name, 'rule': 'excluded_public_component'})
    if path.is_symlink():
        if not path.resolve().is_relative_to(root): errors.append({'file': name, 'rule': 'external_symlink'})
        continue
    if path.suffix in {'.key', '.pem', '.ckpt', '.pth'} or path.name.startswith('.runtime-'):
        errors.append({'file': name, 'rule': 'prohibited_artifact'})
    if path.stat().st_size > 50 * 1024**2: errors.append({'file': name, 'rule': 'large_file'})
    try: text = path.read_text()
    except UnicodeError: continue
    for rule, pattern in rules.items():
        for match in re.finditer(pattern, text):
            line = text[:match.start()].count('\n') + 1
            # Exact nonfunctional fixture, not a general exemption for test files.
            if rule == 'private_key' and name.endswith('/test/provider/transform.test.ts') and text[match.start():].startswith('-----BEGIN ' + 'PRIVATE KEY-----\\nABCDEF\\n-----END PRIVATE KEY-----'): continue
            errors.append({'file': name, 'line': line, 'rule': rule})
    if re.search(r'/Users/[A-Za-z][A-Za-z0-9_-]*/|/inspire/(?:hdd|ssd)/(?:global_user|project/[^/]+)/[A-Za-z][A-Za-z0-9]*-[0-9]{8,}', text): portability.append(name)
    if path.suffix == '.py' and '/skills/' not in name:
        try: ast.parse(text, filename=name); python_count += 1
        except SyntaxError as exc: errors.append({'file': name, 'line': exc.lineno, 'rule': 'python_syntax'})
print(json.dumps({'checked_files': checked, 'python_syntax_checked': python_count, 'errors': errors, 'portability_findings': portability, 'scope': 'Working tree only; not full secret/history/licensing or scientific acceptance.'}, indent=2))
raise SystemExit(bool(errors))
