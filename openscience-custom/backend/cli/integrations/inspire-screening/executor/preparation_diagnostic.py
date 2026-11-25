"""Read-only, bounded aggregation of a stopped preparation attempt.

No source-library scan, descriptor calculation, molecular output or file writes.
Hashes bind the diagnostic snapshot, not scientific validity or biological activity.
"""
import hashlib
import io
import json
import math
import re
import stat
import sys
from pathlib import Path


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest()


def inspect(root, project, task, expected):
    import pyarrow.parquet as pq
    import yaml

    if not re.fullmatch(r'screen-[a-f0-9]{24}', task) or not re.fullmatch(r'[a-f0-9]{64}', expected):
        raise ValueError('Invalid diagnostic identity')
    root, project = Path(root), Path(project)
    base = root / 'tasks' / task
    files = {}

    def read(path, limit, name):
        if not path.is_absolute() or path.resolve() != path or not stat.S_ISREG(path.lstat().st_mode):
            raise ValueError('Diagnostic path is not an absolute regular file')
        if not 0 < path.stat().st_size <= limit:
            raise ValueError('Diagnostic file exceeds its byte bound')
        with path.open('rb') as handle:
            data = handle.read(limit + 1)
        if len(data) > limit:
            raise ValueError('Diagnostic file grew beyond its byte bound')
        files[name] = {'sha256': hashlib.sha256(data).hexdigest(), 'bytes': len(data)}
        return data

    plan = json.loads(read(base / 'plan.json', 1024 * 1024, 'plan.json'))
    if (digest(plan) != expected or plan['id'] != task or plan['runtime']['root'] != str(root)
            or plan['runtime']['project'] != str(project) or plan['target'] != 'EGFR' or plan['site'] != 'C797'
            or plan['library'] != 'CovInDB/Virtual_Screening_Library.csv'):
        raise ValueError('Diagnostic plan differs from the approved identity')
    if plan['scope'] != 'bounded' or not isinstance(plan['max_records'], int) or not 0 < plan['max_records'] <= 20000:
        raise ValueError('Diagnostic reader supports explicit scopes up to 20000 rows; never truncate larger tasks')
    started = json.loads(read(base / 'prepare/started.json', 65536, 'started.json'))
    error = json.loads(read(base / 'prepare/error.json', 65536, 'error.json'))
    if (started['plan_digest'] != expected or error['plan_digest'] != expected
            or error['task_id'] != task or error['phase'] != 'prepare' or error['status'] != 'FAILED'):
        raise ValueError('Preparation diagnostic files belong to another attempt')
    if (base / 'prepare/manifest.json').exists() or (base / 'prepare/manifest.json').is_symlink():
        raise ValueError('A successful preparation manifest exists; use normal verification')
    target = project / 'configs/covindb/EGFR_C797_mvp_v1.yaml'
    config = yaml.safe_load(read(target, 65536, 'target.yaml'))
    if files['target.yaml']['sha256'] != plan['assets'].get(str(target)):
        raise ValueError('Target configuration differs from the frozen asset')
    defaults = {'min_mw': 180., 'max_mw': 650., 'min_logp': -1., 'max_logp': 6.,
                'max_tpsa': 160., 'max_rotors': 15., 'max_abs_formal_charge': 2.}
    limits = {key: float(config.get('lead_like', {}).get(key, value)) for key, value in defaults.items()}
    if not all(math.isfinite(value) for value in limits.values()):
        raise ValueError('Nonfinite diagnostic threshold')
    if limits['min_mw'] > limits['max_mw'] or limits['min_logp'] > limits['max_logp']:
        raise ValueError('Contradictory diagnostic thresholds')
    metadata = json.loads(read(base / 'prepare/stage2_selection_manifest.json', 1024 * 1024, 'selection.json'))
    if (metadata['source']['max_rows'] != plan['max_records']
            or metadata['source']['sha256'] != plan['assets'].get(metadata['source']['path'])
            or metadata['target']['config_path'] != str(target)
            or metadata['target']['config_sha256'] != files['target.yaml']['sha256']
            or metadata['selection']['top_n_requested'] != plan['shortlist']):
        raise ValueError('Prescreen metadata differs from the approved scope/assets')
    data = read(base / 'prepare/covindb_master.parquet', 32 * 1024 * 1024, 'master.parquet')
    parquet = pq.ParquetFile(io.BytesIO(data))
    if parquet.metadata.num_rows > plan['max_records']:
        raise ValueError('Diagnostic table exceeds the approved row scope')
    columns = ['eligible', 'mw', 'clogp', 'tpsa', 'rotatable_bonds', 'formal_charge',
               'warhead_count', 'electrophile_atom_index']
    frame = parquet.read(columns=columns).to_pandas()
    numeric = columns[1:]
    if frame[numeric].isna().any().any() or not all(frame[column].map(math.isfinite).all() for column in numeric):
        raise ValueError('Nonfinite diagnostic descriptor')
    if frame.eligible.dtype.kind != 'b':
        raise ValueError('Eligibility must be boolean')
    compatible = frame.warhead_count > 0
    valid = compatible & (frame.electrophile_atom_index >= 0)
    failures = {
        'mw_below_min': frame.mw < limits['min_mw'], 'mw_above_max': frame.mw > limits['max_mw'],
        'clogp_below_min': frame.clogp < limits['min_logp'], 'clogp_above_max': frame.clogp > limits['max_logp'],
        'tpsa_above_max': frame.tpsa > limits['max_tpsa'],
        'rotors_above_max': frame.rotatable_bonds > limits['max_rotors'],
        'charge_above_max': frame.formal_charge.abs() > limits['max_abs_formal_charge'],
    }
    passed = valid.copy()
    for failed in failures.values():
        passed &= ~failed
    if not passed.equals(frame.eligible):
        raise ValueError('Recorded eligibility contradicts descriptor/warhead gates')
    counts = metadata['counts']
    if (len(frame) != counts['unique'] or counts['input_rows'] > plan['max_records']
            or counts['input_rows'] != counts['unique'] + counts['duplicates'] + counts['invalid']
            or int(compatible.sum()) != counts['compatible_warhead']
            or int(valid.sum()) != counts['unambiguous_electrophile']
            or int(passed.sum()) != counts['hard_gate_pass']):
        raise ValueError('Diagnostic counts disagree with prescreen metadata')
    result = {'version': 'preparation-diagnostic-v1', 'task_id': task, 'plan_digest': expected,
              'rows': len(frame), 'input_rows': counts['input_rows'], 'eligible': int(passed.sum()),
              'incompatible': int((~compatible).sum()), 'ambiguous': int((compatible & ~valid).sum()),
              'valid_warhead': int(valid.sum()), 'limits': limits,
              'property_failures': {key: int(value.sum()) for key, value in failures.items()},
              'valid_warhead_property_failures': {key: int((value & valid).sum()) for key, value in failures.items()},
              'files': files, 'scientific_quality_verified': False, 'compute_submitted': False}
    # Re-read the exact witnessed bytes; changing files never become a new
    # silently accepted snapshot during this request.
    paths = {'plan.json': base / 'plan.json', 'started.json': base / 'prepare/started.json',
             'error.json': base / 'prepare/error.json', 'target.yaml': target,
             'selection.json': base / 'prepare/stage2_selection_manifest.json',
             'master.parquet': base / 'prepare/covindb_master.parquet'}
    for name, path in paths.items():
        saved = dict(files[name])
        read(path, saved['bytes'], name)
        if files[name] != saved:
            raise ValueError('Diagnostic evidence changed during read')
    return result


if __name__ == '__main__':
    try:
        print(json.dumps(inspect(*sys.argv[1:]), sort_keys=True, allow_nan=False))
    except Exception:
        # Do not expose molecule values, raw logs or arbitrary file contents.
        print(json.dumps({'diagnostic_unavailable': True}))
        raise SystemExit(1)
