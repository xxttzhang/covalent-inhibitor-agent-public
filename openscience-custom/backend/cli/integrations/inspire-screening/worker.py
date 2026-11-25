"""Inspire shared-disk adapter for the existing registered EGFR C797 pilot.

No arbitrary commands or input paths are accepted. The original campaign,
database, and prior result registry are never modified by this adapter.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

SHARED = Path(os.environ.get('COVSCREEN_LEGACY_SHARED_ROOT', '/inspire/unconfigured')) / '2_agent'
PROJECT = SHARED / 'science'
BASE = SHARED / 'openscience_inspire_screening_v1'
SEED = PROJECT / 'state/jobs/20260901T042553-covindb-prescreen-30669aeda2/artifacts'
CAMPAIGN = PROJECT / 'configs/library_first/EGFR_C797_covindb_pilot_v1.yaml'
TARGET = PROJECT / 'configs/covindb/EGFR_C797_mvp_v1.yaml'
CARSI = SHARED.parent / '1_zhang/CarsiDock-Cov'
PDB = PROJECT / 'projects/egfr-c797/ligand_first/source_structures/1M17.pdb'
REFERENCE = PROJECT / 'projects/egfr-c797/ligand_first/derived/1M17_AQ4_parent_pose.sdf'
CAMPAIGN_ID = 'egfr-c797-covindb-pilot-v1'


def digest(path):
    value = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            value.update(block)
    return value.hexdigest()


def paths():
    return [CAMPAIGN, TARGET, PDB, REFERENCE,
            SEED / 'stage2_candidates.parquet', SEED / 'stage2_candidates.sdf',
            SEED / 'stage2_selection_manifest.json',
            PROJECT / 'environment/activate_carsidock.sh',
            CARSI / 'checkpoints/carsidock_230731.ckpt',
            *sorted((PROJECT / 'scripts').glob('*.py')),
            *sorted((PROJECT / 'src/covscreen').rglob('*.py')),
            *sorted((CARSI / 'src').rglob('*.py')),
            CARSI / 'run_carsidockcov_screening.py', Path(__file__).resolve()]


def preflight():
    import pandas as pd
    import torch
    import yaml
    from rdkit import Chem
    files = paths()
    missing = [str(p) for p in files if not p.is_file()]
    if missing:
        raise ValueError('Missing required assets: ' + ', '.join(missing))
    target = yaml.safe_load(TARGET.read_text())
    campaign = yaml.safe_load(CAMPAIGN.read_text())
    if (target.get('target_id'), target.get('residue_number'), target.get('warhead_class'),
            target.get('reaction_type'), target.get('structure', {}).get('carsidock_covres')) != (
            'EGFR', 797, 'acrylamide', 'Michael Addition', 'A:CYS:773'):
        raise ValueError('Registered EGFR reaction/site contract changed; re-audit required.')
    if campaign.get('covres') != 'A:CYS:773' or campaign.get('reaction_type') != 'Michael Addition':
        raise ValueError('Campaign reaction/site contract changed.')
    if not any(line.startswith('ATOM') and line[21:22] == 'A' and line[22:26].strip() == '773'
               and line[17:20] == 'CYS' and line[12:16].strip() == 'SG' for line in PDB.read_text().splitlines()):
        raise ValueError('Registered receptor CYS 773 SG atom is missing.')
    manifest = json.loads((SEED / 'stage2_selection_manifest.json').read_text())
    gates = manifest.get('quality_gates', {})
    if manifest.get('status') != 'completed' or not gates or not all(v is True for v in gates.values()):
        raise ValueError('Prescreen evidence/identity gates are not PASS.')
    table = pd.read_parquet(SEED / 'stage2_candidates.parquet')
    if not {'input_sdf_index', 'compound_id', 'warhead_class'}.issubset(table.columns):
        raise ValueError('Candidate identity schema changed.')
    if table.input_sdf_index.duplicated().any() or table.compound_id.duplicated().any():
        raise ValueError('Candidate identity is not unique.')
    if set(table.warhead_class) != {'acrylamide'}:
        raise ValueError('Mixed reaction classes require a separately audited campaign.')
    molecules = list(Chem.SDMolSupplier(str(SEED / 'stage2_candidates.sdf'), removeHs=False, sanitize=False))
    if len(molecules) != len(table) or any(m is None for m in molecules):
        raise ValueError('SDF/table record counts or parsing do not agree.')
    identities = {int(r.input_sdf_index): str(r.compound_id) for r in table.itertuples()}
    observed = {}
    for mol in molecules:
        if not mol.HasProp('INPUT_SDF_INDEX') or not mol.HasProp('COMPOUND_ID'):
            raise ValueError('SDF identity tags missing.')
        index = int(mol.GetProp('INPUT_SDF_INDEX'))
        if index in observed:
            raise ValueError('Duplicate SDF identity.')
        observed[index] = mol.GetProp('COMPOUND_ID')
    if observed != identities:
        raise ValueError('SDF and candidate identities do not match.')
    hashes = {str(p): digest(p) for p in files}
    return {'campaign': CAMPAIGN_ID, 'prepared_candidates': len(table), 'hashes': hashes,
            'reaction': 'Michael Addition', 'canonical_site': 'EGFR C797',
            'structure_site': '1M17 chain A CYS 773 (registered offset +24)',
            'torch': torch.__version__, 'cuda_build': torch.version.cuda,
            'gpu_execution_verified': False,
            'scope': 'Existing prepared pilot candidates, not the whole database. GPU runtime remains to be tested.'}


def jobdir(name):
    if not re.fullmatch(r'os-cov-[a-f0-9]{24}', name):
        raise ValueError('Invalid integration job name.')
    return BASE / 'runs' / name


def validate(plan):
    if plan.get('campaign') != CAMPAIGN_ID:
        raise ValueError('Only the registered pilot campaign is supported.')
    if type(plan.get('candidates')) is not int or not 1 <= plan['candidates'] <= 500:
        raise ValueError('Candidate cap must be between 1 and 500.')
    if type(plan.get('minutes')) is not int or not 1 <= plan['minutes'] <= 240:
        raise ValueError('Runtime must be between 1 and 240 minutes.')
    jobdir(plan['name'])
    return plan


def verify(plan):
    validate(plan)
    current = preflight()
    if current['hashes'] != plan['assets']['hashes']:
        raise ValueError('Input/worker/engine hashes changed since approval. Create a new plan.')
    if plan['candidates'] > current['prepared_candidates']:
        raise ValueError('Requested candidates exceed the prepared set.')


def prepare(plan):
    import pandas as pd
    from rdkit import Chem
    verify(plan)
    directory = jobdir(plan['name'])
    directory.mkdir(parents=True, exist_ok=False)
    # Preserve original identity tags and table indices; never renumber by pose order.
    table = pd.read_parquet(SEED / 'stage2_candidates.parquet').head(plan['candidates'])
    wanted = set(int(v) for v in table.input_sdf_index)
    table.to_parquet(directory / 'candidates.parquet', index=False)
    written = 0
    with Chem.SDWriter(str(directory / 'candidates.sdf')) as writer:
        for mol in Chem.SDMolSupplier(str(SEED / 'stage2_candidates.sdf'), removeHs=False, sanitize=False):
            if int(mol.GetProp('INPUT_SDF_INDEX')) in wanted:
                writer.write(mol)
                written += 1
    if written != len(table):
        raise ValueError('Prepared candidate count mismatch.')
    frozen = dict(plan, prepared_hashes={name: digest(directory / name) for name in ['candidates.parquet', 'candidates.sdf']})
    with (directory / 'plan.json').open('x') as stream:
        json.dump(frozen, stream)
    return {'prepared': True, 'candidates': written, 'path': str(directory), 'plan_sha256': digest(directory / 'plan.json')}


def result(name):
    directory = jobdir(name)
    path = directory / 'result.json'
    if not path.is_file():
        return {'artifact_ready': False, 'path': str(directory), 'note': 'No verified completion report yet; inspect Inspire status/logs.'}
    return json.loads(path.read_text())


def run(name, expected):
    import pandas as pd
    import torch
    directory = jobdir(name)
    if digest(directory / 'plan.json') != expected:
        raise ValueError('Prepared plan was modified.')
    plan = json.loads((directory / 'plan.json').read_text())
    # Exclusive marker prevents accidental reruns in the same output directory.
    with (directory / 'started.json').open('x') as stream:
        json.dump({'started_unix': time.time()}, stream)
    report = {'name': name, 'status': 'FAILED', 'artifact_ready': False, 'path': str(directory)}
    try:
        verify(plan)
        for filename, sha in plan['prepared_hashes'].items():
            if digest(directory / filename) != sha:
                raise ValueError('Prepared candidate inputs changed.')
        if not torch.cuda.is_available():
            raise ValueError('CUDA is unavailable; refusing CPU fallback for a GPU job.')
        report['gpu'] = torch.cuda.get_device_name(0)
        command = [sys.executable, str(PROJECT / 'scripts/run_carsidock_registered.py'),
                   '--chunk', '0', '--device', 'cuda', '--pdb', str(PDB),
                   '--reference-ligand', str(REFERENCE), '--ligand-sdf', str(directory / 'candidates.sdf'),
                   '--ligand-table', str(directory / 'candidates.parquet'), '--covres', 'A:CYS:773',
                   '--reaction-type', 'Michael Addition', '--output-dir', str(directory / 'artifacts'),
                   '--output-scores', str(directory / 'artifacts/carsidock_scores.parquet'),
                   '--carsidock-repo', str(CARSI), '--checkpoint', str(CARSI / 'checkpoints/carsidock_230731.ckpt'),
                   '--num-conformer', '1', '--cuda-device-index', '0', '--seed', '20260725']
        with (directory / 'engine.log').open('x') as log:
            completed = subprocess.run(command, cwd=PROJECT, stdout=log, stderr=subprocess.STDOUT,
                                       timeout=plan['minutes'] * 60, check=False)
        if completed.returncode:
            raise ValueError('CarsiDock returned nonzero exit code ' + str(completed.returncode))
        score = directory / 'artifacts/carsidock_scores.parquet'
        frame = pd.read_parquet(score)
        approved = pd.read_parquet(directory / 'candidates.parquet')
        if len(frame) != plan['candidates'] or set(frame.compound_id) != set(approved.compound_id) or frame.compound_id.duplicated().any():
            raise ValueError('Output candidate denominator/identity is incomplete.')
        success = int(frame.engine_status.eq('SUCCESS').sum())
        report.update(status='SUCCEEDED' if success == len(frame) else 'PARTIAL', artifact_ready=True,
                      candidates=len(frame), successful_candidates=success, failed_candidates=len(frame) - success,
                      score_path=str(score), score_sha256=digest(score), plan_sha256=expected)
    except Exception as error:
        report['error'] = str(error)
    with (directory / 'result.json').open('x') as stream:
        json.dump(report, stream)
    print(json.dumps(report), flush=True)
    return 0 if report['status'] == 'SUCCEEDED' else 1


if __name__ == '__main__':
    if not os.environ.get('COVSCREEN_LEGACY_SHARED_ROOT', '').startswith('/inspire/'):
        raise ValueError('Configure COVSCREEN_LEGACY_SHARED_ROOT before running the legacy worker')
    action = sys.argv[1]
    if action == 'run':
        sys.exit(run(sys.argv[2], sys.argv[3]))
    if action == 'preflight':
        print(json.dumps(preflight()))
    elif action == 'prepare':
        print(json.dumps(prepare(json.loads(sys.argv[2]))))
    elif action == 'result':
        print(json.dumps(result(sys.argv[2])))
    else:
        raise ValueError('Unsupported worker operation.')
