"""Exact native Library VS approval, registration and same-task observation.

Execution completion is not acceptance of poses. Product assessment is a
separate gate; no old prescreen or CarsiDock approval is transferred.
"""
from copy import deepcopy
import json

from .approval_scope import normalize_spec
from .covdock_vs import bound_file
from .execution import JobManager
from .jobs import sha256_file
from .library_vs_input import available, load as load_input, environment_review, request_for
from .library_vs_prescreen_execution import workflow, digest, TERMINAL
from .ligand_parent_evidence import version_lock
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load

ACTIONS = {'start_vs', 'vs_status', 'evaluate_vs', 'report_vs'}
STAGES = {'VS_EXECUTION_RUNNING', 'VS_EXECUTION_REVIEW'}


def public(state):
    record = state.get('vs_execution')
    return {key: deepcopy(value) for key, value in record.items() if key != 'approved_scope_sha256'} if record else None


def assessment(state):
    record = state.get('vs_execution') or {}
    summary = record.get('evaluation_summary')
    outcome = 'not_evaluated'
    if summary:
        outcome = ('review_required' if summary['passing_candidate_count'] else 'rejected'
            if all(row['evidence_complete'] for row in summary['candidates']) else 'evidence_insufficient')
    if record.get('evidence_error'):
        outcome = 'evidence_insufficient'
    return {'scope': 'library_covdock_vs_pose_review', 'state': outcome,
        'reason': 'Provisional product/geometry review is not biological validation or MD admission.'}


def actions(state):
    if state.get('pending'):
        return []
    if state['stage'] == 'VS_PLAN_READY' and available(state):
        return ['start_vs']
    if state['stage'] in STAGES and (state.get('vs_execution') or {}).get('run_id'):
        return ['vs_status'] + (['evaluate_vs', 'report_vs'] if state['stage'] == 'VS_EXECUTION_REVIEW'
            and state['vs_execution'].get('job_state') == 'COMPLETED' else [])
    return []


def planned_request(root, state):
    plan = state.get('vs_plan') or {}
    if not plan or (state.get('vs_runtime') or {}).get('state') != 'VS_PLAN_COMPILED':
        raise ValueError('Library VS requires its frozen runtime plan and positive environment assessment')
    payload = load_input(root, state)
    reviewed = environment_review(root, state, payload)
    if not reviewed or reviewed['decision'] != 'suitable_for_exploratory_vs':
        raise ValueError('Library VS requires a current source-bound positive environment assessment')
    path = bound_file(root, plan['path'])
    if sha256_file(path) != plan['sha256']:
        raise ValueError('Library VS private execution plan changed')
    request = request_for(root, state, JobManager(root).config.get('schrodinger', {}),
                          source_revision=plan['source_revision'])
    return request, json.loads(path.read_text())


def validate_request(root, request):
    from .docking_policy import POLICY
    if not POLICY['native_vs_workflow_ready']:
        raise ValueError('CovDock-VS native workflow migration is not yet complete')
    state = load(workflow().directory(root, request.project_id))
    expected, _ = planned_request(root, state)
    if (state['stage'] != 'VS_PLAN_READY' or (state.get('vs_execution') or {}).get('run_id')
        or state.get('pending') and state['pending'].get('action') != 'start_vs'
        or any(getattr(request, key) != getattr(expected, key) for key in
               ('request_id', 'project_id', 'route', 'engine', 'parameters', 'resource_budget', 'attempt', 'parent_run_id'))):
        raise ValueError('Library VS request differs from its original unsubmitted study plan')


def execution_request(root, state, args):
    from .docking_policy import POLICY
    if not POLICY['native_vs_workflow_ready']:
        raise ValueError('CovDock-VS native workflow migration is not yet complete')
    if not available(state) or state['stage'] != 'VS_PLAN_READY':
        raise ValueError('Observe the original Library VS task; do not replace its registration')
    request, frozen = planned_request(root, state)
    if normalize_spec(JobManager(root)._prepare_submission(request)) != frozen:
        raise ValueError('Library VS source, inputs or runtime changed after planning')
    return request.model_copy(update={'execution_mode': 'execute', 'human_approved': args.get('human_approved', False),
                                     'approval_note': args.get('approval_note')})


def start_request(root, args):
    location = workflow().directory(root, args['workflow_id'])
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, args['expected_revision'])
        if args.get('action') != 'start_vs' or set(args) - {
            'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}:
            raise ValueError('Workflow owns the Library VS input and budget')
        return execution_request(root, state, args)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    from .source_release import verify_source
    record = state['vs_execution']
    record['job_identity_verified'] = False
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    verify_source(spec)
    verify_spec(spec)
    scope = decode(decode(spec.provenance['native_approval'])['approval'])['scope']
    if (digest(scope) != record['approved_scope_sha256'] or normalize_spec(spec) != scope['spec']
        or spec.engine != 'schrodinger-covdock-vs' or spec.route != 'library-first'
        or spec.project_id != state['workflow_id'] or spec.request_id != record['request_id']
        or any(getattr(result, key) != getattr(spec, key) for key in
               ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Library VS registered task differs from native approval and study')
    # Revalidates registered prescreen products, current receptor and judgment.
    request, frozen = planned_request(root, state)
    if spec.parameters['vs_preparation'] != request.parameters['vs_preparation']:
        raise ValueError('Library VS runtime or preparation source changed after approval')
    if normalize_spec(spec) != {**frozen, 'execution_mode': 'execute', 'human_approved': True,
                                'approval_note': spec.approval_note}:
        raise ValueError('Library VS task differs from the current frozen runtime plan')
    for name, checksum in spec.input_sha256.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('Library VS registered input changed: ' + name)
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        job_identity_verified=True, output_validation='NOT_EVALUATED', scientific_acceptance=False, md_ready=False)
    record.pop('evidence_error', None)
    state['stage'] = 'VS_EXECUTION_REVIEW' if result.state in TERMINAL else 'VS_EXECUTION_RUNNING'
    return public(state)


def reject(state, error):
    state['vs_execution'].update(job_identity_verified=False, evidence_error=str(error),
        output_validation='EVIDENCE_REJECTED', scientific_acceptance=False, md_ready=False)
    state['vs_execution'].pop('evaluation_summary', None)
    state['vs_execution'].pop('report', None)


def review(root, state, *, evaluate_fresh=False, create_report=False):
    from .library_vs_evidence import evaluate, save_review, load_review, report
    record = state['vs_execution']
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(record['run_id']), manager.registry.load_result(record['run_id'])
    value = (evaluate(root, state, manager, spec, result) if evaluate_fresh or not record.get('evaluation_artifact')
             else load_review(root, state, manager, spec, result))
    record['evaluation_artifact'] = save_review(root, value)
    record['evaluation_summary'] = {'candidate_count': value['candidate_count'],
        'passing_candidate_count': value['passing_candidate_count'],
        'policy': value['policy'],
        'candidates': [{**{key: item[key] for key in ('candidate_id', 'pose_count', 'status', 'review_eligible',
            'evidence_complete', 'passing_pose_count', 'selected_pose_index')},
            'aggregate_pose_failure_reasons': sorted({reason for pose in item['poses'] for reason in pose['assessment']['failure_reasons']}),
            'aggregate_missing_reasons': sorted({reason for pose in item['poses'] for reason in pose['assessment']['missing_reasons']}
                | ({'no_output_poses'} if not item['pose_count'] else set())
                | ({'pose_measurement_error'} if item['errors'] else set())),
            'measurement_error_count': len(item['errors'])} for item in value['candidates']]}
    record['output_validation'] = value['output_validation']
    if create_report or record.get('report'):
        record['report'] = report(root, record['evaluation_artifact'], value)
    return value


def refresh(root, state):
    record = state.get('vs_execution') or {}
    if not record.get('run_id') or state.get('pending'):
        return
    stage = state['stage']
    try:
        observe(root, state, record['run_id'])
        if record.get('evaluation_artifact'):
            review(root, state)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        reject(state, error)
    finally:
        if stage not in STAGES:
            state['stage'] = stage


def reconcile(root, state):
    from .submission_identity import lookup_submission
    pending = state.get('pending') or {}
    if pending.get('action') != 'start_vs':
        raise ValueError('No uncertain Library VS registration to reconcile')
    found = lookup_submission(root, pending['submission'])
    if found.get('registered_job_observed'):
        state['vs_execution']['run_id'] = found['run_id']
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
            raise ValueError('Library VS execution action is unavailable at the current stage')
        if action == 'start_vs':
            if not CURRENT.get():
                raise NativeApprovalDenied('Library VS requires exact native approval')
            if set(args) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns Library VS execution inputs')
            request = execution_request(root, state, args)
            state['vs_execution'] = {'request_id': request.request_id,
                'approved_scope_sha256': digest(decode(CURRENT.get())['scope']),
                'job_identity_verified': False, 'scientific_acceptance': False, 'md_ready': False}
            begin_operation(location, state, tool, action, submission={
                'project_id': request.project_id, 'request_id': request.request_id, 'execution_mode': 'execute'})
            run_id = JobManager(root).submit(request).run_id
        else:
            if args:
                raise ValueError('Observe only the original registered Library VS task')
            run_id = state['vs_execution']['run_id']
        state['vs_execution']['run_id'] = run_id
        try:
            result = observe(root, state, run_id)
            if action in {'evaluate_vs', 'report_vs'} or state['vs_execution'].get('evaluation_artifact'):
                review(root, state, evaluate_fresh=action == 'evaluate_vs', create_report=action == 'report_vs')
                result = public(state)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            reject(state, error)
            state.update(pending=None, stage='VS_EXECUTION_RUNNING')
            changed(location, state, 'library_vs_evidence_rejected')
            raise
        state['pending'] = None
        changed(location, state, action)
        return route.response(state, result)
