"""Bind a study's measured receptor and exact candidates to a CovDock draft.

This adapter prepares inputs and compiles a plan; environment suitability and
native execution are separate decisions, never inferred from file readiness.
"""
import hashlib
import json
from pathlib import Path

from .approval_scope import normalize_spec
from .covdock_gate import STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION
from .covdock_receptor import CovDockRuntimeUnavailable, inspect_receptor
from .execution import JobManager
from .jobs import ArtifactRegistry, JobSubmissionRequest, sha256_file
from .ligand_parent_evidence import version_lock
from .ligand_receptor_execution import observe, review, reject_evidence
from .ligand_receptor_input import validate_binding
from .ligand_refinement_input import load_refinement_input
from .receptor_evidence import regular
from .refinement_input import read_candidate_bytes
from .workflow_protocol import changed, revision
from .workflow_store import load as load_state


ACTIONS = {'prepare_covdock_input', 'check_covdock_runtime', 'review_covdock_environment',
           'read_covdock_environment'}


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + '\n').encode()


def build(root, state):
    # Accept either supported identifier without rewriting the sealed request.
    # validate_binding and the reviewed receptor identity below still verify
    # the accession, species, sequence, site and original source lineage.
    if state['target'] not in (
            {'target': 'EGFR', 'site': 'C797', 'taxon': 9606},
            {'target': 'P00533', 'site': 'C797', 'taxon': 9606}):
        raise ValueError('This bounded CovDock workflow requires explicit EGFR C797 / human identity')
    handoff = state['refinement_input']
    prepared = handoff['receptor_preparation']
    if prepared.get('evaluation_verification') != 'verified':
        raise ValueError('CovDock input requires a verified receptor review')
    stage = state['stage']
    try:
        output = observe(root, state, prepared['run_id'])
        review(root, state, output, reuse=True)
    except (ValueError, KeyError, TypeError, OSError) as error:
        reject_evidence(state, error)
        raise
    finally:
        state['stage'] = stage
    evaluation = prepared['evaluation']
    loaded = validate_binding(root, state)
    candidates_source = load_refinement_input(root, state)
    receptor = evaluation['result']['receptor']
    receptor_path, _ = regular(root, root / receptor['path'], receptor['sha256'])
    identity = evaluation['result']['identity']
    if identity['uniprot'] != 'P00533' or identity['canonical_cys'] != 797:
        raise ValueError('Prepared receptor does not match the supported canonical target')
    attachment = identity['author_chain'] + ':' + str(identity['author_residue'])
    check = inspect_receptor(receptor_path, attachment, expected_sha256=receptor['sha256'])
    table, _ = regular(root, root / handoff['candidate_table'], handoff['input_sha256'])
    candidates = read_candidate_bytes(table, table.read_bytes())
    direct = handoff.get('source_kind') == 'sealed-ligand-direct-lo-v1'
    from .ligand_docking_policy import POLICY as ROUTING_POLICY
    limit = ROUTING_POLICY['direct_lo_max_candidates'] if direct else 3
    if (not 1 <= len(candidates) <= limit or set(candidates['warhead_class']) != {'acrylamide'}
            or set(candidates['covdock_reaction_type']) != {'Michael Addition'}):
        raise ValueError(f'The bounded CovDock branch supports one to {limit} acrylamide candidates')
    pipeline = JobManager(root).registry.job_dir(prepared['run_id']) / 'artifacts/receptor-preparation'
    environment = pipeline / 'prepared/environment-decision.json'
    regular(root, environment, evaluation['result']['inventory'][str(environment.relative_to(root))])
    context = evaluation['result']['context']
    source = loaded['acquisition']
    requirements = {
        'status': 'REVIEW_REQUIRED', 'construct_audit': source['construct_audit'],
        'missing_construct_residue_count': handoff['receptor_input']['missing_construct_residue_count'],
        'excluded_environment': json.loads(environment.read_text()),
        'site_context': context,
        'questions': ['Does the isolated selected chain represent the intended biological context?',
            'Are removed ligands, waters, ions, cofactors or chains relevant to this binding site?',
            'Do modeled or unresolved regions affect the pocket or target cysteine?',
            'Is this target/variant/reaction combination appropriate for the intended study?'],
        'biological_suitability_verified': False, 'reactivity_validated': False}
    return {'schema': 'ligand-covdock-input-v1', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'evidence_id': state['evidence_id'],
        'target': state['target'], 'candidate_artifact_id': handoff['artifact_id'],
        'target_artifact_id': handoff['target']['artifact_id'],
        'receptor_review': prepared['evaluation_artifact'], 'preparation_run_id': prepared['run_id'],
        'candidate_table': handoff['candidate_table'],
        'input_count': handoff['input_count'], 'input_sha256': handoff['input_sha256'],
        'identity_sha256': handoff['identity_sha256'], 'receptor': receptor,
        'attachment_residue': attachment, 'receptor_check': check,
        'reaction_type': 'Michael Addition', 'input_score_source': 'core',
        'gate_algorithm_version': LIGAND_ALGORITHM_VERSION,
        'review_requirements': requirements, 'environment_review_completed': False,
        'lineage_sha256': {**evaluation['result']['inventory'], **candidates_source['source_manifests']},
        'implementation_sha256': sha256_file(Path(__file__)),
        'planning_only': True, 'compute_submitted': False, 'execution_authorized': False,
        'vendor_preparation_required': True, 'vendor_license_availability_verified': False,
        'md_ready': False}


def present(root, path, payload):
    checksum = sha256_file(path)
    return {'artifact_id': 'ligand-covdock-input-' + checksum[:32], 'manifest_sha256': checksum,
        'path': str(path.relative_to(root)), 'candidate_count': payload['input_count'],
        'attachment_residue': payload['attachment_residue'], 'reaction_type': payload['reaction_type'],
        'environment_review_completed': False, 'planning_only': True,
        'compute_submitted': False, 'execution_authorized': False}


def freeze(root, state):
    payload = build(root, state)
    raw = encoded(payload)
    checksum = hashlib.sha256(raw).hexdigest()
    directory = root / 'state/ligand-covdock-inputs' / checksum[:32]
    if any(path.is_symlink() for path in (root / 'state', directory.parent, directory)):
        raise ValueError('CovDock input cannot traverse symbolic links')
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / 'input.json'
    if not path.exists():
        # Preserve canonical key order used for this content identity.
        ArtifactRegistry._write_json(path, json.loads(raw))
    regular(root, path, checksum)
    return present(root, path, payload)


def load(root, state):
    ref = state['refinement_input'].get('covdock_input')
    if not ref:
        raise ValueError('Prepare the current study CovDock input first')
    checksum = ref['manifest_sha256']
    expected = 'state/ligand-covdock-inputs/' + checksum[:32] + '/input.json'
    if ref['path'] != expected:
        raise ValueError('CovDock input path differs from its identity')
    path, _ = regular(root, root / expected, checksum)
    payload = json.loads(path.read_text())
    expected_payload = build(root, state)
    # Replay the known pre-v4 input constructor without changing signed inputs.
    # All scientific fields still undergo the current independent reconstruction.
    if (payload.get('gate_algorithm_version') == STRICT_ALGORITHM_VERSION
            and payload.get('implementation_sha256') == '1bc88b45ff30663c634e3463cae903d6ee27ab62fef9c8c5767b4cc7d01011da'):
        expected_payload['gate_algorithm_version'] = STRICT_ALGORITHM_VERSION
        expected_payload['implementation_sha256'] = payload['implementation_sha256']
    if payload != expected_payload or ref != present(root, path, payload):
        raise ValueError('CovDock input differs from current study evidence')
    return payload


def environment_inventory(state, payload):
    ref = state['refinement_input']['covdock_input']
    witness = payload['receptor_review']
    return {**payload['lineage_sha256'], witness['path']: witness['manifest_sha256'],
            ref['path']: ref['manifest_sha256']}


def environment_review(root, state, payload):
    from .receptor_environment_review import load as load_review
    handoff = state['refinement_input']
    ref = handoff.get('environment_review')
    if not ref:
        return None
    return load_review(root, ref, handoff['covdock_input'], environment_inventory(state, payload))


def retire_environment(handoff, reason):
    review = handoff.pop('environment_review', None)
    plan = handoff.pop('covdock_plan', None)
    if review or plan:
        handoff.setdefault('environment_review_history', []).append({
            'review': review, 'plan': plan, 'reason': reason})


def request_for(root, state):
    from .ligand_instance import selected_instance
    payload = load(root, state)
    direct = state['refinement_input'].get('source_kind') == 'sealed-ligand-direct-lo-v1'
    routed_vs = (state['refinement_input'].get('source_kind') == 'registered-ligand-vs-review-v1'
                 and (state.get('docking_decision') or {}).get('selected_route') == 'vs_then_lo')
    instance = direct or routed_vs
    workers = min(payload['input_count'], 4) if instance else 1
    reviewed = environment_review(root, state, payload)
    ref = state['refinement_input'].get('environment_review')
    lineage = sorted(set(environment_inventory(state, payload)) | ({ref['path']} if ref else set()))
    accepted = bool(reviewed and reviewed['decision'] == 'suitable_for_exploratory_refinement')
    return JobSubmissionRequest(request_id=state['workflow_id'] + '-covdock-' +
        state['refinement_input']['covdock_input']['manifest_sha256'][:12] +
        ('-' + ref['manifest_sha256'][:12] if ref else ''),
        project_id=state['design_id'], route='ligand-first', engine='schrodinger-covdock-lo', execution_mode='plan',
        parameters={'workflow_id': state['workflow_id'], 'candidate_table': payload['candidate_table'],
            **({'dispatch_policy': 'ligand-lo-instance-v1', 'execution_instance': selected_instance()} if instance else {}),
            'expected_input_count': payload['input_count'], 'expected_input_sha256': payload['input_sha256'],
            'expected_identity_sha256': payload['identity_sha256'], 'receptor': payload['receptor']['path'],
            'attachment_residue': payload['attachment_residue'], 'nucleophilic_atom': 'SG',
            'reaction_type': payload['reaction_type'], 'input_score_source': payload['input_score_source'],
            'gate_algorithm_version': payload['gate_algorithm_version'], 'refinement_lineage_files': lineage,
            'prepare_receptor': True, 'final_n': payload['input_count'], 'njobs': workers, 'host': f'localhost:{workers}',
            'resume': False, 'environment_review_completed': reviewed is not None,
            'environment_eligible_for_exploratory_refinement': accepted,
            'environment_review': ref, 'environment_assessment': reviewed['decision'] if reviewed else 'not_reviewed'},
        resource_budget={'gpu_count': 0, 'cpu_count': workers, 'memory_gb': 8,
                         'max_runtime_minutes': 240 if instance else 30, 'max_candidates': payload['input_count']})


def revalidate_saved(root, state):
    handoff = state.get('refinement_input') or {}
    if not handoff.get('covdock_input'):
        return None
    try:
        payload = load(root, state)
    except (ValueError, KeyError, TypeError, OSError) as error:
        prior = handoff.pop('covdock_input')
        retire_environment(handoff, 'CovDock input evidence invalidated: ' + str(error))
        handoff['covdock_input_error'] = {'artifact_id': prior['artifact_id'], 'reason': str(error)}
        return str(error)
    try:
        environment_review(root, state, payload)
    except (ValueError, KeyError, TypeError, OSError) as error:
        retire_environment(handoff, 'Environment review invalidated: ' + str(error))
        handoff['environment_review_error'] = str(error)
        return str(error)
    return None


def verify_saved_readonly(root, state):
    """Withdraw corrupt downstream drafts in the read projection only.

    This is a cheap byte-identity check, not a replay of receptor preparation.
    Start/check actions still rebuild the full source-bound plan independently.
    """
    handoff = state.get('refinement_input') or {}
    for key in ('covdock_input', 'environment_review'):
        ref = handoff.get(key)
        if not ref:
            continue
        try:
            regular(root, root / ref['path'], ref['manifest_sha256'])
        except (ValueError, KeyError, TypeError, OSError) as error:
            if key == 'covdock_input':
                handoff.pop(key)
                handoff['covdock_input_error'] = {'artifact_id': ref.get('artifact_id'), 'reason': str(error)}
            else:
                handoff['environment_review_error'] = str(error)
            retire_environment(handoff, 'Read-only draft identity check failed: ' + str(error))
            return str(error)


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load_state(location)
        revision(state, expected_revision)
        from .receptor_environment_review import EVIDENCE_READ_SCHEMA, read_evidence
        permitted = ({'environment_review'} if action == 'review_covdock_environment' else
                     set(EVIDENCE_READ_SCHEMA) if action == 'read_covdock_environment' else set())
        if set(arguments) - permitted or state.get('pending') or state['stage'] != 'RECEPTOR_PREPARATION_REVIEW':
            raise ValueError('CovDock planning requires the current idle verified receptor review')
        handoff = state['refinement_input']
        try:
            if action == 'prepare_covdock_input':
                reference = freeze(root, state)
                if handoff.get('covdock_input') not in (None, reference):
                    raise ValueError('Existing CovDock input changed; reconsider before replacing it')
                handoff['covdock_input'] = reference
                handoff.pop('covdock_input_error', None)
                output = {'covdock_input': reference, 'review_requirements': load(root, state)['review_requirements']}
            elif action in {'review_covdock_environment', 'read_covdock_environment'}:
                from .receptor_environment_review import freeze as freeze_review
                payload = load(root, state)
                current = environment_review(root, state, payload)
                if action == 'review_covdock_environment':
                    reference = freeze_review(root, handoff['covdock_input'],
                        environment_inventory(state, payload), arguments.get('environment_review'))
                    if reference != handoff.get('environment_review'):
                        retire_environment(handoff, 'Superseded by a new source-bound environment assessment')
                    handoff['environment_review'] = reference
                    handoff.pop('environment_review_error', None)
                    current = environment_review(root, state, payload)
                output = {'environment_review': current, 'review_requirements': payload['review_requirements'],
                    'evidence_inventory': environment_inventory(state, payload),
                    'native_execution_available': True, 'native_approval_required': True}
                if action == 'read_covdock_environment':
                    output['evidence'] = read_evidence(root, environment_inventory(state, payload), **arguments)
            elif action == 'check_covdock_runtime':
                request = request_for(root, state)
                previous = handoff.get('covdock_plan') or {}
                try:
                    spec = normalize_spec(JobManager(root)._prepare_submission(request))
                except CovDockRuntimeUnavailable as error:
                    handoff['covdock_plan'] = {**previous, 'runtime_check': 'WAITING_RUNTIME_CONFIGURATION',
                        'runtime_error': str(error), 'compute_submitted': False, 'execution_authorized': False}
                else:
                    if previous.get('spec') is not None and previous['spec'] != spec:
                        raise ValueError('Frozen CovDock plan changed; reconsider before replacing it')
                    accepted = request.parameters['environment_eligible_for_exploratory_refinement']
                    handoff['covdock_plan'] = {'request_id': request.request_id, 'spec': spec,
                        'runtime_check': ('REGISTERED_COVDOCK_PLAN_COMPILED' if accepted
                                          else 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED'),
                        'runtime_assets_present': True, 'license_availability_verified': False,
                        'compute_submitted': False, 'execution_authorized': False,
                        'environment_review_completed': request.parameters['environment_review_completed'],
                        'environment_eligible_for_exploratory_refinement': accepted,
                        'environment_assessment': request.parameters['environment_assessment']}
                output = {'covdock_plan': handoff['covdock_plan']}
            else:
                raise ValueError('Unknown CovDock preparation action')
        except (ValueError, KeyError, TypeError, OSError) as error:
            diagnosis = revalidate_saved(root, state)
            changed(location, state, 'covdock_input_rejected', {'reason': diagnosis or str(error)})
            raise
        changed(location, state, action)
        return response(state, {**output, 'compute_submitted': False, 'execution_authorized': False})
