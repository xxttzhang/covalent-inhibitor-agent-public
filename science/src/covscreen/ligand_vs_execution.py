"""Native VS approval and same-job recovery, independent of historical poses.

Registration is not HPC submission or scientific acceptance. The release policy
keeps execution closed until dispatch and independent readback are integrated.
"""
import hashlib
import json

from .approval_scope import normalize_spec
from .covdock_vs import bound_file
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .ligand_parent_evidence import version_lock
from .ligand_vs_input import available, environment_review, load as load_input, request_for
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load


ACTIONS = {'start_vs', 'vs_status', 'evaluate_vs', 'report_vs'}
STAGES = {'VS_EXECUTION_RUNNING', 'VS_EXECUTION_REVIEW'}
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def planned_request(root, state):
    plan = state.get('vs_plan') or {}
    if not plan or (state.get('vs_runtime') or {}).get('state') != 'VS_PLAN_COMPILED':
        raise ValueError('VS execution requires a frozen runtime plan and positive environment assessment')
    payload = load_input(root, state)
    reviewed = environment_review(root, state, payload)
    if not reviewed or reviewed['decision'] != 'suitable_for_exploratory_vs':
        raise ValueError('VS execution requires a current source-bound positive environment assessment')
    manager = JobManager(root)
    request = request_for(root, state, manager.config.get('schrodinger', {}), source_revision=plan['source_revision'])
    path = bound_file(root, plan['path'])
    if sha256_file(path) != plan['sha256']:
        raise ValueError('VS frozen private plan changed')
    return request, json.loads(path.read_text())


def validate_request(root, request):
    """An execute request must come from the current native study, not a raw packet."""
    from .ligand_workflow import directory
    workflow = request.parameters['vs_preparation']['plan']['source']['workflow_id']
    state = load(directory(root, workflow))
    expected, _ = planned_request(root, state)
    fields = ('request_id', 'project_id', 'route', 'engine', 'parameters', 'resource_budget',
              'parent_run_id', 'attempt')
    if any(getattr(request, field) != getattr(expected, field) for field in fields):
        raise ValueError('VS request differs from the current frozen study and budget')
    if (state['stage'] not in {'NEEDS_POSE_EVALUATION', 'POSE_PLAN_READY', 'POSE_INPUTS_BLOCKED',
                              'POSE_EXECUTION_REVIEW', 'BRANCH_REPORTED'}
            or (state.get('vs_execution') or {}).get('run_id')
            or state.get('pending') and state['pending'].get('action') != 'start_vs'):
        raise ValueError('VS execution requires the original unsubmitted workflow plan')


def execution_request(root, state, arguments):
    from .docking_policy import POLICY
    if not POLICY['native_vs_workflow_ready']:
        raise ValueError('CovDock-VS native workflow migration is not yet complete')
    if not available(state) or state.get('vs_execution'):
        raise ValueError('Observe the original VS task; do not replace a pending or registered submission')
    request, frozen = planned_request(root, state)
    if normalize_spec(JobManager(root)._prepare_submission(request)) != frozen:
        raise ValueError('VS inputs, runtime or source changed after the plan was frozen')
    return JobSubmissionRequest.model_validate({**request.model_dump(mode='json'), 'execution_mode':'execute',
        'human_approved':arguments.get('human_approved', False), 'approval_note':arguments.get('approval_note')})


def start_request(root, arguments):
    from .ligand_workflow import directory
    location = directory(root, arguments['workflow_id'])
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, arguments['expected_revision'])
        if arguments.get('action') != 'start_vs' or set(arguments) - {
                'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}:
            raise ValueError('Workflow owns the VS input and budget')
        return execution_request(root, state, arguments)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    record = state['vs_execution']
    record['job_identity_verified'] = False
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    verify_spec(spec)
    scope = decode(decode(spec.provenance['native_approval'])['approval'])['scope']
    if (digest(scope) != record['approved_scope_sha256'] or normalize_spec(spec) != scope['spec']
            or spec.engine != 'schrodinger-covdock-vs' or spec.route != 'ligand-first'
            or spec.project_id != state['workflow_id'] or spec.request_id != record['request_id']
            or any(getattr(result, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('VS registered task differs from the native-approved study plan')
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        job_identity_verified=True, compute_registered=True, output_validation='NOT_EVALUATED',
        scientific_acceptance=False, md_ready=False)
    state['stage'] = 'VS_EXECUTION_REVIEW' if result.state in TERMINAL else 'VS_EXECUTION_RUNNING'
    for name, checksum in spec.input_sha256.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('VS registered input changed: ' + name)
    # Study evidence and source-bound judgment must remain valid after recovery.
    payload = load_input(root, state)
    reviewed = environment_review(root, state, payload)
    if not reviewed or reviewed['decision'] != 'suitable_for_exploratory_vs':
        raise ValueError('VS environment assessment cannot be revalidated')
    record.pop('evidence_error', None)
    return {**record, 'note':'Registered task status is not accepted structure evidence or MD admission.'}


def review(root, state, *, evaluate_fresh=False, create_report=False):
    from .ligand_vs_evidence import evaluate, save_review, load_review, report
    record = state['vs_execution']
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(record['run_id']), manager.registry.load_result(record['run_id'])
    value = (evaluate(root,state,manager,spec,result) if evaluate_fresh or not record.get('evaluation_artifact')
             else load_review(root,state,manager,spec,result))
    record['evaluation_artifact'] = save_review(root,state,value)
    record['evaluation_summary'] = {'candidate_count':value['candidate_count'],
        'passing_candidate_count':value['passing_candidate_count'],
        'candidates':[{key:item[key] for key in ('candidate_id','pose_count','status','review_eligible',
            'evidence_complete','passing_pose_count','selected_pose_index')} for item in value['candidates']]}
    record['output_validation'] = value['output_validation']
    if create_report or record.get('report'):
        record['report'] = report(root,record['evaluation_artifact'],value)
    return value


def reject(state, error):
    record = state['vs_execution']
    record.update(evidence_error=str(error), scientific_acceptance=False, md_ready=False,
                  output_validation='EVIDENCE_REJECTED')
    record.pop('evaluation_summary',None)
    record.pop('report',None)


def revalidate_saved(root, state):
    record = state.get('vs_execution') or {}
    if not record.get('run_id') or state.get('pending'):
        return
    stage = state['stage']
    try:
        observe(root,state,record['run_id'])
        if record.get('evaluation_artifact'):
            review(root,state)
    except (ValueError,KeyError,TypeError,OSError,RuntimeError) as error:
        reject(state,error)
        return str(error)
    finally:
        # Upstream revalidation cannot rewind an active downstream MD stage.
        if stage not in STAGES:
            state['stage'] = stage


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response, DESIGN
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    from .submission_identity import lookup_submission
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if action == 'start_vs':
            if not CURRENT.get():
                raise NativeApprovalDenied('VS requires exact native approval')
            if set(arguments) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns the VS execution inputs')
            request = execution_request(root, state, arguments)
            # The proof itself contains private runtime paths. Persist only its
            # scope digest in model-visible state; the signed registry owns it.
            state['vs_execution'] = {'request_id':request.request_id,
                'approved_scope_sha256':digest(decode(CURRENT.get())['scope']),
                'job_identity_verified':False, 'scientific_acceptance':False, 'md_ready':False}
            begin_operation(location, state, DESIGN, action, submission={
                'project_id':request.project_id, 'request_id':request.request_id, 'execution_mode':'execute'})
            run_id = JobManager(root).submit(request).run_id
        elif action == 'reconcile':
            pending = state.get('pending') or {}
            if arguments or pending.get('action') != 'start_vs':
                raise ValueError('No uncertain VS registration to reconcile')
            output = lookup_submission(root, pending['submission'])
            if not output.get('registered_job_observed'):
                return response(state, output)
            run_id = output['run_id']
        elif action in {'vs_status','evaluate_vs','report_vs'}:
            if arguments or state.get('pending') or state['stage'] not in STAGES:
                raise ValueError('Observe only the original registered VS task')
            run_id = state['vs_execution']['run_id']
        else:
            raise ValueError('Unknown VS execution action')
        record = state['vs_execution']
        record.setdefault('run_id', run_id)
        try:
            output = observe(root, state, run_id)
            if action in {'evaluate_vs','report_vs'} or record.get('evaluation_artifact'):
                review(root,state,evaluate_fresh=action == 'evaluate_vs',create_report=action == 'report_vs')
                output.update(record)
        except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
            reject(state,error)
            if state['stage'] not in STAGES:
                state['stage'] = 'VS_EXECUTION_RUNNING'
            state['pending'] = None
            changed(location, state, 'vs_evidence_rejected')
            raise
        state['pending'] = None
        changed(location, state, action)
        return response(state, output)
