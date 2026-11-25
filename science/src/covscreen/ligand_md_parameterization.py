"""Ligand workflow binding over the shared native parameterization executor."""
from copy import deepcopy

from .ligand_md_source import record as source_record, project_id, review_stage

from .approval_scope import normalize_spec
from .execution import JobManager, NativeAdductRuntimeUnavailable
from .jobs import sha256_file
from .ligand_parent_evidence import version_lock
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load

ENGINE = 'library-first-adduct-parameterization'
STAGES = {'MD_PARAMETERIZATION_RUNTIME_REQUIRED', 'MD_PARAMETERIZATION_PLAN_READY',
          'MD_PARAMETERIZATION_RUNNING', 'MD_PARAMETERIZATION_REVIEW'}
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}


def parent_input(root, state):
    from .ligand_md_graph_execution import revalidate_saved
    stage = state['stage']
    try:
        error = revalidate_saved(root, state)
        record = state.get('md_graph_execution') or {}
        if error or record.get('verification') != 'verified' or not record.get('admission'):
            raise ValueError('Parameterization requires the verified original graph: ' + (error or 'not verified'))
        return deepcopy(record['admission'])
    finally:
        state['stage'] = stage


def request(root, state):
    from .tool_api import _adduct_parameterization_request
    parent = parent_input(root, state)
    value = _adduct_parameterization_request(root, {
        'adduct_admission_run_id': parent['run_id'],
        'adduct_input_manifest_sha256': parent['adduct_input_manifest_sha256']}, execution_mode='plan')
    return value.model_copy(update={'project_id': project_id(state),
        'request_id': state['workflow_id'] + '-parameterization-' + parent['manifest_sha256'][:16],
        'parameters': {**value.parameters, 'workflow_id': state['workflow_id']}})


def validate_request(root, value):
    from .ligand_workflow import directory
    state = load(directory(root, value.parameters['workflow_id']))
    expected = request(root, state)
    fields = ('request_id', 'project_id', 'route', 'engine', 'parameters',
              'resource_budget', 'parent_run_id', 'attempt')
    if any(getattr(value, key) != getattr(expected, key) for key in fields):
        raise ValueError('Parameterization request differs from the bound workflow candidate and budget')
    record = state.get('md_parameterization') or {}
    pending = state.get('pending') or {}
    if (state['stage'] != 'MD_PARAMETERIZATION_PLAN_READY' or record.get('run_id')
            or record.get('approved_scope') and pending.get('action') != 'start_md_parameterization'
            or pending and pending.get('action') != 'start_md_parameterization'
            or (record.get('plan') or {}).get('state') != 'plan_compiled' or record.get('evidence_error')):
        raise ValueError('Parameterization requires the original unsubmitted plan')


def require_dispatch_authority(root, value):
    from .native_approval import CURRENT, decode, NativeApprovalDenied
    from .ligand_workflow import directory, DESIGN
    if not CURRENT.get():
        raise NativeApprovalDenied('Parameterization dispatch requires native workflow approval')
    proof = decode(CURRENT.get())
    state = load(directory(root, value.parameters['workflow_id']))
    record = state.get('md_parameterization') or {}
    if (proof.get('tool') != DESIGN or (state.get('pending') or {}).get('action') != 'start_md_parameterization'
            or record.get('approved_scope') != proof.get('scope') or record.get('request_id') != value.request_id):
        raise NativeApprovalDenied('Parameterization requires the original pending workflow approval')


def plan(root, state):
    record = state.get('md_parameterization') or {}
    if state.get('pending') or record.get('run_id') or record.get('approved_scope'):
        raise ValueError('Observe or reconcile the original parameterization; do not replan it')
    if state['stage'] not in STAGES | {'MD_GRAPH_REVIEW'}:
        raise ValueError('Parameterization planning requires completed graph review')
    parent = parent_input(root, state)
    selection = deepcopy(source_record(state)['md_selection'])
    if record and (record['input'] != parent or record['selection'] != selection):
        raise ValueError('Parameterization parent or selected candidate changed')
    old = record.get('plan') or {}
    try:
        compiled = normalize_spec(JobManager(root)._prepare_submission(request(root, state)))
    except NativeAdductRuntimeUnavailable as error:
        current = {'state': 'runtime_configuration_required', 'reason': str(error)}
        if old.get('spec'):
            current['spec'] = old['spec']
        stage = 'MD_PARAMETERIZATION_RUNTIME_REQUIRED'
    else:
        if old.get('spec') and old['spec'] != compiled:
            raise ValueError('Frozen parameterization plan changed; release the unexecuted plan before replanning')
        current = {'state': 'plan_compiled', 'spec': compiled, 'execution_authorized': False}
        stage = 'MD_PARAMETERIZATION_PLAN_READY'
    state['md_parameterization'] = {'input': parent, 'selection': selection, 'plan': current,
        'verification': 'not_ready', 'compute_submitted': False}
    state['stage'] = stage


def execution_request(root, state, arguments):
    record = state.get('md_parameterization') or {}
    if (state.get('pending') or record.get('run_id') or record.get('approved_scope')
            or state['stage'] != 'MD_PARAMETERIZATION_PLAN_READY'):
        raise ValueError('Observe the original parameterization; execution needs an idle frozen plan')
    value = request(root, state)
    if (record['input'] != parent_input(root, state)
            or record['selection'] != source_record(state)['md_selection']
            or normalize_spec(JobManager(root)._prepare_submission(value)) != record['plan'].get('spec')):
        raise ValueError('Parameterization plan or selected input changed')
    return value.model_copy(update={'execution_mode': 'execute',
        'human_approved': arguments.get('human_approved', False), 'approval_note': arguments.get('approval_note')})


def start_request(root, arguments):
    from .ligand_workflow import directory
    folder = directory(root, arguments['workflow_id'])
    with version_lock(folder, '.workflow.lock'):
        state = load(folder)
        revision(state, arguments['expected_revision'])
        if arguments.get('action') != 'start_md_parameterization' or set(arguments) - {
                'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}:
            raise ValueError('Workflow owns parameterization inputs and resource budget')
        return execution_request(root, state, arguments)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    from .md_parameterization_evidence import parameterization_receipt
    from .worker_observation import observe_worker
    record = state['md_parameterization']
    record.update(verification='evidence_rejected', job_identity_verified=False)
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    verify_spec(spec)
    scope = record['approved_scope']
    if (spec.engine != ENGINE or spec.route != 'library-first' or spec.project_id != project_id(state)
            or spec.request_id != record['request_id'] or spec.parent_run_id != record['input']['run_id']
            or spec.parameters.get('workflow_id') != state['workflow_id'] or normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or any(getattr(result, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Parameterization job differs from its exact approved workflow plan')
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        job_identity_verified=True, approval='APPROVED', compute_submitted=True,
        failure_reason=result.failure_reason, message=result.message,
        worker=observe_worker(manager.registry, spec, result))
    state['stage'] = 'MD_PARAMETERIZATION_REVIEW' if result.state in TERMINAL else 'MD_PARAMETERIZATION_RUNNING'
    for name, digest in spec.input_sha256.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError('Parameterization frozen input changed: ' + name)
    parent = parent_input(root, state)
    if parent != record['input'] or source_record(state)['md_selection'] != record['selection']:
        raise ValueError('Parameterization parent or candidate changed after approval')
    record.pop('evidence_error', None)
    record['verification'] = 'not_ready'
    if result.state == 'COMPLETED' and result.return_code == 0:
        record['parameterization'] = parameterization_receipt(root, record, manager, spec, result, parent)
        record['verification'] = 'verified'
    return projection(state)


def reject(state, error):
    record = state['md_parameterization']
    record.update(verification='evidence_rejected', evidence_error=str(error))
    if record.get('run_id') and not record.get('job_identity_verified'):
        state['stage'] = 'MD_PARAMETERIZATION_RUNNING'


def revalidate_saved(root, state):
    record = state.get('md_parameterization') or {}
    if state.get('pending'):
        return
    try:
        if record.get('run_id'):
            observe(root, state, record['run_id'])
        elif record:
            if parent_input(root, state) != record['input']:
                raise ValueError('Frozen parameterization parent changed')
            for name, digest in (record.get('plan') or {}).get('spec', {}).get('input_sha256', {}).items():
                path = root / name
                if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
                    raise ValueError('Parameterization planned input changed: ' + name)
            record.pop('evidence_error', None)
    except (ValueError, KeyError, TypeError, OSError) as error:
        reject(state, error)
        return str(error)


def projection(state):
    record = state.get('md_parameterization')
    if not record:
        return None
    value = deepcopy(record)
    value.pop('approved_scope', None)
    witness = value.pop('parameterization', None)
    value['parameterization'] = witness if value.get('verification') == 'verified' else None
    if witness and value['parameterization'] is None:
        value['historical_parameterization'] = witness
    value.update(scope='registered_covalent_parameterization_not_md_system_readiness',
        parameterization_completed=value.get('verification') == 'verified', md_system_ready=False,
        simulation_started=False, discovery_route='ligand-first', engine=ENGINE)
    return value


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response, DESIGN
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    from .submission_identity import lookup_submission
    folder = directory(root, workflow_id)
    with version_lock(folder, '.workflow.lock'):
        state = load(folder)
        revision(state, expected_revision)
        if action in {'plan_md_parameterization', 'release_md_parameterization_plan'}:
            if arguments or state.get('pending'):
                raise ValueError('Workflow owns parameterization inputs; reconcile pending operations')
            try:
                if action == 'plan_md_parameterization':
                    plan(root, state)
                else:
                    record = state.get('md_parameterization') or {}
                    if state['stage'] not in STAGES or not record or record.get('run_id') or record.get('approved_scope'):
                        raise ValueError('Only an unsubmitted parameterization plan can be released')
                    state.setdefault('md_parameterization_plan_history', []).append(state.pop('md_parameterization'))
                    state['stage'] = 'MD_GRAPH_REVIEW'
            except (ValueError, KeyError, TypeError, OSError) as error:
                if state.get('md_parameterization'):
                    reject(state, error)
                changed(folder, state, 'md_parameterization_plan_rejected')
                raise
            changed(folder, state, action)
            return response(state, projection(state))
        if action == 'start_md_parameterization':
            if not CURRENT.get():
                raise NativeApprovalDenied('Parameterization requires exact native approval')
            if set(arguments) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns parameterization inputs and resource budget')
            value = execution_request(root, state, arguments)
            state['md_parameterization'].update(request_id=value.request_id,
                approved_scope=decode(CURRENT.get())['scope'], job_identity_verified=False)
            begin_operation(folder, state, DESIGN, action, submission={
                'project_id': value.project_id, 'request_id': value.request_id, 'execution_mode': 'execute'})
            run_id = JobManager(root).submit(value).run_id
        elif action == 'reconcile':
            pending = state.get('pending') or {}
            if arguments or pending.get('action') != 'start_md_parameterization':
                raise ValueError('No uncertain parameterization submission to reconcile')
            found = lookup_submission(root, pending['submission'])
            if not found.get('registered_job_observed'):
                return response(state, found)
            run_id = found['run_id']
        elif action == 'md_parameterization_status':
            if arguments or state.get('pending') or state['stage'] not in STAGES or not state.get('md_parameterization', {}).get('run_id'):
                raise ValueError('Observe only the original registered parameterization task')
            run_id = state['md_parameterization']['run_id']
        else:
            raise ValueError('Unknown parameterization action')
        state['md_parameterization'].setdefault('run_id', run_id)
        state['pending'] = None
        try:
            output = observe(root, state, run_id)
        except (ValueError, KeyError, TypeError, OSError) as error:
            reject(state, error)
            changed(folder, state, 'md_parameterization_evidence_rejected')
            raise
        changed(folder, state, action)
        return response(state, output)
