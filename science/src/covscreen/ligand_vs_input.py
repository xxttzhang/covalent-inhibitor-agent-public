"""Bind VS preparation to a current Ligand design, without a CarsiDock parent.

The projected receptor is an explicit, auditable preparation input. It is not
a repaired receptor or an environment suitability decision. No job is launched.
Private runtime plans are stored separately from model-visible workflow output.
"""
import hashlib
import io
import json
import os
from pathlib import Path
from uuid import uuid4

from rdkit import Chem

from .approval_scope import normalize_spec
from .covdock_vs import bound_file, identity, molecules, unresolved_stereo
from .covdock_vs_preparation import freeze as freeze_preparation
from .covdock_receptor import CovDockRuntimeUnavailable
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .ligand_design import design_folder, verify
from .ligand_first_inputs import load_campaign, build_parent_pose_mol
from .ligand_parent_evidence import require_latest, version_lock
from .ligand_refinement_target import target_drafts, encoded
from .rdkit_covalentizer import CovalentizedCandidate, evaluate_chemistry_quality
from .target_hypothesis import folder as evidence_folder, read as read_evidence
from .workflow_protocol import changed, revision
from .workflow_store import load as load_state


ACTIONS = {'prepare_vs_input', 'read_construct_evidence', 'review_construct', 'select_receptor_conformers', 'read_vs_input', 'review_vs_environment', 'check_vs_runtime'}
STAGES = {'NEEDS_POSE_EVALUATION', 'POSE_PLAN_READY', 'POSE_INPUTS_BLOCKED',
          'POSE_EXECUTION_REVIEW'}
CONSTRUCT_ACTIONS = {'read_construct_evidence', 'review_construct', 'select_receptor_conformers'}


def construct_available(state):
    from .ligand_direct_lo import SOURCE, STAGE
    return available(state) or (state.get('stage') == STAGE and not state.get('pending')
        and (state.get('refinement_input') or {}).get('source_kind') == SOURCE)


def construct_frozen(state):
    from .ligand_direct_lo import SOURCE
    handoff = state.get('refinement_input') or {}
    # Graph candidate identity is independent of receptor construct/conformers.
    # All historical pose-derived handoffs retain their original freeze boundary.
    return bool(state.get('vs_input') or state.get('vs_receptor') or
        (handoff and (handoff.get('source_kind') != SOURCE or handoff.get('target'))))


def available(state):
    # Reported branches must use the existing explicit reopen operation. It
    # restores only a resumable, sealed design; zero-candidate reports cannot
    # advertise VS preparation merely because a design ID is still present.
    if (state.get('pending') or not state.get('design_id') or state['stage'] not in STAGES
            or state.get('historical_md_source') or state.get('vs_execution')):
        return False
    # Do not fork a running or uncertain historical computation.
    for key in ('pose_execution', 'af3_execution', 'covdock_execution', 'md_graph_execution'):
        record = state.get(key) or {}
        if record.get('run_id') and record.get('job_state') not in {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}:
            return False
    return True


def sealed_sources(root, directory, required):
    manifest = bound_file(root, str((directory / 'manifest.json').relative_to(root)))
    verify(directory)
    entries = json.loads(manifest.read_text())['files']
    inventory = {str(manifest.relative_to(root)): sha256_file(manifest)}
    for path in required:
        name = str(path.relative_to(root))
        actual = sha256_file(bound_file(root, name))
        if entries.get(str(path.relative_to(directory))) != actual:
            raise ValueError('VS study source is not sealed: ' + name)
        inventory[name] = actual
    return inventory


def construct_evidence(root, state):
    """Read the current sealed construct without generating VS input or approval."""
    require_latest(root, state['evidence_id'])
    source = evidence_folder(root, state['evidence_id'])
    evidence = read_evidence(root, state['evidence_id'])
    directory = design_folder(root, state['design_id'])
    verify(directory)
    record = json.loads((directory / 'design.json').read_text())
    if (record['design_id'] != state['design_id'] or record['evidence_id'] != state['evidence_id']
            or record['evidence_manifest_sha256'] != sha256_file(source / 'manifest.json')
            or evidence['state'] != 'EVIDENCE_READY'
            or any(evidence['evidence'].get(key) != value for key, value in state['target'].items())):
        raise ValueError('Construct evidence does not belong to the current study')
    uniprot = json.loads((directory / 'uniprot.json').read_text())
    if (uniprot.get('primaryAccession') != evidence['evidence']['identity'].get('uniprot_id')
            or uniprot.get('sequence', {}).get('value') != evidence['evidence']['identity'].get('sequence')
            or uniprot.get('organism', {}).get('taxonId') != state['target']['taxon']):
        raise ValueError('Construct target identity differs from current evidence')
    result = target_drafts(directory / 'case', uniprot, construct_evidence_only=True)
    result['design_manifest_sha256'] = sha256_file(directory / 'manifest.json')
    result['evidence_manifest_sha256'] = sha256_file(source / 'manifest.json')
    return result


def build(root, state, *, use_prepared=True):
    """Re-read current scientific sources, including graph chemistry and mapping."""
    from .ligand_candidate_source import read_design
    source = read_design(root, state)
    design, lineage, uniprot, campaign = (source[key] for key in
        ('design', 'lineage', 'uniprot', 'campaign'))
    rows, mols = source['rows'], source['molecules']
    decision = None
    limit = 3  # Historical inputs retain their exact bytes and selection.
    if state.get('docking_decision'):
        from .ligand_docking import verified_decision
        decision = verified_decision(state, len(source['admitted_indices']))
        if decision['selected_route'] != 'vs_then_lo':
            raise ValueError('Frozen docking route does not select VS')
        limit = len(source['admitted_indices'])
        if limit > 100:
            raise ValueError('VS supports at most 100 candidates per signed task; partition the design explicitly before routing. No candidates were truncated.')
    stream = io.StringIO()
    writer = Chem.SDWriter(stream)
    selected = []
    deferred = []
    for index, (row, mol) in enumerate(zip(rows, mols)):
        ambiguity = unresolved_stereo(mol)
        if ambiguity:
            deferred.append({'candidate_id': row['candidate_id'],
                'reason': 'UNRESOLVED_STEREOCHEMISTRY', 'centers': ambiguity,
                'disposition': 'requires_explicit_stereochemical_design_before_vs'})
        elif len(selected) < limit:
            mol.SetProp('_Name', row['candidate_id'])
            mol.SetProp('COMPOUND_ID', row['candidate_id'])
            mol.SetIntProp('INPUT_SDF_INDEX', index)
            writer.write(mol)
            selected.append(row['candidate_id'])
        else:
            deferred.append({'candidate_id': row['candidate_id'], 'reason': 'PILOT_CANDIDATE_LIMIT'})
    writer.flush()
    sdf = stream.getvalue().encode()
    writer.close()
    if not selected:
        raise ValueError('No VS candidates with explicit stereochemistry: ' + json.dumps(deferred))
    drafts = target_drafts(design / 'case', uniprot, construct_review=state.get('construct_review'),
                           conformer_selection=state.get('conformer_selection'))
    target = json.loads(drafts['target-source.json'])
    if state['target']['site'] != 'C' + str(target['mapping']['uniprot_residue']):
        raise ValueError('VS site differs from the current study')
    files = {key: drafts[key] for key in ('receptor-projection.pdb', 'receptor-inventory.json', 'target-source.json')}
    files['ligands.sdf'] = sdf
    from .covdock_vs_geometry import Policy
    files['geometry-policy.json'] = encoded(Policy().model_dump(mode='json'))
    payload = {'schema': 'ligand-vs-study-input-v1', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'evidence_id': state['evidence_id'], 'target': state['target'],
        'selected_candidate_ids': selected, 'deferred_candidate_ids': [row['candidate_id'] for row in deferred],
        'candidate_deferrals': deferred,
        'selection': 'up to three stereochemically explicit candidates in deterministic enumeration order; not a potency ranking; unresolved stereochemistry deferred without selecting an isomer',
        'attachment_residue': target['projected_chain'] + ':' + str(target['projected_site']),
        'source_site_mapping': target['mapping'], 'reaction_type': 'Michael Addition',
        'lineage_sha256': lineage,
        'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        'review_requirements': {'environment_review_required': True,
            'missing_construct_residue_count': target['missing_construct_residue_count'],
            'excluded_atom_count': target['excluded_atom_count'],
            'protected_interactions_defined': bool(campaign.protection.protected_interactions),
            'questions': ['Assess the selected chain and biological assembly',
                'Assess all excluded chains, ligands, waters, ions and cofactors',
                'Assess unresolved construct regions and the mapped target site',
                'Assess Michael Addition scope and protected interactions'],
            'unresolved_residues_rebuilt': False},
        'receptor_prepared': False, 'environment_suitability_verified': False,
        'compute_submitted': False, 'scientific_acceptance': False, 'md_ready': False}
    if decision:
        payload.update(routing_decision=decision, generated_candidate_count=len(rows),
            selection='all chemically admitted stereochemically explicit candidates, in original input order; VS preselection for independent LO, not scientific acceptance')
    if use_prepared and (state.get('vs_receptor') or {}).get('use_for_vs'):
        from .ligand_vs_receptor import augment
        augment(root, state, payload, files)
        payload['files'] = {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()}
    if any(sha256_file(bound_file(root, name)) != sha for name, sha in lineage.items()):
        raise ValueError('VS study source changed during input binding')
    return payload, files


def reference(root, location, payload):
    checksum = sha256_file(location / 'input.json')
    return {'artifact_id': 'ligand-vs-input-' + checksum[:32], 'manifest_sha256': checksum,
        'directory': str(location.relative_to(root)), 'candidate_count': len(payload['selected_candidate_ids']),
        'attachment_residue': payload['attachment_residue'], 'environment_review_required': True,
        'compute_submitted': False, 'scientific_acceptance': False, 'md_ready': False}


def freeze(root, state):
    payload, files = build(root, state)
    raw = encoded(payload)
    checksum = hashlib.sha256(raw).hexdigest()
    location = root / 'state/ligand-vs-inputs' / checksum[:32]
    if any(path.is_symlink() for path in (root / 'state', location.parent, location)):
        raise ValueError('VS study input cannot traverse symlinks')
    location.parent.mkdir(parents=True, exist_ok=True)
    if not location.exists():
        staging = location.with_name('.' + checksum[:32] + '-' + uuid4().hex[:12])
        staging.mkdir()
        for name, contents in {**files, 'input.json': raw}.items():
            (staging / name).write_bytes(contents)
        staging.rename(location)
    ref = reference(root, location, payload)
    load(root, {**state, 'vs_input': ref})
    return ref


def load(root, state, *, use_prepared=True):
    ref = state['vs_input']
    payload, files = build(root, state, use_prepared=use_prepared)
    checksum = hashlib.sha256(encoded(payload)).hexdigest()
    expected = 'state/ligand-vs-inputs/' + checksum[:32]
    if ref['directory'] != expected or ref['manifest_sha256'] != checksum:
        raise ValueError('VS input differs from current study evidence. Read status and use '
                         'use_prepared_receptor_for_vs only if advertised to refresh an unsubmitted '
                         'prepared-receptor input; this preserves history and requires a new environment '
                         'review and approval. Repeating read_vs_input cannot repair the binding.')
    location = root / expected
    if {path.name for path in location.iterdir()} != {*files, 'input.json'}:
        raise ValueError('VS input inventory differs from its sealed study')
    for name, raw in {**files, 'input.json': encoded(payload)}.items():
        if bound_file(root, expected + '/' + name).read_bytes() != raw:
            raise ValueError('VS input bytes changed: ' + name)
    if ref != reference(root, location, payload):
        raise ValueError('VS input reference changed')
    return payload


def environment_inventory(state, payload):
    directory = state['vs_input']['directory']
    return {**payload['lineage_sha256'], directory + '/input.json': state['vs_input']['manifest_sha256'],
            **{directory + '/' + name: sha for name, sha in payload['files'].items()}}


def environment_review(root, state, payload):
    from .receptor_environment_review import load as load_review
    ref = state.get('vs_environment_review')
    if ref:
        return load_review(root, ref, state['vs_input'], environment_inventory(state, payload), scope='covdock-vs')
    return None


def retire_plan(state, reason):
    history = state.get('vs_plan_history')
    if history is not None and not isinstance(history, list):
        raise ValueError('VS plan history must be a list or null')
    previous = {name: state.pop(name, None) for name in ('vs_plan', 'vs_runtime', 'vs_environment_review')}
    if any(previous.values()):
        state['vs_plan_history'] = [*(history or []), {**previous, 'reason': reason}]


def revalidate_saved(root, state):
    if not state.get('vs_input') or state.get('pending'):
        return None
    try:
        payload = load(root, state)
    except (ValueError, KeyError, TypeError, OSError) as error:
        retire_plan(state, 'Current VS source could not be revalidated')
        state['vs_input_error'] = str(error)
        return str(error)
    try:
        environment_review(root, state, payload)
    except (ValueError, KeyError, TypeError, OSError) as error:
        retire_plan(state, 'Current VS environment review could not be revalidated')
        state['vs_review_error'] = str(error)
        return str(error)
    state.pop('vs_input_error', None)
    state.pop('vs_review_error', None)
    return None


def request_for(root, state, configuration, *, source_revision=None):
    payload = load(root, state)
    review = environment_review(root, state, payload)
    directory = state['vs_input']['directory']
    manifests = environment_inventory(state, payload)
    ref = state.get('vs_environment_review')
    if review:
        manifests[ref['path']] = ref['manifest_sha256']
    count = len(payload['selected_candidate_ids'])
    packet = freeze_preparation(root, {
        'source': {'route': 'ligand-first', 'workflow_id': state['workflow_id'],
                   'revision': state['revision'] if source_revision is None else source_revision,
                   'manifests': manifests},
        'original_ligands': directory + '/ligands.sdf',
        'receptor': directory + '/' + payload.get('receptor_file', 'receptor-projection.pdb'),
        'attachment_residue': payload['attachment_residue'],
        'execution_instance': configuration.get('execution_instance'),
        'budget': {'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16,
                   'max_runtime_minutes': 60, 'max_candidates': count},
        'njobs': min(count, 4) if payload.get('routing_decision') else count}, configuration)
    return JobSubmissionRequest(request_id=state['workflow_id'] + '-vs-' + state['vs_input']['manifest_sha256'][:12]
        + ('-' + ref['manifest_sha256'][:12] if ref else '')
        + ('-' + packet['plan']['execution_instance'] if 'execution_instance' in packet['plan'] else ''),
        project_id=state['workflow_id'], route='ligand-first', engine='schrodinger-covdock-vs',
        parameters={'vs_preparation': packet}, resource_budget=packet['plan']['budget'], execution_mode='plan')


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load_state(location)
        revision(state, expected_revision)
        from .receptor_environment_review import EVIDENCE_READ_SCHEMA, read_evidence
        permitted = ({'conformer_selection'} if action == 'select_receptor_conformers' else
                     {'construct_review'} if action == 'review_construct' else
                     {'environment_review'} if action == 'review_vs_environment' else
                     set(EVIDENCE_READ_SCHEMA) if action == 'read_vs_input' else set())
        accessible = construct_available(state) if action in CONSTRUCT_ACTIONS else available(state)
        if action not in ACTIONS or set(arguments) - permitted or not accessible:
            raise ValueError('VS planning requires an idle current Ligand design with no uncertain job')
        if action == 'read_construct_evidence':
            return response(state, {'construct_evidence': construct_evidence(root, state),
                                    'compute_submitted': False}, compact=True)
        if action == 'select_receptor_conformers':
            from .receptor_conformers import validate_selection
            if construct_frozen(state):
                raise ValueError('Conformers are frozen in downstream inputs; reconsider before changing them')
            selected = validate_selection(construct_evidence(root, state)['receptor_conformers'],
                                          arguments.get('conformer_selection'))
            if selected != state.get('conformer_selection'):
                state['conformer_selection'] = selected
                changed(location, state, action)
            return response(state, {'conformer_selection': selected, 'compute_submitted': False,
                                    'execution_authorized': False}, compact=True)
        if action == 'review_construct':
            from .construct_audit import validate_construct_review
            if construct_frozen(state):
                raise ValueError('Construct scope is frozen in downstream inputs; reconsider before changing it')
            reviewed = validate_construct_review(construct_evidence(root, state), arguments.get('construct_review'))
            if reviewed != state.get('construct_review'):
                state['construct_review'] = reviewed
                changed(location, state, action)
            return response(state, {'construct_review': reviewed, 'compute_submitted': False,
                                    'execution_authorized': False}, compact=True)
        if action == 'prepare_vs_input':
            ref = freeze(root, state)
            if state.get('vs_input') not in (None, ref):
                raise ValueError('Frozen VS input changed; reconsider the study before replacing it')
            state['vs_input'] = ref
        payload = load(root, state)
        if action == 'review_vs_environment':
            from .receptor_environment_review import freeze as freeze_review
            ref = freeze_review(root, state['vs_input'], environment_inventory(state, payload),
                                arguments.get('environment_review'), scope='covdock-vs')
            if ref != state.get('vs_environment_review'):
                retire_plan(state, 'Superseded by a new source-bound VS environment assessment')
            state['vs_environment_review'] = ref
            state.pop('vs_review_error', None)
        reviewed = environment_review(root, state, payload)
        if action == 'check_vs_runtime':
            manager = JobManager(root)
            previous = state.get('vs_plan') or {}
            try:
                request = request_for(root, state, manager.config.get('schrodinger', {}),
                                      source_revision=previous.get('source_revision'))
                spec = normalize_spec(manager._prepare_submission(request))
            except CovDockRuntimeUnavailable:
                state['vs_runtime'] = {'state': 'WAITING_RUNTIME_CONFIGURATION',
                    'reason': 'Configured vendor installation or license file is unavailable',
                    'compute_submitted': False}
            else:
                raw = encoded(spec)
                checksum = hashlib.sha256(raw).hexdigest()
                private = location / 'vs-plans'
                if private.is_symlink():
                    raise ValueError('VS private plans cannot traverse symlinks')
                private.mkdir(exist_ok=True, mode=0o700)
                path = private / (checksum + '.json')
                if not path.exists():
                    with path.open('xb') as stream:
                        path.chmod(0o600)
                        stream.write(raw)
                if path.is_symlink() or path.read_bytes() != raw:
                    raise ValueError('VS private plan changed')
                ref = {'path': str(path.relative_to(root)), 'sha256': checksum,
                    'source_revision': request.parameters['vs_preparation']['plan']['source']['revision']}
                if previous and previous != ref:
                    raise ValueError('Frozen VS runtime plan changed; release or reconsider before replacement')
                state['vs_plan'] = ref
                eligible = bool(reviewed and reviewed['decision'] == 'suitable_for_exploratory_vs')
                state['vs_runtime'] = {'state': ('VS_PLAN_COMPILED' if eligible
                                               else 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'),
                    'plan_sha256': checksum, 'resource_budget': request.resource_budget.model_dump(mode='json'),
                    'mode': 'enrichment', 'worker_backend': request.parameters['vs_preparation']['worker_backend'],
                    'compute_submitted': False,
                    'environment_eligible_for_exploratory_vs': eligible,
                    'license_checkout_verified': False, 'native_execution_available': False}
                instance = request.parameters['vs_preparation']['plan'].get('execution_instance')
                if instance is not None:
                    state['vs_runtime']['execution_instance'] = instance
                if instance is not None and os.environ.get('COVALENT_LIGAND_VS_INSTANCE') != instance:
                    state['vs_runtime']['execution_blocker'] = 'INSTANCE_EXECUTOR_NOT_ENABLED'
                    state['vs_runtime']['execution_blocker_message'] = (
                        f'The VS plan targets {instance}, but its instance executor is disabled. '
                        'The memory value is an estimate; the instance has no per-task memory cap. '
                        'Execution and cancellation must be connected before dispatch. No compute was submitted.'
                    )
        evidence = read_evidence(root, environment_inventory(state, payload), **arguments) if action == 'read_vs_input' else None
        if action != 'read_vs_input':
            changed(location, state, action)
        return response(state, {'vs_input': state['vs_input'], 'review_requirements': payload['review_requirements'],
            'environment_review': reviewed, 'evidence_inventory': environment_inventory(state, payload),
            'evidence': evidence,
            'source_site_mapping': payload['source_site_mapping'], 'vs_runtime': state.get('vs_runtime'),
            'compute_submitted': False, 'scientific_acceptance': False, 'md_ready': False}, compact=True)
