"""Freeze coordinate-independent selected parents for the shared LO executor."""
import csv
import hashlib
import io
import json
from pathlib import Path

from .batch_input import validate
from .batch_vs import evaluate


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def table(review, manifest):
    originals = {row['candidate_id']: row for row in manifest['candidates']}
    selected = review['lo_selection']['selected']
    if not 1 <= len(selected) <= 12 or len({r['candidate_id'] for r in selected}) != len(selected):
        raise ValueError('LO input requires one to twelve distinct eligible candidates')
    rows = []
    for choice in selected:
        original = originals[choice['candidate_id']]
        if original['parent_id'] != choice['parent_id'] or original['input_index'] != choice['input_index']:
            raise ValueError('LO selection differs from its original candidate')
        rows.append({'compound_id': original['candidate_id'], 'parent_id': original['parent_id'],
            'protomer_id': original['candidate_id'], 'input_sdf_index': original['input_index'],
            'standardized_smiles': original['mapped_smiles'], 'route': 'de-novo',
            'target_id': 'EGFR', 'site_id': 'EGFR_C797', 'warhead_class': 'acrylamide',
            'electrophilic_atom_map': 906, 'covdock_reaction_type': 'Michael Addition',
            'source_engine': 'schrodinger-covdock-vs', 'source_pose_run_id': review['run_id'],
            'selected_pose_record_index': choice['vs_pose_index'], 'raw_score': choice['vs_score']})
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    identity = hashlib.sha256(json.dumps(sorted([[r['input_sdf_index'], r['compound_id']] for r in rows]),
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    return stream.getvalue().encode(), identity


def freeze(root, checksum):
    from covscreen.refinement_input import verify_refinement_input
    root = Path(root).resolve(strict=True)
    review = evaluate(root, checksum)
    source = root / 'state/de-novo-batches' / ('batch-' + checksum)
    manifest, _ = validate(source, checksum)
    raw, identity = table(review, manifest)
    payload = {'schema': 'denovo-batch-lo-input-v1', 'task_id': manifest['task_id'],
        'workflow_id': review['workflow_id'], 'vs_run_id': review['run_id'],
        'batch_manifest_sha256': checksum, 'review_sha256': hashlib.sha256(encoded(review)).hexdigest(),
        'candidate_count': review['lo_selection']['selected_count'],
        'candidate_table_sha256': hashlib.sha256(raw).hexdigest(), 'identity_sha256': identity,
        'receptor': str((source / 'covdock-receptor/receptor.pdb').relative_to(root)), 'attachment_residue': 'A:797',
        'lineage_sha256': {**review['input_sha256'], **review['output_sha256']},
        'compute_submitted': False, 'generated_coordinates_reused': False, 'vs_coordinates_reused': False}
    digest = hashlib.sha256(encoded(payload)).hexdigest()
    folder = root / 'state/de-novo-lo-inputs' / digest
    for name, content in [('review.json', encoded(review)), ('candidates.csv', raw), ('input.json', encoded(payload))]:
        path = folder / name
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ValueError('LO input cannot traverse symlinks')
        folder.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(content)
        if path.read_bytes() != content:
            raise ValueError('Frozen LO input changed')
    verify_refinement_input(folder / 'candidates.csv', payload['candidate_count'],
        payload['candidate_table_sha256'], identity, expected_route='de-novo')
    return {**payload, 'directory': str(folder.relative_to(root)), 'manifest_sha256': digest}


def load(root, digest):
    import re
    from covscreen.refinement_input import verify_refinement_input
    if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Exact LO input digest required')
    root = Path(root).resolve(strict=True)
    folder = root / 'state/de-novo-lo-inputs' / digest
    def read(path):
        if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
            raise ValueError('LO source must be regular immutable data')
        return path.read_bytes()
    raw = read(folder / 'input.json')
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError('LO manifest changed')
    payload = json.loads(raw)
    if payload['schema'] != 'denovo-batch-lo-input-v1':
        raise ValueError('Unsupported LO input')
    review_raw = read(folder / 'review.json')
    if hashlib.sha256(review_raw).hexdigest() != payload['review_sha256']:
        raise ValueError('LO review changed')
    source = root / 'state/de-novo-batches' / ('batch-' + payload['batch_manifest_sha256'])
    manifest, _ = validate(source, payload['batch_manifest_sha256'])
    review = json.loads(review_raw)
    expected, identity = table(review, manifest)
    if (read(folder / 'candidates.csv') != expected or identity != payload['identity_sha256']
            or payload['task_id'] != manifest['task_id'] or payload['vs_run_id'] != review['run_id']
            or payload['workflow_id'] != review['workflow_id']):
        raise ValueError('LO input no longer matches its VS selection')
    for name, checksum in payload['lineage_sha256'].items():
        path = root / name
        if not path.resolve().is_relative_to(root) or hashlib.sha256(read(path)).hexdigest() != checksum:
            raise ValueError('LO source lineage changed')
    verify_refinement_input(folder / 'candidates.csv', payload['candidate_count'],
        payload['candidate_table_sha256'], identity, expected_route='de-novo')
    if payload.get('retry_of'):
        from .batch_lo_retry import validate as validate_retry
        validate_retry(root, payload)
    return payload, folder
