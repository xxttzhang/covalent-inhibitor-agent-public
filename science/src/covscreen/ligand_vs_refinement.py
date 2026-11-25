"""Freeze an explicit VS-reviewed subset for independently approved refinement.

This transfers candidate identities, not a VS pose into MD or an old approval.
The original pre-reaction molecules are re-docked by the existing LO engine.
"""
import csv
from copy import deepcopy
import hashlib
import io
import json
from uuid import uuid4

from .covdock_vs import bound_file
from .execution import JobManager
from .jobs import sha256_file
from .ligand_candidate_table import candidate_table_row
from .ligand_design import design_folder, seal, verify
from .ligand_first_inputs import load_campaign
from .ligand_parent_evidence import version_lock
from .rdkit_covalentizer import CovalentizedCandidate
from .refinement_input import verify_refinement_input
from .workflow_protocol import changed, revision
from .workflow_store import load as load_state


SOURCE = 'registered-ligand-vs-review-v1'
STAGE = 'VS_REFINEMENT_READY'
ACTIONS = {'prepare_vs_refinement', 'prepare_vs_refinement_target'}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def source(root, state, *, fresh=False):
    from .ligand_vs_execution import observe
    from .ligand_vs_evidence import evaluate, load_review
    from .ligand_vs_input import load as load_input
    # Pure lineage validation must not reset the public upstream assessment or
    # change the current downstream stage as observe() normally does.
    state = deepcopy(state)
    record = state.get('vs_execution') or {}
    if not record.get('evaluation_artifact') or record.get('evidence_error'):
        raise ValueError('Refinement requires a verified current VS review')
    stage = state['stage']
    try:
        observe(root, state, record['run_id'])
    finally:
        state['stage'] = stage
    manager = JobManager(root)
    spec = manager.registry.load_spec(record['run_id'])
    result = manager.registry.load_result(record['run_id'])
    review = load_review(root, state, manager, spec, result)
    if fresh and evaluate(root, state, manager, spec, result) != review:
        raise ValueError('VS review changed; evaluate it again before candidate selection')
    return load_input(root, state), review


def build(root, state, candidate_ids, reason, *, fresh=False):
    if (not isinstance(candidate_ids, list) or not 1 <= len(candidate_ids) <= 3
            or any(not isinstance(name, str) or not name for name in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)):
        raise ValueError('Select one to three distinct VS candidate IDs')
    if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 8000:
        raise ValueError('Record a reason for the VS refinement subset')
    inputs, review = source(root, state, fresh=fresh)
    original_ids = inputs['selected_candidate_ids']
    selected = [name for name in original_ids if name in candidate_ids]
    if len(selected) != len(candidate_ids):
        raise ValueError('Refinement candidate is outside the original VS input')
    directory = design_folder(root, state['design_id'])
    campaign = load_campaign(directory / 'case/campaign.json')
    candidates = {row['candidate_id']: CovalentizedCandidate.model_validate(row)
        for row in json.loads((directory / 'generation/candidates.json').read_text())['candidates']}
    assessed = {row['candidate_id']: row for row in review['candidates']}
    rows = []
    for name in selected:
        candidate = assessed[name]
        if (not candidate['review_eligible'] or candidate['status'] != 'PASS'
                or candidate['selected_pose_index'] is None or candidate['errors']):
            raise ValueError('Refinement requires a jointly passing VS candidate: ' + name)
        poses = [pose for pose in candidate['poses'] if pose['pose_index'] == candidate['selected_pose_index']]
        if len(poses) != 1 or not poses[0]['assessment']['review_eligible']:
            raise ValueError('Selected VS pose is not independently eligible')
        row = candidate_table_row(candidates[name], campaign, original_ids.index(name))
        row.update(route='ligand-first', source_engine='schrodinger-covdock-vs',
            source_pose_run_id=review['run_id'], pose_geometry_status='PASS',
            selected_pose_record_index=candidate['selected_pose_index'],
            raw_score=poses[0]['covdock_vs_score'])
        rows.append(row)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    raw = stream.getvalue().encode()
    identity = hashlib.sha256(json.dumps(sorted([[row['input_sdf_index'], row['compound_id']] for row in rows]),
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    ref = state['vs_execution']['evaluation_artifact']
    lineage = {**inputs['lineage_sha256'], **review['input_sha256'], **review['output_sha256'],
               ref['path']: ref['manifest_sha256']}
    for name, checksum in lineage.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('VS refinement source changed during selection')
    payload = {'schema': 'ligand-vs-refinement-input-v1', 'source_kind': SOURCE,
        'workflow_id': state['workflow_id'], 'design_id': state['design_id'], 'evidence_id': state['evidence_id'],
        'source_run_id': review['run_id'], 'source_manifests': lineage, 'review': ref,
        'selected_candidate_ids': selected, 'selection_reason': reason.strip(),
        'candidate_table': 'candidates.csv', 'input_count': len(rows),
        'input_sha256': hashlib.sha256(raw).hexdigest(), 'identity_sha256': identity,
        'refinement_submitted': False, 'md_ready': False, 'grants_resource_approval': False,
        'interpretation': 'VS selects pre-reaction molecules for independent LO redocking; no VS pose is an MD input.'}
    return payload, raw


def reference(root, location, payload):
    return {'source_kind': SOURCE, 'artifact_id': location.name,
        'manifest_sha256': sha256_file(location / 'manifest.json'),
        'candidate_table': str((location / 'candidates.csv').relative_to(root)),
        **{key: payload[key] for key in ('source_run_id', 'selected_candidate_ids', 'selection_reason',
            'input_count', 'input_sha256', 'identity_sha256', 'refinement_submitted', 'md_ready')}}


def freeze(root, state, candidate_ids, reason):
    payload, raw = build(root, state, candidate_ids, reason, fresh=True)
    parent = root / 'results/ligand-refinement-inputs'
    location = parent / ('ligand-vs-refinement-' + hashlib.sha256(encoded(payload)).hexdigest()[:24])
    if any(path.is_symlink() for path in (root / 'results', parent, location)):
        raise ValueError('VS refinement input cannot traverse symbolic links')
    parent.mkdir(parents=True, exist_ok=True)
    if not location.exists():
        staging = parent / ('.' + location.name + '-' + uuid4().hex)
        staging.mkdir()
        (staging / 'refinement-input.json').write_bytes(encoded(payload))
        (staging / 'candidates.csv').write_bytes(raw)
        seal(staging)
        staging.rename(location)
    ref = reference(root, location, payload)
    load(root, {**state, 'refinement_input': ref})
    return ref


def load(root, state):
    ref = state['refinement_input']
    payload, raw = build(root, state, ref['selected_candidate_ids'], ref['selection_reason'])
    identifier = 'ligand-vs-refinement-' + hashlib.sha256(encoded(payload)).hexdigest()[:24]
    location = root / 'results/ligand-refinement-inputs' / identifier
    manifest = bound_file(root, str((location / 'manifest.json').relative_to(root)))
    if sha256_file(manifest) != ref['manifest_sha256']:
        raise ValueError('VS refinement manifest changed')
    verify(location)
    expected = reference(root, location, payload)
    if any(ref.get(key) != value for key, value in expected.items()):
        raise ValueError('VS refinement binding differs from the current verified subset')
    if ((location / 'refinement-input.json').read_bytes() != encoded(payload)
            or (location / 'candidates.csv').read_bytes() != raw):
        raise ValueError('VS refinement candidate bytes changed')
    verify_refinement_input(location / 'candidates.csv', payload['input_count'], payload['input_sha256'],
                            payload['identity_sha256'], expected_route='ligand-first')
    return payload


def revalidate_saved(root, state):
    ref = state.get('refinement_input') or {}
    if ref.get('source_kind') != SOURCE:
        return
    try:
        load(root, state)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        ref['source_error'] = str(error)
        return str(error)
    ref.pop('source_error', None)


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response
    from .ligand_refinement_target import freeze as freeze_target
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load_state(location)
        revision(state, expected_revision)
        if state.get('pending') or state['stage'] not in {'VS_EXECUTION_REVIEW', STAGE}:
            raise ValueError('VS refinement requires an idle reviewed branch')
        if action == 'prepare_vs_refinement':
            if set(arguments) != {'candidate_ids', 'reason'}:
                raise ValueError('Select explicit VS candidate IDs and a reason')
            ref = freeze(root, state, **arguments)
            previous = state.get('refinement_input')
            if previous and any(previous.get(key) != value for key, value in ref.items()):
                raise ValueError('Refinement subset is frozen; reconsider before replacing it')
            state['refinement_input'] = previous or ref
            state['stage'] = STAGE
        elif action == 'prepare_vs_refinement_target':
            if arguments or state['stage'] != STAGE:
                raise ValueError('Freeze the VS refinement candidates before target preparation')
            load(root, state)
            handoff = state['refinement_input']
            target = freeze_target(root, state)
            if handoff.get('target') not in (None, target):
                raise ValueError('Existing refinement target changed')
            handoff['target'] = target
            handoff.pop('target_error', None)
        else:
            raise ValueError('Unknown VS refinement action')
        changed(location, state, action)
        return response(state, {'refinement_input': state['refinement_input'],
            'compute_submitted': False, 'execution_authorized': False})
