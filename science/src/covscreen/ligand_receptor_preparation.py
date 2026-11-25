"""Bind the shared CPU preparation compiler to a current Ligand study.

Input packaging and planning never launch a task. Native execution uses the
separate lifecycle adapter; prepared-receptor acceptance remains independent.
"""
from .approval_scope import normalize_spec
from .execution import JobManager
from .jobs import JobSubmissionRequest
from .ligand_receptor_source import context, pre_vs, planning_available, verify_inputs
from .ligand_parent_evidence import version_lock
from .ligand_receptor_input import freeze, validate_binding
from .receptor_input import ReceptorRuntimeUnavailable
from .workflow_protocol import changed, revision
from .workflow_store import load


ACTIONS = {'prepare_receptor_input', 'check_receptor_runtime'}
STAGES = {'DIRECT_LO_READY', 'POSE_EXECUTION_REVIEW', 'VS_REFINEMENT_READY', 'AF3_RUNTIME_REQUIRED', 'AF3_PLAN_READY', 'AF3_EXECUTION_REVIEW'}


def request_for(root, state):
    loaded = validate_binding(root, state)
    binding = context(state)['receptor_input']
    count = loaded['acquisition']['missing_construct_residue_count']
    if type(count) is not int or not 0 <= count <= 100:
        raise ValueError('Observed missing residues exceed the registered preparation bound')
    policy_parameters = {}
    request_suffix = ''
    selection = context(state).get('receptor_relaxation_selection')
    if selection is not None:
        import hashlib
        import json
        from .receptor_relaxation import RELAXATION_POLICY_V2
        version = selection.get('version')
        policy = RELAXATION_POLICY_V2
        if version in {'v2-local', 'v2-local-bounded', 'v2-local-block'}:
            from .ligand_receptor_local import derive_policy, derive_bounded_policy, derive_block_policy
            history = [r for r in context(state).get('receptor_preparation_history', [])
                       if r.get('run_id') == selection.get('previous_run_id')]
            if len(history) != 1:
                raise ValueError('Local repair plan requires its unique retained predecessor')
            derive = {'v2-local':derive_policy, 'v2-local-bounded':derive_bounded_policy,
                      'v2-local-block':derive_block_policy}[version]
            policy, evidence = derive(root, state, history[0])
            if policy != selection.get('policy') or evidence != selection.get('evidence'):
                raise ValueError('Local repair selection differs from registered source evidence')
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        if (version not in {'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'} or selection.get('policy_sha256') != digest
                or selection.get('input_manifest_sha256') != binding['manifest_sha256']):
            raise ValueError('Receptor relaxation selection differs from the current input or policy')
        policy_parameters = {'relaxation_policy_version': version,
            'relaxation_policy': policy, 'relaxation_policy_sha256': digest}
        request_suffix = '-' + version + '-' + digest[:12]
    return JobSubmissionRequest(request_id=state['workflow_id'] + '-receptor-' + binding['manifest_sha256'][:12] + request_suffix,
        project_id=state['design_id'], route='ligand-first', engine='library-first-receptor-preparation',
        execution_mode='plan', parameters={'workflow_id': state['workflow_id'],
            'receptor_input_artifact_id': binding['artifact_id'], 'manifest_sha256': binding['manifest_sha256'],
            'altlocs': loaded['acquisition']['altlocs'], 'heavy_seed': 20260725,
            'max_missing_residues': count, **policy_parameters},
        resource_budget={'max_runtime_minutes': 30 if policy_parameters.get('relaxation_policy_version') in {'v2-local', 'v2-local-bounded', 'v2-local-block'} else 15, 'gpu_count': 0, 'cpu_count': 1,
                         'memory_gb': 4, 'max_candidates': 1})


def revalidate_saved(root, state):
    handoff = context(state)
    if not handoff.get('receptor_input'):
        return None
    try:
        validate_binding(root, state)
    except (ValueError, KeyError, TypeError, OSError) as error:
        record = handoff.get('receptor_preparation') or {}
        if record.get('run_id') or (state.get('pending') or {}).get('action') == 'start_receptor_preparation':
            record.update(evidence_error=str(error), receptor_prepared=False, output_validation='EVIDENCE_REJECTED')
            return str(error)
        previous = handoff.pop('receptor_input')
        handoff.pop('receptor_preparation', None)
        handoff['receptor_input_error'] = {'artifact_id': previous['artifact_id'], 'reason': str(error)}
        return str(error)
    return None


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if arguments or state.get('pending') or not planning_available(state):
            raise ValueError('Receptor preparation requires an idle current-study receptor source')
        try:
            verify_inputs(root, state)
            if pre_vs(state) and not state.get('vs_receptor'):
                state['vs_receptor'] = {'source_input': state['vs_input'], 'resume_stage': state['stage']}
            handoff = context(state)
            if action == 'prepare_receptor_input':
                binding = freeze(root, state)
                if handoff.get('receptor_input') not in (None, binding):
                    raise ValueError('Frozen receptor input changed')
                handoff['receptor_input'] = binding
                handoff.pop('receptor_input_error', None)
                output = {'receptor_input': binding, 'compute_submitted': False}
            elif action == 'check_receptor_runtime':
                request = request_for(root, state)
                previous = handoff.get('receptor_preparation') or {}
                try:
                    spec = normalize_spec(JobManager(root)._prepare_submission(request))
                except ReceptorRuntimeUnavailable as error:
                    handoff['receptor_preparation'] = {**previous, 'request_id': request.request_id,
                        'runtime_check': 'WAITING_RUNTIME_CONFIGURATION', 'runtime_error': str(error),
                        'compute_submitted': False, 'receptor_prepared': False}
                else:
                    if previous.get('spec') is not None and previous['spec'] != spec:
                        raise ValueError('Frozen receptor plan changed; reconsider before replacing its inputs')
                    handoff['receptor_preparation'] = {'request_id': request.request_id, 'spec': spec,
                        'runtime_check': 'CPU_PREPARATION_PLAN_COMPILED',
                        'compute_submitted': False, 'receptor_prepared': False,
                        'approval': 'NOT_REQUESTED', 'execution_connected': True,
                        'preparation_scope': {
                            'missing_residue_policy': 'model_deposited_construct_gaps_with_pdbfixer',
                            'max_missing_residues': spec['parameters']['max_missing_residues'],
                            'missing_heavy_atom_policy': 'model_missing_heavy_atoms',
                            'subsequent_checks': ['chirality_plan', 'relaxation', 'identity_quality', 'geometry_quality'],
                            'modeled_coordinates_are_experimental': False,
                            'input_status_is_not_execution_policy':
                                'unresolved_residues_rebuilt=false describes the input before execution; '
                                'this plan attempts missing-residue and heavy-atom reconstruction. '
                                'Do not describe this approval as no rebuilding.',
                            'completion_is_not_quality_acceptance': True},
                        'limitations': ['Import probes are not a successful receptor repair.',
                            'Execution requires separate native approval; quality/context review and CovDock remain pending.']}
                output = {'receptor_preparation': handoff['receptor_preparation'], 'compute_submitted': False}
            else:
                raise ValueError('Unknown receptor preparation action')
        except (ValueError, KeyError, TypeError, OSError):
            diagnosis = revalidate_saved(root, state)
            if diagnosis:
                changed(location, state, 'receptor_input_rejected', {'reason': diagnosis})
            raise
        changed(location, state, action)
        return response(state, output, compact=pre_vs(state))


def select_restrained_after_failure(root, *, workflow_id, expected_revision, reason, local=False, bounded=False, block=False, **arguments):
    """Archive a verified failed receptor task; selection alone never submits."""
    from .ligand_receptor_source import policy_replanning_available
    import hashlib
    import json
    from copy import deepcopy
    from .ligand_workflow import directory, response
    from .ligand_receptor_execution import observe
    from .receptor_relaxation import RELAXATION_POLICY_V2
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if (arguments or state.get('pending') or not policy_replanning_available(state)
                or state['stage'] != 'RECEPTOR_PREPARATION_REVIEW'
                or not isinstance(reason, str) or not 5 <= len(reason.strip()) <= 2000):
            raise ValueError('Restrained replanning requires an idle failed pre-VS or direct-LO receptor study and a concrete reason')
        handoff = context(state)
        if handoff.get('receptor_relaxation_selection') and not (bounded or block):
            raise ValueError('A restrained policy was already selected; observe its existing plan')
        record = handoff.get('receptor_preparation') or {}
        if not record.get('run_id'):
            raise ValueError('A registered failed receptor task is required')
        observed = observe(root, state, record['run_id'])
        if observed['job_state'] != 'FAILED':
            raise ValueError('Only a verified failed task can be replaced by this policy selection')
        policy, evidence = RELAXATION_POLICY_V2, None
        version = 'v2'
        if block:
            if not local or bounded or (handoff.get('receptor_relaxation_selection') or {}).get('version') != 'v2-local-bounded':
                raise ValueError('Block recovery requires the prior bounded selection')
            from .ligand_receptor_local import derive_block_policy
            policy, evidence = derive_block_policy(root, state, record)
            version = 'v2-local-block'
        elif bounded:
            if not local or (handoff.get('receptor_relaxation_selection') or {}).get('version') != 'v2-local':
                raise ValueError('Bounded recovery requires the prior harmonic-local selection')
            from .ligand_receptor_local import derive_bounded_policy
            policy, evidence = derive_bounded_policy(root, state, record)
            version = 'v2-local-bounded'
        elif local:
            from .ligand_receptor_local import derive_policy
            policy, evidence = derive_policy(root, state, record)
            version = 'v2-local'
        digest = hashlib.sha256(json.dumps(policy, sort_keys=True,
            separators=(',', ':'), allow_nan=False).encode()).hexdigest()
        selection = {'version': version, 'policy_sha256': digest,
            'input_manifest_sha256': handoff['receptor_input']['manifest_sha256'],
            'reason': reason.strip(), 'previous_run_id': record['run_id']}
        if local:
            selection.update(policy=policy, evidence=evidence)
        handoff.setdefault('receptor_preparation_history', []).append(deepcopy(record))
        handoff.pop('receptor_preparation')
        if bounded or block:
            handoff.setdefault('receptor_relaxation_selection_history', []).append(
                deepcopy(handoff['receptor_relaxation_selection']))
        handoff['receptor_relaxation_selection'] = selection
        from .ligand_direct_lo import SOURCE as DIRECT_SOURCE, STAGE as DIRECT_STAGE
        state['stage'] = DIRECT_STAGE if handoff.get('source_kind') == DIRECT_SOURCE else handoff['resume_stage']
        if not planning_available(state):
            raise ValueError('Original study stage cannot resume receptor planning')
        request_for(root, state)
        action = ('select_block_local_receptor_policy' if block else 'select_bounded_local_receptor_policy'
                  if bounded else 'select_local_receptor_policy' if local else 'select_restrained_receptor_policy')
        changed(location, state, action, selection)
        return response(state, {'receptor_relaxation_selection': selection, 'compute_submitted': False}, compact=True)
