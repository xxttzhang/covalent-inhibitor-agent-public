"""Explicit Library VS subset to independently reviewed native CovDock-LO input."""
from copy import deepcopy
import csv
import hashlib
import io
import json
import os
from uuid import uuid4

from rdkit import Chem

from .approval_scope import normalize_spec
from .covdock_gate import STRICT_ALGORITHM_VERSION
from .covdock_receptor import CovDockRuntimeUnavailable
from .covdock_vs import bound_file, molecules
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .library_vs_prescreen_execution import workflow
from .library_vs_prescreen_input import encoded
from .ligand_parent_evidence import version_lock
from .refinement_input import verify_refinement_input
from . import receptor_environment_review as environment
from .workflow_protocol import changed, revision
from .workflow_store import load as load_state

ACTIONS = {'prepare_vs_refinement', 'read_lo_input', 'review_lo_environment', 'check_lo_runtime'}
STAGES = {'LO_INPUT_REVIEW', 'LO_PLAN_READY'}
BUDGET = {'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16, 'max_runtime_minutes': 180}
DISPATCH = 'library-vs-lo-v1'


def actions(state):
    if state.get('pending') or state.get('lo_execution'):
        return []
    if state['stage'] in STAGES:
        return sorted(ACTIONS)
    record = state.get('vs_execution') or {}
    if (state['stage'] == 'VS_EXECUTION_REVIEW' and record.get('job_identity_verified')
            and not record.get('evidence_error')
            and (record.get('evaluation_summary') or {}).get('passing_candidate_count', 0) > 0):
        return ['prepare_vs_refinement']
    return []


def source(root, state, *, fresh=False):
    from .library_vs_execution import observe
    from .library_vs_evidence import evaluate, load_review
    from .library_vs_input import load
    current = deepcopy(state)
    record = current.get('vs_execution') or {}
    if not record.get('evaluation_artifact'):
        raise ValueError('Library LO requires a registered VS scientific review')
    observe(root, current, record['run_id'])
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(record['run_id']), manager.registry.load_result(record['run_id'])
    review = load_review(root, current, manager, spec, result)
    if fresh and evaluate(root, current, manager, spec, result) != review:
        raise ValueError('VS assessment changed; review again before LO selection')
    return load(root, current), review


def build(root, state, candidate_ids, reason, *, fresh=False):
    if (not isinstance(candidate_ids, list) or not 1 <= len(candidate_ids) <= 5
            or any(not isinstance(name, str) or not name for name in candidate_ids)
            or len(set(candidate_ids)) != len(candidate_ids)):
        raise ValueError('Select one to five distinct Library VS candidate IDs')
    if not isinstance(reason, str) or not 20 <= len(reason.strip()) <= 2000:
        raise ValueError('LO candidate selection requires a concrete reason')
    inputs, review = source(root, state, fresh=fresh)
    originals = molecules(bound_file(root, state['vs_input']['directory'] + '/ligands.sdf'), False)
    ids = inputs['selected_candidate_ids']
    selected = [name for name in ids if name in candidate_ids]
    if len(selected) != len(candidate_ids):
        raise ValueError('LO candidate is outside the current VS input')
    assessed = {row['candidate_id']: row for row in review['candidates']}
    rows = []
    for name in selected:
        result = assessed[name]
        if (result['status'] != 'PASS' or not result['review_eligible'] or result['errors']
                or result['selected_pose_index'] is None):
            raise ValueError('LO requires a jointly passing VS candidate: ' + name)
        poses = [pose for pose in result['poses'] if pose['pose_index'] == result['selected_pose_index']]
        if len(poses) != 1 or not poses[0]['assessment']['review_eligible']:
            raise ValueError('LO selection has no independently passing VS pose')
        index = ids.index(name)
        original = inputs['candidates'][index]
        mol = originals[index]
        if original['candidate_id'] != name or mol.GetProp('_Name') != name:
            raise ValueError('LO molecule differs from the original prescreen/VS order')
        rows.append({'compound_id': name, 'parent_id': original['parent_inchikey'], 'protomer_id': name,
            'input_sdf_index': original['input_sdf_index'], 'source_record_index': original['source_record_index'],
            'standardized_smiles': Chem.MolToSmiles(mol, isomericSmiles=True),
            'route': 'library-first', 'target_id': 'EGFR', 'site_id': 'EGFR_C797',
            'warhead_class': 'acrylamide', 'electrophilic_atom_map': 906,
            'covdock_reaction_type': 'Michael Addition', 'source_engine': 'schrodinger-covdock-vs',
            'source_pose_run_id': review['run_id'], 'selected_pose_record_index': result['selected_pose_index'],
            'prescreen_selection_channel': original['selection_channel'], 'raw_score': poses[0]['covdock_vs_score']})
    stream = io.StringIO(newline='')
    writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    raw = stream.getvalue().encode()
    identity = hashlib.sha256(json.dumps(sorted([[r['input_sdf_index'], r['compound_id']] for r in rows]),
        ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    ref = state['vs_execution']['evaluation_artifact']
    lineage = {**inputs['lineage_sha256'], **review['input_sha256'], **review['output_sha256'],
               ref['path']: ref['manifest_sha256']}
    for name, checksum in lineage.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('Library LO source changed during candidate selection')
    receptor = state['vs_input']['directory'] + '/receptor.pdb'
    lineage[receptor] = inputs['files']['receptor.pdb']
    return {'schema': 'library-vs-lo-input-v1', 'workflow_id': state['workflow_id'],
        'target': state['target'], 'site_selection': state['site_selection'],
        'selected_candidate_ids': selected, 'selection_reason': reason.strip(),
        'source_run_id': review['run_id'], 'prescreen_run_id': inputs['prescreen_run_id'], 'vs_review': ref,
        'screening_receptor': state['screening_receptor'], 'receptor': receptor,
        'attachment_residue': inputs['attachment_residue'], 'reaction_type': 'Michael Addition',
        'candidate_count': len(rows), 'candidate_table_sha256': hashlib.sha256(raw).hexdigest(),
        'identity_sha256': identity, 'lineage_sha256': lineage,
        'review_requirements': inputs['review_requirements'],
        'interpretation': 'VS selects pre-reaction molecules for independent LO redocking; no VS pose is an MD input.',
        'compute_submitted': False, 'execution_authorized': False, 'md_ready': False}, raw


def reference(root, location, payload):
    return {'artifact_id': location.name, 'directory': str(location.relative_to(root)),
        'manifest_sha256': sha256_file(location / 'input.json'),
        'candidate_count': payload['candidate_count'], 'selected_candidate_ids': payload['selected_candidate_ids'],
        'selection_reason': payload['selection_reason'], 'source_run_id': payload['source_run_id'],
        'attachment_residue': payload['attachment_residue'], 'md_ready': False}


def freeze(root, state, candidate_ids, reason):
    payload, table = build(root, state, candidate_ids, reason, fresh=True)
    raw = encoded(payload)
    location = root / 'state/library-lo-inputs' / ('library-lo-input-' + hashlib.sha256(raw).hexdigest()[:32])
    if any(p.is_symlink() for p in (root / 'state', location.parent, location)):
        raise ValueError('Library LO input cannot traverse symlinks')
    location.parent.mkdir(parents=True, exist_ok=True)
    if not location.exists():
        staging = location.with_name('.' + location.name + '-' + uuid4().hex)
        staging.mkdir()
        (staging / 'input.json').write_bytes(raw)
        (staging / 'candidates.csv').write_bytes(table)
        staging.rename(location)
    ref = reference(root, location, payload)
    load(root, {**state, 'lo_input': ref})
    return ref


def load(root, state):
    ref = state['lo_input']
    payload, table = build(root, state, ref['selected_candidate_ids'], ref['selection_reason'])
    location = root / 'state/library-lo-inputs' / ('library-lo-input-' + hashlib.sha256(encoded(payload)).hexdigest()[:32])
    if (ref != reference(root, location, payload) or set(p.name for p in location.iterdir()) != {'input.json', 'candidates.csv'}
            or bound_file(root, str((location / 'input.json').relative_to(root))).read_bytes() != encoded(payload)
            or bound_file(root, str((location / 'candidates.csv').relative_to(root))).read_bytes() != table):
        raise ValueError('Library LO immutable input changed')
    verify_refinement_input(location / 'candidates.csv', payload['candidate_count'], payload['candidate_table_sha256'],
                            payload['identity_sha256'], expected_route='library-first')
    return payload


def inventory(state, payload):
    return {**payload['lineage_sha256'], state['lo_input']['directory'] + '/input.json': state['lo_input']['manifest_sha256'],
        state['lo_input']['directory'] + '/candidates.csv': payload['candidate_table_sha256']}


def reviewed(root, state, payload):
    ref = state.get('lo_environment_review')
    return environment.load(root, ref, state['lo_input'], inventory(state, payload)) if ref else None


def request_for(root, state):
    payload = load(root, state)
    judgment = reviewed(root, state, payload)
    ref = state.get('lo_environment_review')
    lineage = inventory(state, payload)
    if ref:
        lineage[ref['path']] = ref['manifest_sha256']
    count = payload['candidate_count']
    from .library_instance import configured, budget, LO_POLICY
    instance, cpu_limit = configured(root)
    resources = budget(count, stage='lo', cpu_limit=cpu_limit) if instance else {**BUDGET, 'max_candidates': count}
    return JobSubmissionRequest(request_id=state['workflow_id'] + '-lo-' + state['lo_input']['manifest_sha256'][:12]
        + ('-' + ref['manifest_sha256'][:12] if ref else ''), project_id=state['workflow_id'],
        route='library-first', engine='schrodinger-covdock-lo', execution_mode='plan',
        resource_budget=resources, parameters={
            'workflow_id': state['workflow_id'], 'dispatch_policy': LO_POLICY if instance else DISPATCH,
            **({'execution_instance': instance} if instance else {}),
            'candidate_table': state['lo_input']['directory'] + '/candidates.csv',
            'expected_input_count': count, 'expected_input_sha256': payload['candidate_table_sha256'],
            'expected_identity_sha256': payload['identity_sha256'], 'input_score_source': 'core',
            'receptor': payload['receptor'], 'attachment_residue': payload['attachment_residue'],
            'nucleophilic_atom': 'SG', 'reaction_type': 'Michael Addition',
            'gate_algorithm_version': STRICT_ALGORITHM_VERSION, 'refinement_lineage_files': sorted(lineage),
            'prepare_receptor': True, 'final_n': count, 'njobs': min(count, resources['cpu_count']),
            'host': f"localhost:{resources['cpu_count']}", 'resume': False,
            'environment_review_completed': judgment is not None,
            'environment_eligible_for_exploratory_refinement': bool(judgment and judgment['decision'] == 'suitable_for_exploratory_refinement'),
            'environment_review': ref, 'environment_assessment': judgment['decision'] if judgment else 'not_reviewed'})


def retire(state, reason):
    previous = {key: state.pop(key, None) for key in ('lo_plan', 'lo_runtime')}
    if any(previous.values()):
        state.setdefault('lo_plan_history', []).append({**previous, 'reason': reason})


def refresh(root, state):
    if not state.get('lo_input') or state.get('pending'):
        return
    try:
        reviewed(root, state, load(root, state))
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        state['lo_input_error'] = str(error)
    else:
        state.pop('lo_input_error', None)


def step(root, tool, workflow_id, expected_revision, action, args):
    route = workflow()
    location = route.directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load_state(location)
        revision(state, expected_revision)
        if tool != route.RESEARCH or action not in actions(state):
            raise ValueError('Library LO planning action is unavailable at this stage')
        fields = ({'candidate_ids', 'reason'} if action == 'prepare_vs_refinement' else
                  {'environment_review'} if action == 'review_lo_environment' else
                  set(environment.EVIDENCE_READ_SCHEMA) if action == 'read_lo_input' else set())
        valid_fields = not (set(args) - fields) if action == 'read_lo_input' else set(args) == fields
        if not valid_fields:
            raise ValueError('Workflow owns LO inputs and budget')
        if action == 'prepare_vs_refinement':
            ref = freeze(root, state, **args)
            if state.get('lo_input') not in (None, ref):
                raise ValueError('LO subset is frozen; reconsider before replacing it')
            state['lo_input'] = ref
            state.setdefault('lo_environment_review', None)
            if state['stage'] not in STAGES:
                state['stage'] = 'LO_INPUT_REVIEW'
        payload = load(root, state)
        if action == 'review_lo_environment':
            ref = environment.freeze(root, state['lo_input'], inventory(state, payload), args['environment_review'])
            if ref != state.get('lo_environment_review'):
                retire(state, 'LO environment judgment changed')
            state['lo_environment_review'] = ref
            state['stage'] = 'LO_INPUT_REVIEW'
        if action == 'check_lo_runtime':
            request = request_for(root, state)
            try:
                spec = normalize_spec(JobManager(root)._prepare_submission(request))
            except CovDockRuntimeUnavailable as error:
                retire(state, 'LO runtime unavailable')
                state['lo_runtime'] = {'state': 'WAITING_RUNTIME_CONFIGURATION', 'reason': str(error), 'compute_submitted': False}
                state['stage'] = 'LO_INPUT_REVIEW'
            else:
                raw = encoded(spec)
                checksum = hashlib.sha256(raw).hexdigest()
                private = root / 'state/library-lo-plans' / (checksum + '.json')
                if any(p.is_symlink() for p in (root / 'state', private.parent, private)):
                    raise ValueError('LO private plan cannot traverse symlinks')
                private.parent.mkdir(parents=True, exist_ok=True)
                if not private.exists():
                    with os.fdopen(os.open(private, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as stream:
                        stream.write(raw)
                if private.read_bytes() != raw or private.stat().st_mode & 0o077:
                    raise ValueError('LO private plan changed or is not private')
                ref = {'path': str(private.relative_to(root)), 'sha256': checksum}
                if state.get('lo_plan') not in (None, ref):
                    raise ValueError('LO runtime plan changed; review and replan explicitly')
                state['lo_plan'] = ref
                accepted = request.parameters['environment_eligible_for_exploratory_refinement']
                state['lo_runtime'] = {'state': 'LO_PLAN_COMPILED' if accepted else 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED',
                    'plan_sha256': checksum, 'budget': request.resource_budget.model_dump(mode='json'),
                    'compute_submitted': False, 'native_approval_required': True, 'license_checkout_verified': False}
                state['stage'] = 'LO_PLAN_READY' if accepted else 'LO_INPUT_REVIEW'
        evidence = environment.read_evidence(root, inventory(state, payload), **args) if action == 'read_lo_input' else None
        state.pop('lo_input_error', None)
        changed(location, state, action)
        return route.response(state, {'lo_input': state['lo_input'], 'review_requirements': payload['review_requirements'],
            'evidence_inventory': inventory(state, payload), 'environment_review': reviewed(root, state, payload),
            **({'evidence': evidence} if evidence is not None else {}),
            'compute_submitted': False, 'execution_authorized': False})
