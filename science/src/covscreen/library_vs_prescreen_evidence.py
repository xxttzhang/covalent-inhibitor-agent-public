"""Read registered prescreen evidence and map exact selected molecules for VS.

No ranking, source sampling, chemistry screening or docking is rerun. Selected
parents and reactive atoms are independently checked against frozen source rows.
"""
import csv
from collections import Counter
import hashlib
import io
import json
import math
from pathlib import Path

from rdkit import Chem
import yaml

from .covdock_vs import bound_file, identity, molecules
from .jobs import sha256_file
from .library_vs_prescreen_input import load as load_input, encoded
from .md_preparation_evidence import registered_output

OUTPUTS = {'master': 'covindb_master.parquet', 'rejected': 'covindb_rejected.parquet',
    'candidates_parquet': 'stage2_candidates.parquet', 'candidates_csv': 'stage2_candidates.csv',
    'candidates_sdf': 'stage2_candidates.sdf'}


def integer(value, label, minimum=0, maximum=20000):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError('Invalid prescreen count/index: ' + label)
    return value


def rows(path):
    import pandas as pd
    # Parquet maintains scalar types; pandas JSON removes numpy scalar wrappers.
    return json.loads(pd.read_parquet(path).to_json(orient='records', double_precision=15))


def mapped_candidate(row, mol, source_row, query):
    from .layout import enable_library_first
    enable_library_first()
    from covscreen_library_first.covindb import _parent
    original = str(source_row.get('SMILES') or '').strip()
    parent = Chem.MolFromSmiles(original)
    if parent is None:
        raise ValueError('Selected source molecule cannot be parsed')
    parent = _parent(parent)
    canonical = Chem.MolToSmiles(parent, canonical=True, isomericSmiles=True)
    key = Chem.MolToInchiKey(parent) or hashlib.sha256(canonical.encode()).hexdigest()
    if (row['original_smiles'] != original or row['standardized_smiles'] != canonical
        or row['parent_inchikey'] != key or row['compound_id'] != 'covindb-' + key.lower()
        or row['eligible'] is not True or row['gate_status'] != 'PASS'
        or row['warhead_class'] != 'acrylamide' or row['gate_reason']):
        raise ValueError('Selected prescreen parent/identity/gates differ from the frozen source')
    source_matches = parent.GetSubstructMatches(query, uniquify=True)
    if (not source_matches or len({match[0] for match in source_matches}) != 1
        or row['electrophile_atom_index'] != source_matches[0][0]
        or row['warhead_count'] != len(source_matches)):
        raise ValueError('Prescreen source electrophile identity is ambiguous or changed')
    if (mol.GetNumHeavyAtoms() != parent.GetNumHeavyAtoms() or identity(mol) != canonical
        or any(atom.GetAtomMapNum() for atom in mol.GetAtoms())
        or any(not mol.HasProp(name) or mol.GetProp(name) != str(value) for name, value in {
            '_Name': row['compound_id'], 'COMPOUND_ID': row['compound_id'],
            'INPUT_SDF_INDEX': row['input_sdf_index'], 'PARENT_INCHIKEY': key,
            'WARHEAD_CLASS': 'acrylamide', 'SELECTION_CHANNEL': row['selection_channel']}.items())):
        raise ValueError('Prescreen SDF identity, stereochemistry or row order changed')
    # The saved electrophile index belongs to the source-standardized molecule.
    # SDF was rebuilt from canonical SMILES, so independently rematch its graph.
    pairs = {(match[1], match[0]) for match in mol.GetSubstructMatches(query, uniquify=True)}
    if len(pairs) != 1:
        raise ValueError('VS alpha/beta mapping is not unique in the actual SDF')
    alpha, beta = next(iter(pairs))
    mapped = Chem.Mol(mol)
    mapped.GetAtomWithIdx(alpha).SetAtomMapNum(905)
    mapped.GetAtomWithIdx(beta).SetAtomMapNum(906)
    return mapped, {'candidate_id': row['compound_id'], 'input_sdf_index': row['input_sdf_index'],
        'source_record_index': row['input_index'], 'parent_inchikey': key, 'canonical_isomeric_smiles': canonical,
        'original_alpha_index': alpha, 'original_beta_index': beta, 'alpha_atom_map': 905, 'beta_atom_map': 906,
        'selection_channel': row['selection_channel'], 'channel_score': row['channel_score'],
        'reference_similarity': row['reference_similarity']}


def evaluate(root, state, manager, spec, result):
    from .source_release import verify_source
    from .native_approval import verify_spec
    verify_source(spec)
    verify_spec(spec)
    if (spec.engine != 'covindb-prescreen' or spec.route != 'library-first'
        or spec.project_id != state['workflow_id'] or spec.run_id != state['vs_prescreen']['run_id']
        or spec.parameters.get('vs_prescreen') != state['vs_prescreen']['input']
        or any(getattr(spec, key) != getattr(result, key) for key in ('run_id', 'engine', 'route', 'project_id', 'attempt', 'parent_run_id'))
        or (result.state, result.return_code) not in {('COMPLETED', 0), ('FAILED', 2)}):
        raise ValueError('Prescreen review requires the original terminated native task and complete producer output')
    source, inventory = load_input(root, state['vs_prescreen']['input'])
    output = manager.registry.job_dir(spec.run_id) / 'artifacts'
    hashes = {}
    def file(name):
        path = registered_output(root, output, spec, result, name)
        hashes[str(path.relative_to(root))] = sha256_file(path)
        return path
    manifest = json.loads(file('stage2_selection_manifest.json').read_text())
    if (manifest.get('algorithm_version') != 'covindb-prescreen-v1.3' or manifest.get('schema_version') != '1.0'
        or manifest.get('status') != 'completed' or manifest.get('outputs') != OUTPUTS):
        raise ValueError('Unsupported prescreen output or incomplete producer manifest')
    files = {key: file(name) for key, name in OUTPUTS.items()}
    expected_source = {'path': str(root / source['roles']['library']), 'sha256': source['input_sha256'][source['roles']['library']],
        'size_bytes': (root / source['roles']['library']).stat().st_size, 'max_rows': 20000,
        'record_selection': 'file_order_prefix', 'sampling_seed': None, 'records_scanned': 20000, 'records_read': 20000}
    config_path = root / source['roles']['science_config']
    config = yaml.safe_load(config_path.read_text())
    expected_target = {'config_path': str(config_path), 'config_sha256': sha256_file(config_path),
        'target_id': 'EGFR', 'site_id': 'EGFR_C797', 'reaction_type': 'Michael Addition', 'warhead_class': 'acrylamide'}
    if (any(manifest['source'].get(key) != value for key, value in expected_source.items())
        or manifest['target'] != expected_target or manifest['selection']['top_n_requested'] != 5
        or config['warhead_smarts'] != '[C:1]=[C:2][C:3](=[O:4])[N:5]'):
        raise ValueError('Prescreen result changed its frozen source, target or reaction scope')
    selected = integer(manifest['selection']['top_n_selected'], 'selected', maximum=5)
    counts = manifest['counts']
    for key in ('input_rows', 'parsed', 'standardized', 'unique', 'duplicates', 'invalid', 'compatible_warhead',
                'unambiguous_electrophile', 'hard_gate_pass', 'hard_gate_rejected', 'selected'):
        integer(counts[key], key)
    masters, rejected, candidates = rows(files['master']), rows(files['rejected']), rows(files['candidates_parquet'])
    if (counts['input_rows'] != 20000 or counts['selected'] != selected or len(candidates) != selected
        or counts['unique'] != len(masters) or counts['unique'] + counts['duplicates'] + counts['invalid'] != 20000
        or counts['standardized'] != counts['unique'] + counts['duplicates']
        or counts['hard_gate_pass'] + counts['hard_gate_rejected'] != counts['unique']
        or not selected <= counts['hard_gate_pass'] <= counts['unambiguous_electrophile'] <= counts['compatible_warhead'] <= counts['unique']
        or not counts['standardized'] <= counts['parsed'] <= 20000
        or len(rejected) != counts['invalid'] + counts['duplicates'] + counts['hard_gate_rejected']):
        raise ValueError('Prescreen input/selection/rejection denominators are inconsistent')
    master_index = {integer(row['input_index'], 'master index', maximum=19999): row for row in masters}
    reject_index = {integer(row['input_index'], 'rejection index', maximum=19999): row for row in rejected}
    if (len(master_index) != len(masters) or len(reject_index) != len(rejected)
        or len({row['compound_id'] for row in masters}) != len(masters)
        or len({row['parent_inchikey'] for row in masters}) != len(masters)
        or set(master_index) | set(reject_index) != set(range(20000))
        or sum(row['eligible'] is True for row in masters) != counts['hard_gate_pass']
        or sum(row['warhead_count'] > 0 for row in masters) != counts['compatible_warhead']
        or sum(row['electrophile_atom_index'] >= 0 for row in masters) != counts['unambiguous_electrophile']
        or dict(Counter(row['failure_reason'] for row in rejected)) != counts['rejection_reasons']
        or {index for index, row in master_index.items() if row['eligible'] is not True} != set(master_index) & set(reject_index)):
        raise ValueError('Prescreen source record coverage is incomplete or duplicated')
    with files['candidates_csv'].open(newline='') as stream:
        csv_rows = list(csv.DictReader(stream))
    if len(csv_rows) != selected:
        raise ValueError('Prescreen CSV/Parquet candidate denominators disagree')
    mols = molecules(files['candidates_sdf'], False) if selected else []
    if len(mols) != selected or not selected and files['candidates_sdf'].read_bytes().strip():
        raise ValueError('Prescreen SDF candidate denominator disagrees')
    with (root / source['roles']['library']).open(encoding='utf-8-sig', newline='') as stream:
        source_rows = list(csv.DictReader(stream))
    for index, row in [*master_index.items(), *reject_index.items()]:
        raw = source_rows[index]
        if (row['original_smiles'] != str(raw.get('SMILES') or '').strip()
            or row['source_record_id'] != str(raw.get('') or raw.get('Compound') or index)):
            raise ValueError('Prescreen master/rejection lineage differs from frozen source rows')
    query = Chem.MolFromSmarts(config['warhead_smarts'])
    mapped, values, seen = [], [], set()
    for index, (row, mol, csv_row) in enumerate(zip(candidates, mols, csv_rows)):
        source_index = integer(row['input_index'], 'selected source index', maximum=19999)
        if (source_index in seen or row['input_sdf_index'] != index or row['rank'] != index + 1
            or row['selection_channel'] not in {'exploitation', 'diversity', 'exploration', 'backfill'}
            or source_index not in master_index
            or any(row.get(key) != value for key, value in master_index[source_index].items())):
            raise ValueError('Selected candidate differs from its original master row or order')
        seen.add(source_index)
        for key in ('compound_id', 'input_sdf_index', 'input_index', 'rank', 'standardized_smiles', 'parent_inchikey', 'selection_channel'):
            if csv_row.get(key) != str(row[key]):
                raise ValueError('Prescreen CSV/Parquet candidate identity disagrees')
        if (type(row['channel_score']) not in (int, float) or not math.isfinite(row['channel_score'])
            or type(row['reference_similarity']) not in (int, float) or not 0 <= row['reference_similarity'] <= 1):
            raise ValueError('Prescreen selected score is missing or non-finite')
        molecule, candidate = mapped_candidate(row, mol, source_rows[source_index], query)
        mapped.append(molecule)
        values.append(candidate)
    quality = {'rdkit_parse_rate': counts['parsed'] / 20000,
        'selected_warhead_mapping_rate': 1.0 if selected else 0.0,
        'source_unambiguous_warhead_rate': counts['unambiguous_electrophile'] / counts['compatible_warhead'] if counts['compatible_warhead'] else 0.0,
        'identity_loss_count': 0, 'failure_table_present': True}
    gates = {'rdkit_parse_rate_ge_0_99': quality['rdkit_parse_rate'] >= .99,
        'selected_warhead_mapping_rate_ge_0_98': quality['selected_warhead_mapping_rate'] >= .98,
        'identity_loss_zero': True, 'failure_table_present': True}
    passed = all(gates.values())
    if (manifest['quality'] != quality or manifest['quality_gates'] != gates
        or (result.state, result.return_code) != (('COMPLETED', 0) if passed else ('FAILED', 2))
        or manifest['selection_outcome'] != ('CANDIDATES_SELECTED' if selected else 'NO_CANDIDATES_SELECTED')):
        raise ValueError('Prescreen recorded quality gates, outcome and exit status disagree')
    for name, checksum in {**inventory, **hashes}.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('Prescreen evidence changed during review')
    return {'schema': 'library-vs-prescreen-review-v1', 'workflow_id': state['workflow_id'], 'run_id': spec.run_id,
        'counts': counts, 'candidate_count': selected, 'candidates': values, 'quality_gates': gates,
        'selection_outcome': manifest['selection_outcome'], 'selection': manifest['selection'],
        'vs_input_eligible': bool(selected and passed), 'output_validation': 'REGISTERED_PRESCREEN_IDENTITY_REPLAYED',
        'input_sha256': inventory, 'output_sha256': hashes, 'scientific_acceptance': False, 'md_ready': False}, mapped


def freeze(root, review, mapped):
    """Content-addressed review and graph-mapped input, never compute permission."""
    files = {'review.json': encoded(review)}
    if review['vs_input_eligible']:
        stream = io.StringIO()
        writer = Chem.SDWriter(stream)
        for mol in mapped:
            writer.write(mol)
        writer.flush()
        files['vs-candidates.sdf'] = stream.getvalue().encode()
        writer.close()
    checksum = hashlib.sha256(encoded({name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()})).hexdigest()
    directory = root / 'results/library-vs-prescreen-reviews' / checksum[:32]
    if any(path.is_symlink() for path in (root / 'results', directory.parent, directory)):
        raise ValueError('Prescreen review directory cannot traverse symlinks')
    directory.mkdir(parents=True, exist_ok=True)
    for name, raw in files.items():
        path = directory / name
        if path.exists():
            if bound_file(root, str(path.relative_to(root))).read_bytes() != raw:
                raise ValueError('Saved prescreen review changed')
        else:
            with path.open('xb') as stream:
                stream.write(raw)
    return {'artifact_id': 'library-prescreen-review-' + checksum[:32], 'directory': str(directory.relative_to(root)),
        'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        'candidate_count': review['candidate_count'], 'vs_input_eligible': review['vs_input_eligible']}
