"""Workflow-owned candidate selection, independent of native graph/MD admission."""
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from .ligand_md_source import record as source_record, project_id, review_stage

from .jobs import sha256_file
from .md_candidate_pose import freeze_delivered_candidate_pose, load_candidate_pose_input
from .metric_contracts import md_not_evaluated
from .receptor_evidence import regular


def selection_status(state):
    """Current decision only; retained history is never an active selection."""
    record = source_record(state)
    selection = record.get('md_selection')
    history = record.get('md_selection_history') or []
    return {'state': ('selected' if record.get('md_selection_verification') == 'verified'
                      else 'selected_evidence_unverified') if selection else 'no_active_selection',
        'selected_identity': {key: selection[key] for key in ('compound_id', 'input_sdf_index', 'artifact_id')}
                            if selection else None,
        'archived_decision_count': len(history), 'publication_performed': False,
        'scope': 'current_workflow_candidate_choice_not_md_admission'}


def validate(root, state):
    record = source_record(state)
    selection = record['md_selection']
    if state.get('historical_md_source'):
        from .ligand_historical_md_source import validate_selection
        return validate_selection(root, state)
    delivery = record.get('md_handoff')
    if not delivery or record.get('md_handoff_error') or record.get('evaluation_verification') != 'verified':
        raise ValueError('Selected candidate requires the current verified MD handoff')
    if selection['delivery_sha256'] != delivery['manifest_sha256']:
        raise ValueError('Selected candidate delivery changed; do not silently reselect')
    loaded = load_candidate_pose_input(root, selection['artifact_id'], selection['manifest_sha256'])
    value = loaded['manifest']
    if (value['origin'] != {'route': 'ligand-first', 'task_id': record['run_id'],
                           'delivery_sha256': delivery['manifest_sha256']}
            or value['compound_id'] != selection['compound_id']
            or value['input_sdf_index'] != selection['input_sdf_index']
            or value['candidate_id'] != selection['candidate_id']):
        raise ValueError('Selected pose differs from its workflow decision')
    return loaded


def refresh(root, state):
    record = source_record(state)
    if not record.get('md_selection'):
        return
    try:
        validate(root, state)
    except (ValueError, KeyError, TypeError, OSError) as error:
        record['md_selection_verification'] = 'evidence_rejected'
        record['md_selection_error'] = str(error)
        if record.get('md_graph_plan'):
            record['md_graph_plan_error'] = 'Selected pose source could not be verified'
    else:
        record['md_selection_verification'] = 'verified'
        record.pop('md_selection_error', None)
        try:
            for name, digest in (record.get('md_graph_plan') or {}).get('spec', {}).get('input_sha256', {}).items():
                regular(root, root / name, digest)
        except (ValueError, OSError) as error:
            record['md_graph_plan_error'] = str(error)
        else:
            record.pop('md_graph_plan_error', None)


def select(root, state, *, compound_id, input_sdf_index, reason):
    if state.get('md_graph_execution') or state.get('pending'):
        raise ValueError('Observe or reconcile the original graph task before changing its selection')
    if (not isinstance(compound_id, str) or not compound_id.strip()
            or type(input_sdf_index) is not int or input_sdf_index < 0
            or not isinstance(reason, str) or not reason.strip() or len(reason) > 8000):
        raise ValueError('Select an exact compound/index with an explicit scientific reason')
    record = source_record(state)
    delivery = record['md_handoff']
    choice = {'compound_id': compound_id, 'input_sdf_index': input_sdf_index,
              'reason': reason.strip(), 'delivery_sha256': delivery['manifest_sha256']}
    previous = record.get('md_selection')
    if previous:
        if any(previous.get(key) != value for key, value in choice.items()):
            raise ValueError('Release the existing selection before changing the candidate or reason')
        validate(root, state)
        refresh(root, state)
        return previous
    digest = hashlib.sha256(json.dumps({'workflow_id': state['workflow_id'], **choice}, sort_keys=True).encode()).hexdigest()[:32]
    artifact_id = 'candidate-pose-input-' + digest
    parent = root / 'state/candidate_pose_input'
    destination = parent / digest
    for path in (root / 'state', parent, destination):
        if path.is_symlink():
            raise ValueError('Candidate selection cannot traverse symbolic links')
    source, _ = regular(root, root / delivery['path'], delivery['manifest_sha256'])
    directory = source.parent
    prepared, context = directory / 'prepared-receptor.json', directory / 'receptor-context.json'
    staging = parent / ('.' + digest + '-' + uuid4().hex[:12])
    if not destination.exists():
        try:
            freeze_delivered_candidate_pose(source, delivery['manifest_sha256'], compound_id,
                input_sdf_index, prepared, sha256_file(prepared), context, sha256_file(context), staging,
                expected_route='ligand-first', expected_task_id=record['run_id'])
            staging.rename(destination)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
    path = destination / 'candidate-pose-input.json'
    loaded = load_candidate_pose_input(root, artifact_id, sha256_file(path))
    selection = {**choice, 'artifact_id': artifact_id, 'manifest_sha256': sha256_file(path),
        'candidate_id': loaded['manifest']['candidate_id'],
        'decision_scope': 'candidate_pose_selection', 'native_graph_qc_completed': False,
        'md_admission': md_not_evaluated(), 'calculation_submitted': False}
    record['md_selection'] = selection
    try:
        validate(root, state)
    except BaseException:
        record.pop('md_selection', None)
        raise
    refresh(root, state)
    return selection


def release(state, reason):
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 8000:
        raise ValueError('Releasing a selection requires a reason')
    record = source_record(state)
    if state.get('md_graph_execution') or state.get('pending'):
        raise ValueError('Observe or reconcile the original graph task before changing its selection')
    if not record.get('md_selection'):
        raise ValueError('No current candidate selection to release')
    record.setdefault('md_selection_history', []).append({
        'selection': record.pop('md_selection'), 'reason': reason.strip(),
        'verification': record.pop('md_selection_verification', 'not_evaluated'),
        'evidence_error': record.pop('md_selection_error', None),
        'graph_plan': record.pop('md_graph_plan', None),
        'graph_plan_error': record.pop('md_graph_plan_error', None)})
    return {'selection_released': True, 'selection_status': 'deselected_and_archived',
            'active_selection': None, 'publication_performed': False,
            'graph_plan_status': 'no_active_plan', 'calculation_submitted': False}


def graph_request(root, state):
    from .jobs import JobSubmissionRequest
    validate(root, state)
    selection = source_record(state)['md_selection']
    # Retain the existing executor registration; provenance derives discovery
    # route from the frozen pose, never from this historical engine name.
    return JobSubmissionRequest(request_id=state['workflow_id'] + '-graph-' + selection['manifest_sha256'][:16],
        project_id=project_id(state), route='library-first', engine='library-first-md-adduct-admission',
        execution_mode='plan', parameters={'workflow_id': state['workflow_id'], 'candidate_pose_artifact_id': selection['artifact_id'],
            'manifest_sha256': selection['manifest_sha256'], 'native_timeout_seconds': 1200},
        resource_budget={'max_runtime_minutes': 30, 'gpu_count': 0, 'cpu_count': 2,
                         'memory_gb': 8, 'max_candidates': 1})


def plan_graph(root, state):
    from .approval_scope import normalize_spec
    from .execution import JobManager, NativeAdductRuntimeUnavailable
    if state.get('md_graph_execution') or state.get('pending'):
        raise ValueError('Observe or reconcile the original graph task; do not replan it')
    request = graph_request(root, state)
    record = source_record(state)
    old = record.get('md_graph_plan') or {}
    try:
        manager = JobManager(root)
        compiled = manager._prepare_submission(request)
        spec = normalize_spec(compiled)
    except NativeAdductRuntimeUnavailable as error:
        plan = {'state': 'runtime_configuration_required', 'reason': str(error),
                'native_graph_qc_completed': False, 'calculation_submitted': False}
        if old.get('spec'):
            plan['spec'] = old['spec']
    else:
        if old.get('spec') and old['spec'] != spec:
            raise ValueError('Frozen graph plan changed; release the unexecuted selection before replanning')
        plan = {'state': 'plan_compiled', 'spec': spec, 'native_graph_qc_completed': False,
                'calculation_submitted': False, 'execution_authorized': False,
                'scope': 'native_covalent_graph_check_not_full_md_admission'}
    record['md_graph_plan'] = plan
    plan['engine'] = request.engine
    plan['discovery_route'] = 'ligand-first'
    plan['calculation_submitted_scope'] = 'this_planning_operation'
    refresh(root, state)
    return plan


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response
    from .ligand_parent_evidence import version_lock
    from .workflow_store import load
    from .workflow_protocol import changed, revision
    from .ligand_covdock_execution import observe, review, reject_evidence
    required = {'select_md_candidate': {'compound_id', 'input_sdf_index', 'reason'},
                'release_md_candidate': {'reason'}, 'check_md_graph_runtime': set()}
    if action not in required or set(arguments) != required[action]:
        raise ValueError('Use only the fields required by the current MD selection action')
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if state.get('pending') or state['stage'] != review_stage(state):
            raise ValueError('MD selection requires an idle current CovDock review')
        if action == 'release_md_candidate':
            result = release(state, arguments['reason'])
        elif state.get('historical_md_source'):
            if action != 'check_md_graph_runtime':
                raise ValueError('Use prepare_historical_md_candidate to bind the imported candidate')
            validate(root, state)
            result = plan_graph(root, state)
        else:
            record = source_record(state)
            try:
                output = observe(root, state, record['run_id'])
                review(root, state, output, reuse=True)
            except (ValueError, KeyError, TypeError, OSError) as error:
                reject_evidence(state, error)
                changed(location, state, 'md_selection_source_rejected')
                raise
            try:
                if not record.get('md_handoff'):
                    raise ValueError('Prepare a verified current-study MD handoff before selecting')
                result = select(root, state, **arguments) if action == 'select_md_candidate' else plan_graph(root, state)
            except (ValueError, KeyError, TypeError, OSError):
                refresh(root, state)
                changed(location, state, 'md_selection_action_rejected')
                raise
        changed(location, state, action)
        return response(state, result)
