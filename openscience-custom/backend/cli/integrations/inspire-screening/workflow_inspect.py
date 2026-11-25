"""Read-only asset inspection for a NEW Library-first workflow, never pilot reuse.

This reports file availability, not engine runtime or scientific validation.
It never imports the legacy dispatcher, submits jobs, or reads model credentials.
"""
import csv
import hashlib
import json
import os
import re
import sys
from pathlib import Path

import yaml

SHARED = Path(os.environ.get('COVSCREEN_LEGACY_SHARED_ROOT', '/inspire/unconfigured'))
ROOT = SHARED / '2_agent/covalent-library-first'
DATABASE = SHARED / '1_zhang/CovInDB/Virtual_Screening_Library.csv'


def load(path):
    return yaml.safe_load(path.read_text()) or {} if path.is_file() else {}


def present(value, directory=False):
    if not isinstance(value, str) or not value:
        return False
    path = Path(value)
    return path.is_dir() if directory else path.is_file()


def inspect(root, target, site, database):
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', target) or not re.fullmatch(r'C[1-9][0-9]{0,4}', site):
        raise ValueError('Explicit target symbol and canonical cysteine site are required.')
    records = [(path, load(path)) for path in sorted((root / 'configs/targets').glob('*.yaml'))]
    matches = [(path, record) for path, record in records
               if str(record.get('target_id', '')).upper() == target.upper()
               and record.get('residue_number') == int(site[1:])]
    config = load(root / 'configs/local.inspire.yaml')
    carsi, af3, covdock = [config.get(key, {}) for key in ('carsidock', 'alphafold3', 'schrodinger')]
    scripts = root / 'scripts'
    engines = {
        'prepare': (scripts / 'run_covindb_prescreen.py').is_file(),
        'carsidock': (scripts / 'run_carsidock_registered.py').is_file()
            and present(carsi.get('repo'), True) and present(carsi.get('checkpoint')),
        'rtmscore': (scripts / 'run_rtmscore_existing_poses.py').is_file()
            and present(carsi.get('rtmscore_checkpoint')),
        'af3': (scripts / 'run_af3_registered_batch.py').is_file()
            and present(af3.get('runner')) and present(af3.get('python'))
            and present(af3.get('database_dir'), True) and present(af3.get('model_dir'), True),
        'covdock': (scripts / 'run_covdock_lo_registered.py').is_file()
            and present(str(Path(covdock['root']) / 'covalent_docking') if covdock.get('root') else None),
    }
    source = {'kind': 'virtual_screening', 'path': str(database), 'exists': database.is_file(),
              'identity_frozen': False, 'row_count': None}
    if database.is_file():
        stat = database.stat()
        with database.open(newline='', encoding='utf-8-sig') as stream:
            source.update(header=next(csv.reader(stream), []), size_bytes=stat.st_size, mtime_ns=stat.st_mtime_ns)
    campaigns = []
    for path in sorted((root / 'configs/library_first').glob('*.yaml')):
        record = load(path)
        linked = load(root / str(record.get('target_config', 'missing')))
        if str(linked.get('target_id', '')).upper() != target.upper() or linked.get('residue_number') != int(site[1:]):
            continue
        campaigns.append({'name': record.get('campaign_id'), 'path': str(path),
                          'legacy_seed_run_ids': record.get('seed_run_ids', {}),
                          'prescreen_max_rows': record.get('prescreen_max_rows'),
                          'receptor': record.get('pdb'), 'covres': record.get('covres'),
                          'reused_by_this_workflow': False})
    registry = matches[0] if len(matches) == 1 else None
    route = load(root / 'projects' / f'{target.lower()}-{site.lower()}' / 'plans/route_plan.json')
    return {'target_registered': registry is not None,
            'target_contract': registry[1] if registry else None,
            'target_contract_sha256': hashlib.sha256(registry[0].read_bytes()).hexdigest() if registry else None,
            'source': source, 'existing_campaigns': campaigns, 'existing_project_route': route.get('selected_route'),
            'engine_assets_present': engines, 'engine_runtime_verified': False,
            'license_file_present': present(covdock.get('license_file')), 'license_checkout_verified': False,
            'warnings': ['File presence is not GPU/runtime/license validation.',
                         'A new source manifest, receptor/site audit and candidate budget are required.',
                         'Do not reuse old campaign seeds or replace the requested library with a pilot.',
                         'Legacy AF3 candidate-pool defaults are not an uncertainty-based subset contract.'],
            'submitted': False}


if __name__ == '__main__':
    if not os.environ.get('COVSCREEN_LEGACY_SHARED_ROOT', '').startswith('/inspire/'):
        raise ValueError('Configure COVSCREEN_LEGACY_SHARED_ROOT before inspecting the legacy installation')
    request = json.loads(sys.argv[1])
    if set(request) != {'target', 'site'}:
        raise ValueError('Only target and site are accepted; no paths, commands or execution flags.')
    print(json.dumps(inspect(ROOT, request['target'], request['site'], DATABASE), ensure_ascii=False))
