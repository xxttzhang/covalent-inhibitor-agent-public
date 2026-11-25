"""Ligand session lifecycle for preparation, short-QC and equilibration engines.

Scientific inputs, budgets and protocols come from the shared request builders.
Each stage owns a distinct frozen approval and durable submission identity.
"""
from copy import deepcopy

from .approval_scope import normalize_spec
from .execution import JobManager
from .jobs import sha256_file
from .ligand_md_source import record as source_record, project_id
from .ligand_parent_evidence import version_lock
from .native_approval import CURRENT, NativeApprovalDenied, decode, verify_spec
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load

ENGINES = {'system_preparation': 'library-first-openmm-system-preparation',
           'short_qc': 'library-first-openmm-short-qc',
           'equilibration': 'library-first-openmm-equilibration'}
PARENTS = {'system_preparation': 'parameterization', 'short_qc': 'system_preparation', 'equilibration': 'short_qc'}
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}
ACTIONS = {action for name in ENGINES for action in (
    f'plan_md_{name}', f'start_md_{name}', f'md_{name}_status', f'release_md_{name}_plan',
    f'retry_md_{name}_plan')}
STARTS = {f'start_md_{name}' for name in ENGINES}


def stages(name):
    return {f'MD_{name.upper()}_{suffix}' for suffix in ('PLAN_READY', 'RUNNING', 'REVIEW')}


def active(state):
    return next((name for name in reversed(ENGINES) if state.get('md_' + name)), None)


def parent_input(root, state, name):
    parent = PARENTS[name]
    original = state['stage']
    try:
        if parent == 'parameterization':
            from .ligand_md_parameterization import revalidate_saved as revalidate
            error = revalidate(root, state)
        else:
            error = revalidate_saved(root, state, parent)
        record = state.get('md_' + parent) or {}
        if error or record.get('verification') != 'verified' or not record.get(parent):
            raise ValueError('MD stage requires the verified original ' + parent + ': ' + (error or 'not verified'))
        return deepcopy(record[parent])
    finally:
        state['stage'] = original


def request(root, state, name):
    from . import tool_api
    parent = parent_input(root, state, name)
    fields = ({'parameterization_run_id': parent['run_id'],
               'parameterization_audit_sha256': parent['manifest_sha256']} if name == 'system_preparation' else
              {'system_preparation_run_id': parent['run_id'],
               'preparation_manifest_sha256': parent['manifest_sha256']} if name == 'short_qc' else
              {'short_qc_run_id': parent['run_id'], 'short_qc_manifest_sha256': parent['manifest_sha256']})
    factory = getattr(tool_api, '_openmm_' + name + '_request')
    if name == 'equilibration':
        fields['workflow_id'] = state['workflow_id']
    value = factory(root, fields, execution_mode='plan')
    previous = [record for record in (state.get('md_preparation_attempt_history') or []) if record['stage'] == name]
    suffix = '-retry-' + str(len(previous)) if previous else ''
    return value.model_copy(update={'project_id': project_id(state),
        'request_id': state['workflow_id'] + '-' + name + '-' + parent['manifest_sha256'][:16] + suffix,
        'attempt': len(previous) + 1,
        'parameters': {**value.parameters, 'workflow_id': state['workflow_id']}})


def name_for_engine(value):
    return next(name for name, engine in ENGINES.items() if engine == value.engine)


def validate_request(root, value):
    from .ligand_workflow import directory
    name = name_for_engine(value)
    state = load(directory(root, value.parameters['workflow_id']))
    expected = request(root, state, name)
    if any(getattr(value, key) != getattr(expected, key) for key in (
            'request_id', 'project_id', 'route', 'engine', 'parameters', 'resource_budget', 'parent_run_id', 'attempt')):
        raise ValueError('MD request differs from the bound workflow candidate and budget')
    record = state.get('md_' + name) or {}
    pending = state.get('pending') or {}
    if (state['stage'] != f'MD_{name.upper()}_PLAN_READY' or record.get('run_id')
            or record.get('approved_scope') and pending.get('action') != 'start_md_' + name
            or pending and pending.get('action') != 'start_md_' + name
            or (record.get('plan') or {}).get('state') != 'plan_compiled' or record.get('evidence_error')):
        raise ValueError('MD execution requires the original unsubmitted plan')


def require_dispatch_authority(root, value):
    from .ligand_workflow import directory, DESIGN
    if not CURRENT.get():
        raise NativeApprovalDenied('MD dispatch requires native workflow approval')
    name = name_for_engine(value)
    proof = decode(CURRENT.get())
    state = load(directory(root, value.parameters['workflow_id']))
    record = state.get('md_' + name) or {}
    if (proof.get('tool') != DESIGN or (state.get('pending') or {}).get('action') != 'start_md_' + name
            or record.get('approved_scope') != proof.get('scope') or record.get('request_id') != value.request_id):
        raise NativeApprovalDenied('MD dispatch requires the original pending workflow approval')


def plan(root, state, name):
    record = state.get('md_' + name) or {}
    if state.get('pending') or record.get('run_id') or record.get('approved_scope'):
        raise ValueError('Observe or reconcile the original MD task; do not replan it')
    if state['stage'] not in stages(name) | {f'MD_{PARENTS[name].upper()}_REVIEW'}:
        raise ValueError('MD planning requires completed parent review')
    parent = parent_input(root, state, name)
    selection = deepcopy(source_record(state)['md_selection'])
    if record and (record['input'] != parent or record['selection'] != selection):
        raise ValueError('MD parent or selected candidate changed')
    compiled = normalize_spec(JobManager(root)._prepare_submission(request(root, state, name)))
    old = (record.get('plan') or {}).get('spec')
    if old and old != compiled:
        raise ValueError('Frozen MD plan changed; release the unexecuted plan before replanning')
    state['md_' + name] = {'input': parent, 'selection': selection,
        'plan': {'state': 'plan_compiled', 'spec': compiled, 'execution_authorized': False},
        'verification': 'not_ready', 'compute_submitted': False}
    state['stage'] = f'MD_{name.upper()}_PLAN_READY'


def execution_request(root, state, arguments, name):
    record = state.get('md_' + name) or {}
    if (state.get('pending') or record.get('run_id') or record.get('approved_scope')
            or state['stage'] != f'MD_{name.upper()}_PLAN_READY'):
        raise ValueError('Observe the original MD task; execution needs an idle frozen plan')
    value = request(root, state, name)
    if (record['input'] != parent_input(root, state, name)
            or record['selection'] != source_record(state)['md_selection']
            or normalize_spec(JobManager(root)._prepare_submission(value)) != record['plan'].get('spec')):
        raise ValueError('MD plan or selected input changed')
    return value.model_copy(update={'execution_mode': 'execute',
        'human_approved': arguments.get('human_approved', False), 'approval_note': arguments.get('approval_note')})


def start_request(root, arguments):
    from .ligand_workflow import directory
    if arguments.get('action') not in STARTS or set(arguments) - {
            'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}:
        raise ValueError('Workflow owns MD inputs, protocol and resource budget')
    name = arguments['action'].removeprefix('start_md_')
    with version_lock(directory(root, arguments['workflow_id']), '.workflow.lock'):
        state = load(directory(root, arguments['workflow_id']))
        revision(state, arguments['expected_revision'])
        return execution_request(root, state, arguments, name)


def observe(root, state, run_id, name):
    from .md_preparation_evidence import preparation_receipt
    from .md_short_qc_evidence import short_qc_receipt
    from .md_equilibration_evidence import equilibration_receipt
    from .worker_observation import observe_worker
    record = state['md_' + name]
    record.update(verification='evidence_rejected', job_identity_verified=False)
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    verify_spec(spec)
    scope = record['approved_scope']
    if (spec.engine != ENGINES[name] or spec.route != 'library-first' or spec.project_id != project_id(state)
            or spec.request_id != record['request_id'] or spec.parent_run_id != record['input']['run_id']
            or spec.parameters.get('workflow_id') != state['workflow_id'] or normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or any(getattr(result, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('MD job differs from its exact approved workflow plan')
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        job_identity_verified=True, approval='APPROVED', compute_submitted=True,
        failure_reason=result.failure_reason, message=result.message,
        worker=observe_worker(manager.registry, spec, result))
    if name == 'equilibration':
        from .md_progress import read_equilibration_progress
        record['progress'] = read_equilibration_progress(
            manager.registry.job_dir(run_id) / 'artifacts/openmm-equilibration', spec.parameters['system_id'])
    state['stage'] = f'MD_{name.upper()}_' + ('REVIEW' if result.state in TERMINAL else 'RUNNING')
    for filename, digest in spec.input_sha256.items():
        path = root / filename
        if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
            raise ValueError('MD frozen input changed: ' + filename)
    parent = parent_input(root, state, name)
    if parent != record['input'] or source_record(state)['md_selection'] != record['selection']:
        raise ValueError('MD parent or candidate changed after approval')
    record.pop('evidence_error', None)
    record['verification'] = 'not_ready'
    if result.state == 'COMPLETED' and result.return_code == 0:
        validate = {'system_preparation': preparation_receipt, 'short_qc': short_qc_receipt,
                    'equilibration': equilibration_receipt}[name]
        record[name] = validate(root, record, manager, spec, result, parent)
        record['verification'] = 'verified'
    return projection(state, name)


def reject(state, error, name):
    record = state['md_' + name]
    record.update(verification='evidence_rejected', evidence_error=str(error))
    if record.get('run_id') and not record.get('job_identity_verified'):
        state['stage'] = f'MD_{name.upper()}_RUNNING'


def revalidate_saved(root, state, name=None):
    name = name or active(state)
    if not name or state.get('pending'):
        return
    record = state['md_' + name]
    try:
        if record.get('run_id'):
            observe(root, state, record['run_id'], name)
        else:
            if parent_input(root, state, name) != record['input']:
                raise ValueError('Frozen MD parent changed')
            for filename, digest in record['plan']['spec']['input_sha256'].items():
                path = root / filename
                if path.is_symlink() or not path.is_file() or sha256_file(path) != digest:
                    raise ValueError('MD planned input changed: ' + filename)
            record.pop('evidence_error', None)
    except (ValueError, KeyError, TypeError, OSError) as error:
        reject(state, error, name)
        return str(error)


def projection(state, name):
    record = state.get('md_' + name)
    if not record:
        return None
    value = deepcopy(record)
    value.pop('approved_scope', None)
    witness = value.pop(name, None)
    verified = value.get('verification') == 'verified'
    value[name] = witness if verified else None
    if witness and not verified:
        value['historical_' + name] = witness
    value.update(scope='registered_openmm_' + name, discovery_route='ligand-first', engine=ENGINES[name],
        md_system_ready=False, equilibration_completed=False, production_started=False)
    value[name + '_completed'] = verified
    value['next_stage'] = {'system_preparation': 'short_qc', 'short_qc': 'equilibration_review',
        'equilibration': 'production_review'}[name] if verified else None
    if name == 'equilibration':
        admitted = verified and witness['production_review_ready']
        value.update(equilibration_protocol_completed=verified, production_review_ready=admitted,
            equilibrium_convergence_claimed=False, checks=witness['checks'] if verified else None,
            next_stage='production_review' if admitted else None)
        # Protocol completion is not an equilibrium-convergence claim.
        value['equilibration_completed'] = False
    # A successful short-QC is integration evidence, never production/equilibration evidence.
    value['integration_stability_passed'] = name == 'short_qc' and verified
    return value


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response, DESIGN
    from .submission_identity import lookup_submission
    folder = directory(root, workflow_id)
    with version_lock(folder, '.workflow.lock'):
        state = load(folder)
        revision(state, expected_revision)
        name = (next((name for name in ENGINES if action in {
            f'plan_md_{name}', f'start_md_{name}', f'md_{name}_status', f'release_md_{name}_plan', f'retry_md_{name}_plan'}), None)
            if action != 'reconcile' else (state.get('pending') or {}).get('action', '').removeprefix('start_md_'))
        if name not in ENGINES:
            raise ValueError('Unknown MD preparation action')
        if action == f'retry_md_{name}_plan':
            if arguments or state.get('pending') or active(state) != name or state['stage'] not in stages(name):
                raise ValueError('Retry planning requires the original terminal MD stage')
            record = state.get('md_' + name) or {}
            if not record.get('run_id'):
                raise ValueError('Retry planning requires a registered failed task')
            # Re-read signed terminal evidence. Missing evidence is not proof of termination.
            observe(root, state, record['run_id'], name)
            if not record.get('job_identity_verified') or record.get('job_state') not in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
                raise ValueError('Only a confirmed failed terminal MD task can have a new plan')
            proposed = deepcopy(state)
            proposed['md_preparation_attempt_history'] = (proposed.get('md_preparation_attempt_history') or []) + [{'stage': name, **proposed.pop('md_' + name)}]
            proposed['stage'] = f'MD_{PARENTS[name].upper()}_REVIEW'
            plan(root, proposed, name)
            state.clear()
            state.update(proposed)
            changed(folder, state, action)
            return response(state, projection(state, name))
        if action in {f'plan_md_{name}', f'release_md_{name}_plan'}:
            if arguments or state.get('pending'):
                raise ValueError('Workflow owns MD inputs; reconcile pending operations')
            try:
                if action == f'plan_md_{name}':
                    plan(root, state, name)
                else:
                    record = state.get('md_' + name) or {}
                    if (state['stage'] not in stages(name) or not record or record.get('run_id')
                            or record.get('approved_scope') or active(state) != name):
                        raise ValueError('Only an unsubmitted MD plan can be released')
                    state['md_preparation_plan_history'] = (state.get('md_preparation_plan_history') or []) + [{'stage': name, **state.pop('md_' + name)}]
                    state['stage'] = f'MD_{PARENTS[name].upper()}_REVIEW'
            except (ValueError, KeyError, TypeError, OSError) as error:
                if state.get('md_' + name):
                    reject(state, error, name)
                changed(folder, state, 'md_' + name + '_plan_rejected', {'reason': str(error)})
                raise
            changed(folder, state, action)
            return response(state, projection(state, name))
        if action == f'start_md_{name}':
            if not CURRENT.get():
                raise NativeApprovalDenied('MD execution requires exact native approval')
            if set(arguments) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns MD inputs, protocol and resource budget')
            value = execution_request(root, state, arguments, name)
            state['md_' + name].update(request_id=value.request_id,
                approved_scope=decode(CURRENT.get())['scope'], job_identity_verified=False)
            begin_operation(folder, state, DESIGN, action, submission={
                'project_id': value.project_id, 'request_id': value.request_id, 'execution_mode': 'execute'})
            run_id = JobManager(root).submit(value).run_id
        elif action == 'reconcile':
            pending = state.get('pending') or {}
            if arguments or pending.get('action') != 'start_md_' + name:
                raise ValueError('No uncertain MD submission to reconcile')
            found = lookup_submission(root, pending['submission'])
            if not found.get('registered_job_observed'):
                return response(state, found)
            run_id = found['run_id']
        elif action == f'md_{name}_status':
            if (arguments or state.get('pending') or state['stage'] not in stages(name)
                    or not state.get('md_' + name, {}).get('run_id')):
                raise ValueError('Observe only the original registered MD task')
            run_id = state['md_' + name]['run_id']
        state['md_' + name].setdefault('run_id', run_id)
        state['pending'] = None
        try:
            output = observe(root, state, run_id, name)
        except (ValueError, KeyError, TypeError, OSError) as error:
            reject(state, error, name)
            changed(folder, state, 'md_' + name + '_evidence_rejected')
            raise
        changed(folder, state, action)
        return response(state, output)
