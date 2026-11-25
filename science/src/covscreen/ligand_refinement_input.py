"""Freeze reviewed Ligand-first candidates for the existing refinement engines."""
import csv
import hashlib
import io
import json
import re
from uuid import uuid4

from .jobs import sha256_file
from .ligand_design import design_folder, seal, verify
from .ligand_first_inputs import load_campaign
from .rdkit_covalentizer import CovalentizedCandidate
from .ligand_candidate_table import candidate_table_row
from .refinement_input import verify_refinement_input


def load_refinement_input(root, state):
    """Read a frozen handoff without regenerating it or authorizing computation.

    The caller still owns fresh signed-job and scientific-evidence replay.
    """
    binding = state.get('refinement_input')
    if isinstance(binding, dict) and binding.get('source_kind') == 'sealed-ligand-direct-lo-v1':
        from .ligand_direct_lo import load
        return load(root, state)
    if isinstance(binding, dict) and binding.get('source_kind') == 'registered-ligand-vs-review-v1':
        from .ligand_vs_refinement import load
        return load(root, state)
    if not isinstance(binding, dict) or not re.fullmatch(
            r'ligand-refinement-[a-f0-9]{24}', str(binding.get('artifact_id', ''))):
        raise ValueError('Missing or invalid frozen refinement binding')
    directory = root / 'results/ligand-refinement-inputs' / binding['artifact_id']
    for path in (directory, *directory.parents):
        if path == root:
            break
        if path.is_symlink():
            raise ValueError('Frozen refinement input cannot be a symbolic link')
    for name in ('manifest.json', 'refinement-input.json', 'candidates.csv'):
        if (directory / name).is_symlink():
            raise ValueError('Frozen refinement input cannot be a symbolic link')
    if sha256_file(directory / 'manifest.json') != binding.get('manifest_sha256'):
        raise ValueError('Frozen refinement manifest changed')
    verify(directory)
    payload = json.loads((directory / 'refinement-input.json').read_text())
    asset_id = 'ligand-refinement-' + hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    record = state.get('pose_execution') or {}
    evaluation = record.get('evaluation') or {}
    expected = {
        'schema': 'ligand-refinement-input-v1',
        'workflow_id': state['workflow_id'], 'design_id': state['design_id'],
        'evidence_id': state['evidence_id'], 'source_run_id': record.get('run_id'),
        'source_manifests': record.get('source_manifests'),
        'source_output_sha256': evaluation.get('output_sha256'),
        'geometry_review_sha256': hashlib.sha256(json.dumps(evaluation, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
        'candidate_table': 'candidates.csv', 'refinement_submitted': False, 'md_ready': False,
    }
    if (record.get('evaluation_verification') != 'verified' or asset_id != binding['artifact_id']
            or any(payload.get(key) != value for key, value in expected.items())):
        raise ValueError('Frozen refinement input does not match current workflow evidence')
    for key in ('source_run_id', 'input_count', 'input_sha256', 'identity_sha256',
                'refinement_submitted', 'md_ready'):
        if binding.get(key) != payload.get(key):
            raise ValueError('Frozen refinement binding changed: ' + key)
    table = directory / 'candidates.csv'
    if binding.get('candidate_table') != str(table.relative_to(root)):
        raise ValueError('Frozen refinement table path changed')
    verify_refinement_input(table, payload['input_count'], payload['input_sha256'],
                            payload['identity_sha256'])
    return payload


def freeze_refinement_input(root, state):
    """Caller must freshly validate signed job and recompute its geometry review."""
    record = state['pose_execution']
    evaluation = record.get('evaluation')
    if record.get('evaluation_verification') != 'verified' or not evaluation:
        raise ValueError('Refinement requires freshly verified pose geometry evidence')
    from .ligand_pose_evidence import review_eligible
    selected = [row for row in evaluation['candidates'] if review_eligible(row)]
    if not 1 <= len(selected) <= 3:
        raise ValueError('No bounded set of geometry-passing candidates is available for refinement')
    directory = design_folder(root, state['design_id'])
    campaign = load_campaign(directory / 'case/campaign.json')
    candidates = {row['candidate_id']: CovalentizedCandidate.model_validate(row)
                  for row in json.loads((directory / 'generation/candidates.json').read_text())['candidates']}
    identities = json.loads((directory / 'pose-plan/input-binding.json').read_text())['selected_candidate_ids']
    rows = []
    for selected_pose in selected:
        identity = selected_pose['candidate_id']
        if identity not in candidates or identity not in identities or selected_pose['selected_pose_index'] is None:
            raise ValueError('Refinement candidate does not belong to the original pose denominator')
        row = candidate_table_row(candidates[identity], campaign, identities.index(identity))
        row.update(route='ligand-first', source_pose_run_id=record['run_id'],
                   pose_geometry_status='PASS',
                   selected_pose_record_index=selected_pose['selected_pose_record_index'],
                   raw_score=selected_pose['selected_pose']['carsidock_score'])
        rows.append(row)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    contents = stream.getvalue().encode()
    checksum = hashlib.sha256(contents).hexdigest()
    identity = hashlib.sha256(json.dumps(sorted([[row['input_sdf_index'], row['compound_id']] for row in rows]),
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    payload = {'schema': 'ligand-refinement-input-v1', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'evidence_id': state['evidence_id'], 'source_run_id': record['run_id'],
        'source_manifests': record['source_manifests'], 'source_output_sha256': evaluation['output_sha256'],
        'geometry_review_sha256': hashlib.sha256(json.dumps(evaluation, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest(),
        'input_count': len(rows), 'input_sha256': checksum, 'identity_sha256': identity,
        'candidate_table': 'candidates.csv', 'selection': 'all geometry-passing candidates in frozen review order',
        'refinement_submitted': False, 'md_ready': False}
    asset_id = 'ligand-refinement-' + hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    parent = root / 'results/ligand-refinement-inputs'
    parent.mkdir(parents=True, exist_ok=True)
    destination = parent / asset_id
    if destination.is_symlink():
        raise ValueError('Frozen refinement input cannot be a symbolic link')
    if not destination.exists():
        staging = parent / ('.' + asset_id + '-' + uuid4().hex[:12])
        staging.mkdir()
        (staging / 'candidates.csv').write_bytes(contents)
        (staging / 'refinement-input.json').write_text(json.dumps(payload, indent=2) + '\n')
        seal(staging)
        staging.rename(destination)
    verify(destination)
    if json.loads((destination / 'refinement-input.json').read_text()) != payload:
        raise ValueError('Frozen refinement input metadata changed')
    verify_refinement_input(destination / 'candidates.csv', len(rows), checksum, identity)
    binding = {'artifact_id': asset_id, 'manifest_sha256': sha256_file(destination / 'manifest.json'),
        'candidate_table': str((destination / 'candidates.csv').relative_to(root)),
        'source_run_id': record['run_id'], 'input_count': len(rows), 'input_sha256': checksum,
        'identity_sha256': identity, 'refinement_submitted': False, 'md_ready': False}
    load_refinement_input(root, {**state, 'refinement_input': binding})
    return binding
