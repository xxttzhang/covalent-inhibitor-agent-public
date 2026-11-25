"""Native Library LO registration, exact approval and registered result review."""
from copy import deepcopy
import json

from .approval_scope import normalize_spec
from .covdock_vs import bound_file
from .execution import JobManager
from .jobs import sha256_file
from .library_lo_input import request_for, load as load_input
from .library_vs_prescreen_execution import workflow, digest, TERMINAL
from .ligand_parent_evidence import version_lock
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load

ACTIONS = {'start_lo', 'lo_status', 'evaluate_lo', 'report_lo'}
STAGES = {'LO_EXECUTION_RUNNING', 'LO_EXECUTION_REVIEW'}


def public(state):
    record = state.get('lo_execution')
    return {k: deepcopy(v) for k, v in record.items() if k not in {'approved_scope_sha256', 'evaluation'}} if record else None


def actions(state):
    if state.get('pending'):
        return []
    if (state['stage'] == 'LO_PLAN_READY' and not state.get('lo_execution') and not state.get('lo_input_error')
            and (state.get('lo_runtime') or {}).get('state') == 'LO_PLAN_COMPILED'):
        return ['start_lo']
    record = state.get('lo_execution') or {}
    if state['stage'] in STAGES and record.get('run_id'):
        return ['lo_status'] + (['evaluate_lo', 'report_lo'] if
            (record.get('job_state'), record.get('return_code')) in {('COMPLETED', 0), ('FAILED', 2)} else [])
    return []


def planned_request(root, state):
    ref = state.get('lo_plan')
    if not ref or (state.get('lo_runtime') or {}).get('state') != 'LO_PLAN_COMPILED':
        raise ValueError('Library LO requires its frozen runtime plan and positive environment review')
    path = bound_file(root, ref['path'])
    if sha256_file(path) != ref['sha256'] or path.stat().st_mode & 0o077:
        raise ValueError('Library LO private plan changed')
    request = request_for(root, state)
    if not request.parameters['environment_eligible_for_exploratory_refinement']:
        raise ValueError('Library LO requires a positive source-bound refinement judgment')
    return request, json.loads(path.read_text())


def validate_request(root, request):
    state = load(workflow().directory(root, request.project_id))
    expected = request_for(root, state)
    if any(getattr(request, k) != getattr(expected, k) for k in
           ('request_id', 'project_id', 'route', 'engine', 'parameters', 'resource_budget', 'attempt', 'parent_run_id')):
        raise ValueError('Library LO request differs from the bound VS subset and budget')
    if request.execution_mode == 'execute' and (state['stage'] != 'LO_PLAN_READY'
            or (state.get('lo_execution') or {}).get('run_id')
            or state.get('pending') and state['pending'].get('action') != 'start_lo'
            or not expected.parameters['environment_eligible_for_exploratory_refinement']):
        raise ValueError('Library LO requires its original unsubmitted independently reviewed plan')


def execution_request(root, state, args):
    if state.get('pending') or state.get('lo_execution') or state['stage'] != 'LO_PLAN_READY':
        raise ValueError('Observe the original LO task; do not replace it')
    request, frozen = planned_request(root, state)
    if normalize_spec(JobManager(root)._prepare_submission(request)) != frozen:
        raise ValueError('Library LO source or runtime changed after planning')
    return request.model_copy(update={'execution_mode': 'execute', 'human_approved': args.get('human_approved', False),
        'approval_note': args.get('approval_note')})


def start_request(root, args):
    location = workflow().directory(root, args['workflow_id'])
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, args['expected_revision'])
        if args.get('action') != 'start_lo' or set(args) - {'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}:
            raise ValueError('Workflow owns Library LO parameters')
        return execution_request(root, state, args)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    from .source_release import verify_source
    record = state['lo_execution']
    record['job_identity_verified'] = False
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    verify_spec(spec)
    verify_source(spec)
    scope = decode(decode(spec.provenance['native_approval'])['approval'])['scope']
    if (digest(scope) != record['approved_scope_sha256'] or normalize_spec(spec) != scope['spec']
        or spec.route != 'library-first' or spec.engine != 'schrodinger-covdock-lo'
        or spec.project_id != state['workflow_id'] or spec.request_id != record['request_id']
        or any(getattr(result, k) != getattr(spec, k) for k in ('run_id', 'project_id', 'route', 'engine', 'attempt', 'parent_run_id'))):
        raise ValueError('Library LO task differs from the exact native-approved study plan')
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        job_identity_verified=True, compute_registered=True, md_ready=False, scientific_acceptance=False)
    state['stage'] = 'LO_EXECUTION_REVIEW' if result.state in TERMINAL else 'LO_EXECUTION_RUNNING'
    for name, checksum in spec.input_sha256.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('Library LO registered input changed')
    expected = request_for(root, state)
    if (expected.request_id != spec.request_id or expected.resource_budget != spec.resource_budget
            or any(spec.parameters.get(k) != v for k, v in expected.parameters.items())):
        raise ValueError('Library LO approved source differs from current evidence')
    record.pop('evidence_error', None)
    return public(state)


def review(root, state, *, reuse=False, create_report=False):
    from .ligand_covdock_evidence import evaluate_outputs, witness
    record = state['lo_execution']
    manager = JobManager(root)
    current = {**state, 'covdock_execution': record, 'refinement_input': state['lo_input']}
    value = evaluate_outputs(root, current, manager, manager.registry.load_spec(record['run_id']),
        manager.registry.load_result(record['run_id']), reuse=reuse, route='library-first')
    ref = witness(root, value)
    record.update(evaluation=value, evaluation_artifact=ref, output_validation=value['output_validation'],
        evaluation_verification='verified', evaluation_summary={'candidate_count': value['candidate_count'],
            'candidates': value['candidates'], 'policy': value['gate_policy'], 'limitations': value['limitations']})
    if create_report or record.get('report'):
        path = bound_file(root, ref['path']).with_name('library-beta-report.md')
        raw = ('# Library CovDock-LO review\n\nRegistered run: ' + record['run_id']
            + '\n\nIndependent LO evidence for candidate review. AF3 is optional; MD is outside this beta. Computational ranking does not establish activity.\n\n```json\n'
            + json.dumps(record['evaluation_summary'], ensure_ascii=False, indent=2, allow_nan=False) + '\n```\n').encode()
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(raw)
        if path.is_symlink() or path.read_bytes() != raw:
            raise ValueError('Library LO report bytes changed')
        record['report'] = {'path': str(path.relative_to(root)), 'sha256': sha256_file(path), 'md_ready': False}


def reject(state, error):
    record = state['lo_execution']
    record.update(evidence_error=str(error), evaluation_verification='evidence_rejected',
        output_validation='EVIDENCE_REJECTED', job_identity_verified=False, md_ready=False, scientific_acceptance=False)
    record.pop('evaluation_summary', None)
    record.pop('report', None)


def refresh(root, state):
    record = state.get('lo_execution') or {}
    if not record.get('run_id') or state.get('pending'):
        return
    stage = state['stage']
    try:
        observe(root, state, record['run_id'])
        if record.get('evaluation'):
            review(root, state, reuse=True)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        reject(state, error)
    finally:
        if stage not in STAGES:
            state['stage'] = stage


def reconcile(root, state):
    from .submission_identity import lookup_submission
    pending = state.get('pending') or {}
    if pending.get('action') != 'start_lo':
        raise ValueError('No uncertain LO registration to reconcile')
    found = lookup_submission(root, pending['submission'])
    if found.get('registered_job_observed'):
        state['lo_execution']['run_id'] = found['run_id']
        observe(root, state, found['run_id'])
        state['pending'] = None
    return found


def step(root, tool, workflow_id, expected_revision, action, args):
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    route = workflow()
    location = route.directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if tool != route.RESEARCH or action not in actions(state):
            raise ValueError('Library LO execution action is unavailable at this stage')
        if action == 'start_lo':
            if not CURRENT.get():
                raise NativeApprovalDenied('Library LO requires separate exact native approval')
            if set(args) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns LO execution inputs')
            request = execution_request(root, state, args)
            state['lo_execution'] = {'request_id': request.request_id,
                'approved_scope_sha256': digest(decode(CURRENT.get())['scope']),
                'job_identity_verified': False, 'md_ready': False, 'scientific_acceptance': False}
            begin_operation(location, state, tool, action, submission={
                'project_id': request.project_id, 'request_id': request.request_id, 'execution_mode': 'execute'})
            run_id = JobManager(root).submit(request).run_id
        else:
            if args:
                raise ValueError('Observe only the original LO task')
            run_id = state['lo_execution']['run_id']
        state['lo_execution']['run_id'] = run_id
        try:
            observe(root, state, run_id)
            if action in {'evaluate_lo', 'report_lo'} or state['lo_execution'].get('evaluation'):
                review(root, state, reuse=action != 'evaluate_lo', create_report=action == 'report_lo')
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            reject(state, error)
            state['pending'] = None
            changed(location, state, 'library_lo_evidence_rejected')
            raise
        state['pending'] = None
        changed(location, state, action)
        return route.response(state, public(state))
