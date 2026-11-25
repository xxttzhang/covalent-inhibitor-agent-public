"""Current-study AF3 lifecycle over the existing executor and native approval.

AF3 predicts the bound construct from its sequence; the separate CovDock
receptor-preparation obligation is retained. Job completion is not acceptance.
"""
from .af3_target import AF3RuntimeUnavailable
from .approval_scope import normalize_spec
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .ligand_parent_evidence import version_lock
from .ligand_refinement_input import load_refinement_input
from .ligand_refinement_target import load as load_target
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load


ACTIONS = {'check_af3_runtime', 'start_af3', 'af3_status', 'evaluate_af3', 'report_af3'}
PLAN_STAGES = {'POSE_EXECUTION_REVIEW', 'VS_REFINEMENT_READY', 'AF3_RUNTIME_REQUIRED', 'AF3_PLAN_READY'}
RUN_STAGES = {'AF3_EXECUTION_RUNNING', 'AF3_EXECUTION_REVIEW'}


def verify_inputs(root, state):
    if (state.get('refinement_input') or {}).get('source_kind') == 'registered-ligand-vs-review-v1':
        return load_refinement_input(root, state), load_target(root, state)
    from .ligand_pose_execution import observe
    from .ligand_pose_evidence import evaluate_outputs
    record = state.get('pose_execution') or {}
    if record.get('evaluation_verification') != 'verified' or not record.get('run_id'):
        raise ValueError('AF3 requires the current verified pose review')
    stage = state['stage']
    try:
        observe(root, state, record['run_id'])
    finally:
        state['stage'] = stage
    manager = JobManager(root)
    fresh = evaluate_outputs(root, state, manager, manager.registry.load_spec(record['run_id']),
                             manager.registry.load_result(record['run_id']))
    if fresh != record.get('evaluation'):
        raise ValueError('AF3 source pose review changed; refresh the candidate handoff')
    candidates = load_refinement_input(root, state)
    target = load_target(root, state)
    if not 1 <= candidates['input_count'] <= 3:
        raise ValueError('AF3 pilot requires one to three frozen candidates')
    return candidates, target


def request_for(root, state, request_id):
    candidates, target = verify_inputs(root, state)
    handoff = state['refinement_input']
    directory = root / handoff['target']['directory']
    relative = lambda path: str(path.relative_to(root))
    lineage = sorted(set([*candidates['source_manifests'],
        relative((root / handoff['candidate_table']).parent / 'manifest.json'),
        relative(directory / 'manifest.json'), relative(directory / 'target-input.json'),
        *[relative(directory / name) for name in target['files']]]))
    return JobSubmissionRequest(request_id=request_id, project_id=state['design_id'],
        route='ligand-first', engine='alphafold3-mpae', execution_mode='plan',
        parameters={
            'candidate_table': handoff['candidate_table'],
            'source_data_json': relative(directory / 'af3-source.json'),
            'target_config': relative(directory / 'af3-target.json'),
            'expected_input_count': candidates['input_count'],
            'expected_input_sha256': candidates['input_sha256'],
            'expected_identity_sha256': candidates['identity_sha256'],
            'refinement_lineage_files': lineage,
            'source_pose_run_id': candidates['source_run_id'],
            'workflow_id': state['workflow_id'],
            'top_n': candidates['input_count'], 'num_seeds': 1,
            'num_recycles': 3, 'num_diffusion_samples': 1, 'cpu_per_search': 2,
            # Recovery observes this registered job. It must not import old
            # engine outputs into a newly approved refinement batch.
            'seed': 20260725, 'run_data_pipeline': True, 'resume': False,
            'dispatch_policy': 'ligand-smoke-v1', 'dispatch_batch': 'ligand-smoke-' + state['design_id']},
        resource_budget={'max_runtime_minutes': 90, 'gpu_count': 1, 'cpu_count': 20,
                         'memory_gb': 200, 'max_candidates': candidates['input_count']})


def freeze_runtime(root, state):
    if state.get('pending') or state['stage'] not in PLAN_STAGES:
        raise ValueError('AF3 planning requires the current idle refinement handoff')
    previous = state.get('af3_execution') or {}
    if previous.get('run_id'):
        raise ValueError('Observe the existing AF3 task; do not create a replacement')
    request_id = previous.get('request_id') or state['workflow_id'] + '-af3-r' + str(state['revision'] + 1)
    request = request_for(root, state, request_id)
    try:
        spec = JobManager(root)._prepare_submission(request)
    except AF3RuntimeUnavailable as error:
        state['af3_execution'] = {**previous, 'request_id': request_id,
            'runtime_check': 'WAITING_RUNTIME_CONFIGURATION', 'runtime_error': str(error)}
        state['stage'] = 'AF3_RUNTIME_REQUIRED'
        return {'runtime_check': 'WAITING_RUNTIME_CONFIGURATION', 'reason': str(error),
                'compute_submitted': False, 'gpu_queue_observed': False}
    normalized = normalize_spec(spec)
    if previous.get('spec') is not None and previous['spec'] != normalized:
        raise ValueError('Frozen AF3 plan changed; reconsider before a new plan and approval')
    state['af3_execution'] = {'request_id': request_id, 'spec': normalized,
        'runtime_check': 'REGISTERED_PLAN_COMPILED',
        'target_artifact_id': state['refinement_input']['target']['artifact_id'],
        'candidate_artifact_id': state['refinement_input']['artifact_id'],
        'output_validation': 'NOT_EVALUATED', 'scientific_result_verified': False}
    state['stage'] = 'AF3_PLAN_READY'
    return {'runtime_check': 'REGISTERED_PLAN_COMPILED', 'compiled_spec': normalized,
        'compute_submitted': False, 'approval': 'NOT_REQUESTED', 'gpu_queue_observed': False,
        'limitations': ['Runtime files were found; this does not prove GPU availability or model execution.',
                        'The sequence seed requires the AF3 data pipeline, enabled in this plan.',
                        'CovDock receptor preparation and MD admission remain separate.']}


def execution_request(root, state, arguments):
    if state.get('pending') or state['stage'] != 'AF3_PLAN_READY':
        raise ValueError('AF3 execution requires its idle frozen plan')
    record = state.get('af3_execution') or {}
    if record.get('run_id') or record.get('runtime_check') != 'REGISTERED_PLAN_COMPILED':
        raise ValueError('AF3 execution requires a compiled, unsubmitted plan')
    request = request_for(root, state, record['request_id'])
    if normalize_spec(JobManager(root)._prepare_submission(request)) != record.get('spec'):
        raise ValueError('AF3 inputs or execution plan changed after freezing')
    return JobSubmissionRequest.model_validate({**request.model_dump(mode='json'),
        'execution_mode': 'execute', 'human_approved': arguments.get('human_approved', False),
        'approval_note': arguments.get('approval_note')})


def start_request(root, arguments):
    from .ligand_workflow import directory
    location = directory(root, arguments['workflow_id'])
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, arguments['expected_revision'])
        if (arguments.get('action') != 'start_af3' or set(arguments) - {
                'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}):
            raise ValueError('Workflow owns the AF3 plan and resource budget')
        return execution_request(root, state, arguments)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    record = state['af3_execution']
    verify_spec(spec)
    scope = record['approved_scope']
    if (spec.engine != 'alphafold3-mpae' or spec.route != 'ligand-first'
            or spec.project_id != state['design_id'] or spec.request_id != record['request_id']
            or normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or any(getattr(result, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('AF3 job differs from the approved current-study plan')
    # Preserve the authoritative task identity even when later input validation
    # fails. A scientific input failure must not enable a second submission.
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
                  output_validation='NOT_EVALUATED', scientific_result_verified=False)
    state['stage'] = ('AF3_EXECUTION_REVIEW' if result.state in
        {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'} else 'AF3_EXECUTION_RUNNING')
    for name, checksum in spec.input_sha256.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != checksum:
            raise ValueError('AF3 frozen input changed: ' + name)
    verify_inputs(root, state)
    record.pop('input_error', None)
    return {'run_id': run_id, 'job_state': result.state, 'return_code': result.return_code,
        'output_validation': 'NOT_EVALUATED', 'scientific_result_verified': False,
        'md_ready': False, 'note': 'Observe this registered task; completion alone is not refinement acceptance.'}


def review(root, state, output, *, report=False):
    from .ligand_af3_evidence import evaluate_outputs
    from .ligand_af3_report import freeze
    record = state['af3_execution']
    record['evaluation_verification'] = 'evidence_rejected'
    manager = JobManager(root)
    evaluation = evaluate_outputs(root, state, manager, manager.registry.load_spec(record['run_id']),
                                  manager.registry.load_result(record['run_id']))
    record.update(evaluation=evaluation, evaluation_verification='verified',
                  output_validation=evaluation['output_validation'])
    record.pop('evidence_error', None)
    output.update(evaluation=evaluation, output_validation=evaluation['output_validation'])
    if report or record.get('report'):
        record['report'] = freeze(root, state)
        output['report'] = record['report']
    return output


def reject_evidence(state, error):
    record = state['af3_execution']
    record.update(evaluation_verification='evidence_rejected', evidence_error=str(error),
                  scientific_result_verified=False, output_validation='EVIDENCE_REJECTED')
    # Retain the old measurement witness for detecting rewritten artifacts,
    # but never expose its old report as currently verified.
    record.pop('report', None)


def revalidate_saved(root, state):
    record = state.get('af3_execution') or {}
    if not record.get('evaluation') or state.get('pending') or state['stage'] not in RUN_STAGES:
        return None
    try:
        output = observe(root, state, record['run_id'])
        review(root, state, output)
    except (ValueError, KeyError, OSError, TypeError) as error:
        reject_evidence(state, error)
        return str(error)
    return None


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response, DESIGN
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    from .submission_identity import lookup_submission
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if action == 'check_af3_runtime':
            if arguments:
                raise ValueError('AF3 planning accepts only the current workflow identity')
            output = freeze_runtime(root, state)
        elif action == 'start_af3':
            if not CURRENT.get():
                raise NativeApprovalDenied('AF3 execution requires exact native approval')
            if set(arguments) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns the AF3 execution inputs')
            request = execution_request(root, state, arguments)
            state['af3_execution']['approved_scope'] = decode(CURRENT.get())['scope']
            begin_operation(location, state, DESIGN, action, submission={
                'project_id': request.project_id, 'request_id': request.request_id, 'execution_mode': 'execute'})
            result = JobManager(root).submit(request)
            output = observe(root, state, result.run_id)
        elif action == 'reconcile':
            pending = state.get('pending') or {}
            if arguments or pending.get('action') != 'start_af3':
                raise ValueError('No uncertain AF3 submission to reconcile')
            output = lookup_submission(root, pending['submission'])
            if not output.get('registered_job_observed'):
                return response(state, output)
            output = observe(root, state, output['run_id'])
        elif action in {'af3_status', 'evaluate_af3', 'report_af3'}:
            if arguments or state.get('pending') or state['stage'] not in RUN_STAGES:
                raise ValueError('Observe only the current registered AF3 task')
            try:
                output = observe(root, state, state['af3_execution']['run_id'])
                if action != 'af3_status' or state['af3_execution'].get('evaluation'):
                    output = review(root, state, output, report=action == 'report_af3')
            except (ValueError, KeyError, OSError, TypeError) as error:
                reject_evidence(state, error)
                changed(location, state, 'af3_evidence_rejected')
                raise
        else:
            raise ValueError('Unknown AF3 workflow action')
        state['pending'] = None
        changed(location, state, action)
        return response(state, output)
