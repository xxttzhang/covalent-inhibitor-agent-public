"""Freeze graph-designed candidates for LO without inventing upstream poses."""
import csv
import hashlib
import io
import json
from uuid import uuid4

from .covdock_vs import bound_file
from .jobs import sha256_file
from .ligand_candidate_source import read_design
from .ligand_candidate_table import candidate_table_row
from .ligand_design import seal, verify
from .ligand_parent_evidence import version_lock
from .rdkit_covalentizer import CovalentizedCandidate
from .refinement_input import verify_refinement_input
from .workflow_protocol import changed, revision
from .workflow_store import load as load_state

SOURCE = 'sealed-ligand-direct-lo-v1'
STAGE = 'DIRECT_LO_READY'
ACTIONS = {'prepare_direct_lo', 'prepare_direct_lo_target'}


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def available(state):
    from .ligand_vs_input import available as design_available
    return (design_available(state) and not any(state.get(key) for key in
        ('vs_input', 'vs_receptor', 'refinement_input', 'pose_execution', 'af3_execution', 'covdock_execution')))


def build(root, state):
    source = read_design(root, state)
    indexes = source['admitted_indices']
    from .ligand_docking import verified_decision
    decision = verified_decision(state, len(indexes))
    if decision['selected_route'] != 'direct_lo':
        raise ValueError('Direct LO requires 1–6 admitted candidates; routing decision: ' +
                         decision['selected_route'])
    rows = []
    for index in indexes:
        candidate = CovalentizedCandidate.model_validate(source['rows'][index])
        row = candidate_table_row(candidate, source['campaign'], index)
        row.update(route='ligand-first', source_engine='graph-design',
                   pose_geometry_status='NOT_EVALUATED')
        rows.append(row)
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    raw = stream.getvalue().encode()
    identity = hashlib.sha256(json.dumps(sorted([[r['input_sdf_index'], r['compound_id']] for r in rows]),
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    payload = {'schema': 'ligand-direct-lo-input-v1', 'source_kind': SOURCE,
        'workflow_id': state['workflow_id'], 'design_id': state['design_id'],
        'evidence_id': state['evidence_id'], 'source_manifests': source['lineage'],
        'generated_candidate_count': len(source['rows']),
        'selected_candidate_ids': [r['compound_id'] for r in rows],
        'candidate_deferrals': source['deferrals'], 'routing_decision': decision,
        'candidate_table': 'candidates.csv', 'input_count': len(rows),
        'input_sha256': hashlib.sha256(raw).hexdigest(), 'identity_sha256': identity,
        'refinement_submitted': False, 'md_ready': False, 'grants_resource_approval': False,
        'interpretation': 'Original graph-designed molecules for independent preparation and LO; no prior pose assessment.'}
    return payload, raw


def reference(root, location, payload):
    return {'source_kind': SOURCE, 'artifact_id': location.name,
        'manifest_sha256': sha256_file(location / 'manifest.json'),
        'candidate_table': str((location / 'candidates.csv').relative_to(root)),
        **{key: payload[key] for key in ('selected_candidate_ids', 'candidate_deferrals',
            'generated_candidate_count', 'routing_decision', 'input_count', 'input_sha256',
            'identity_sha256', 'refinement_submitted', 'md_ready')}}


def freeze(root, state):
    payload, raw = build(root, state)
    parent = root / 'results/ligand-refinement-inputs'
    location = parent / ('ligand-direct-lo-' + hashlib.sha256(encoded(payload)).hexdigest()[:24])
    if any(path.is_symlink() for path in (root / 'results', parent, location)):
        raise ValueError('Direct LO input cannot traverse symbolic links')
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
    payload, raw = build(root, state)
    identifier = 'ligand-direct-lo-' + hashlib.sha256(encoded(payload)).hexdigest()[:24]
    location = root / 'results/ligand-refinement-inputs' / identifier
    manifest = bound_file(root, str((location / 'manifest.json').relative_to(root)))
    if sha256_file(manifest) != ref['manifest_sha256']:
        raise ValueError('Direct LO manifest changed')
    verify(location)
    if any(ref.get(key) != value for key, value in reference(root, location, payload).items()):
        raise ValueError('Direct LO binding differs from the verified design')
    if ((location / 'refinement-input.json').read_bytes() != encoded(payload)
            or (location / 'candidates.csv').read_bytes() != raw):
        raise ValueError('Direct LO candidate bytes changed')
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
        if arguments or state.get('pending') or not (available(state) or state['stage'] == STAGE):
            raise ValueError('Direct LO preparation requires an idle current design')
        if action == 'prepare_direct_lo':
            ref = freeze(root, state)
            previous = state.get('refinement_input')
            if previous and any(previous.get(key) != value for key, value in ref.items()):
                raise ValueError('Direct LO input is frozen; reconsider before replacing it')
            state['refinement_input'] = previous or ref
            state['stage'] = STAGE
        elif action == 'prepare_direct_lo_target':
            if state['stage'] != STAGE:
                raise ValueError('Freeze direct LO candidates before target preparation')
            load(root, state)
            handoff = state['refinement_input']
            target = freeze_target(root, state)
            if handoff.get('target') not in (None, target):
                raise ValueError('Existing direct LO target changed')
            handoff['target'] = target
            handoff.pop('target_error', None)
        else:
            raise ValueError('Unknown direct LO action')
        changed(location, state, action)
        return response(state, {'refinement_input': state['refinement_input'],
            'compute_submitted': False, 'execution_authorized': False})
