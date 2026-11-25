"""Approved single-task graph checking over the shared executor and reader."""
from copy import deepcopy

from .ligand_md_source import record as source_record, project_id, review_stage

from .approval_scope import normalize_spec
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .ligand_md_selection import graph_request, validate
from .ligand_parent_evidence import version_lock
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load

STAGES = {'MD_GRAPH_RUNNING', 'MD_GRAPH_REVIEW'}
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}


def require_dispatch_authority(root, request):
    from .native_approval import CURRENT, decode, NativeApprovalDenied
    from .ligand_workflow import directory, DESIGN
    if not CURRENT.get():
        raise NativeApprovalDenied('Graph dispatch requires native workflow approval')
    proof = decode(CURRENT.get())
    state = load(directory(root, request.parameters['workflow_id']))
    record = state.get('md_graph_execution') or {}
    if (proof.get('tool') != DESIGN or (state.get('pending') or {}).get('action') != 'start_md_graph'
            or record.get('approved_scope') != proof.get('scope') or record.get('request_id') != request.request_id):
        raise NativeApprovalDenied('Graph dispatch requires the original pending workflow approval')


def validate_request(root, request):
    """Raw execute requests must use the same frozen workflow and bounded plan."""
    from .ligand_workflow import directory
    state = load(directory(root, request.parameters['workflow_id']))
    expected = graph_request(root, state)
    fields = ('request_id', 'project_id', 'route', 'engine', 'parameters',
              'resource_budget', 'parent_run_id', 'attempt')
    if any(getattr(request, key) != getattr(expected, key) for key in fields):
        raise ValueError('Graph request differs from the bound workflow candidate and budget')
    record = state.get('md_graph_execution') or {}
    pending = state.get('pending') or {}
    plan = source_record(state).get('md_graph_plan') or {}
    if (state['stage'] != review_stage(state) or record.get('run_id')
            or record and pending.get('action') != 'start_md_graph'
            or pending and pending.get('action') != 'start_md_graph'
            or plan.get('state') != 'plan_compiled' or source_record(state).get('md_graph_plan_error')):
        raise ValueError('Graph execution requires the unsubmitted frozen workflow plan')


def current_source(root, state):
    if state.get('historical_md_source'):
        validate(root, state)
        return
    from .ligand_covdock_execution import revalidate_saved
    stage = state['stage']
    try:
        parent = source_record(state)
        frozen = (state.get('md_graph_execution') or {}).get('handoff')
        if not parent.get('md_handoff') and frozen:
            # Re-present the original witness for full replay, never adopt a
            # newly derived delivery after a temporary upstream failure.
            parent['md_handoff'] = deepcopy(frozen)
        diagnosis = revalidate_saved(root, state)
        if diagnosis:
            raise ValueError('Current graph source could not be verified: ' + diagnosis)
        validate(root, state)
    finally:
        state['stage'] = stage


def execution_request(root, state, arguments):
    if state.get('pending') or state.get('md_graph_execution') or state['stage'] != review_stage(state):
        raise ValueError('Observe the original graph submission; execution needs an idle frozen plan')
    current_source(root, state)
    record = source_record(state)
    plan = record.get('md_graph_plan') or {}
    if plan.get('state') != 'plan_compiled' or record.get('md_graph_plan_error'):
        raise ValueError('Graph execution requires a verified frozen plan and configured runtime')
    request = graph_request(root, state)
    if normalize_spec(JobManager(root)._prepare_submission(request)) != plan.get('spec'):
        raise ValueError('Graph plan changed; explicitly release the unexecuted selection before replanning')
    return JobSubmissionRequest.model_validate({**request.model_dump(mode='json'),
        'execution_mode': 'execute', 'human_approved': arguments.get('human_approved', False),
        'approval_note': arguments.get('approval_note')})


def start_request(root, arguments):
    from .ligand_workflow import directory
    folder = directory(root, arguments['workflow_id'])
    with version_lock(folder, '.workflow.lock'):
        state = load(folder)
        revision(state, arguments['expected_revision'])
        if arguments.get('action') != 'start_md_graph' or set(arguments) - {
                'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}:
            raise ValueError('Workflow owns graph inputs and resource budget')
        return execution_request(root, state, arguments)


def reject(state, error):
    record = state['md_graph_execution']
    record.update(verification='evidence_rejected', evidence_error=str(error))
    if not record.get('job_identity_verified') or state['stage'] not in STAGES:
        state['stage'] = 'MD_GRAPH_RUNNING'


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    from .md_graph_evidence import graph_admission_receipt
    from .worker_observation import observe_worker
    record = state['md_graph_execution']
    record['verification'] = 'evidence_rejected'
    record['job_identity_verified'] = False
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    verify_spec(spec)
    scope = record['approved_scope']
    if (spec.engine != 'library-first-md-adduct-admission' or spec.route != 'library-first'
            or spec.project_id != project_id(state) or spec.request_id != record['request_id']
            or spec.parameters.get('workflow_id') != state['workflow_id']
            or normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or any(getattr(result, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Graph job differs from the exact approved workflow plan')
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        job_identity_verified=True, approval='APPROVED', compute_submitted=True,
        worker=observe_worker(manager.registry, spec, result))
    state['stage'] = 'MD_GRAPH_REVIEW' if result.state in TERMINAL else 'MD_GRAPH_RUNNING'
    for name, digest in spec.input_sha256.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError('Graph frozen input changed: ' + name)
    # Do not let the parent review overwrite the graph execution stage.
    current_source(root, state)
    parent = source_record(state)
    if record['selection'] != parent.get('md_selection') or record['plan'] != parent.get('md_graph_plan'):
        raise ValueError('Graph selected input or original plan changed after approval')
    expected = graph_request(root, state)
    if (expected.request_id != spec.request_id or expected.resource_budget != spec.resource_budget
            or any(spec.parameters.get(key) != value for key, value in expected.parameters.items())):
        raise ValueError('Graph source binding differs from its approved specification')
    record.pop('evidence_error', None)
    record['verification'] = 'not_ready'
    if result.state == 'COMPLETED' and result.return_code == 0:
        record['admission'] = graph_admission_receipt(root, record, manager, spec, result, record['selection'])
        record['verification'] = 'verified'
    return projection(state)


def projection(state):
    record = state.get('md_graph_execution')
    if not record:
        return None
    value = deepcopy(record)
    value.pop('approved_scope', None)
    value.pop('plan', None)
    value.pop('handoff', None)
    witness = value.pop('admission', None)
    value['admission'] = witness if value.get('verification') == 'verified' else None
    if witness and value['admission'] is None:
        value['historical_admission'] = witness
    value.update(scope='native_covalent_graph_check_not_full_md_admission',
        native_graph_qc_verified=value.get('verification') == 'verified',
        parameterization_completed=False, md_system_ready=False, discovery_route='ligand-first',
        engine='library-first-md-adduct-admission')
    return value


def revalidate_saved(root, state):
    record = state.get('md_graph_execution') or {}
    if state.get('pending') or not record.get('run_id'):
        return
    try:
        observe(root, state, record['run_id'])
    except (ValueError, KeyError, TypeError, OSError) as error:
        reject(state, error)
        return str(error)


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response, DESIGN
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    from .submission_identity import lookup_submission
    folder = directory(root, workflow_id)
    with version_lock(folder, '.workflow.lock'):
        state = load(folder)
        revision(state, expected_revision)
        if action == 'start_md_graph':
            if not CURRENT.get():
                raise NativeApprovalDenied('Graph execution requires exact native approval')
            if set(arguments) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns graph execution inputs and budget')
            request = execution_request(root, state, arguments)
            parent = source_record(state)
            state['md_graph_execution'] = {'request_id': request.request_id,
                'approved_scope': decode(CURRENT.get())['scope'],
                'selection': deepcopy(parent['md_selection']), 'plan': deepcopy(parent['md_graph_plan']),
                'handoff': deepcopy(parent['md_handoff']),
                'verification': 'not_ready', 'job_identity_verified': False}
            begin_operation(folder, state, DESIGN, action, submission={
                'project_id': request.project_id, 'request_id': request.request_id, 'execution_mode': 'execute'})
            run_id = JobManager(root).submit(request).run_id
        elif action == 'reconcile':
            pending = state.get('pending') or {}
            if arguments or pending.get('action') != 'start_md_graph':
                raise ValueError('No uncertain graph submission to reconcile')
            found = lookup_submission(root, pending['submission'])
            if not found.get('registered_job_observed'):
                return response(state, found)
            run_id = found['run_id']
        elif action == 'md_graph_status':
            if arguments or state.get('pending') or state['stage'] not in STAGES:
                raise ValueError('Observe only the original registered graph task')
            run_id = state['md_graph_execution']['run_id']
        else:
            raise ValueError('Unknown graph execution action')
        state['md_graph_execution'].setdefault('run_id', run_id)
        # Once registration is observed, the task identity must be retained even
        # if its signed inputs or output cannot currently be verified.
        state['pending'] = None
        try:
            output = observe(root, state, run_id)
        except (ValueError, KeyError, TypeError, OSError) as error:
            reject(state, error)
            changed(folder, state, 'md_graph_evidence_rejected')
            raise
        changed(folder, state, action)
        return response(state, output)
