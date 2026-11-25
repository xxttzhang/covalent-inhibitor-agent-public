"""Native-approved shared CPU receptor preparation, with one durable job ID."""
import json

from .approval_scope import normalize_spec
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .ligand_receptor_source import context, pre_vs, planning_available, verify_inputs
from .ligand_parent_evidence import version_lock
from .ligand_receptor_input import validate_binding
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load


RUN_STAGES = {'RECEPTOR_PREPARATION_RUNNING', 'RECEPTOR_PREPARATION_REVIEW'}


def execution_request(root, state, arguments):
    from .ligand_receptor_preparation import request_for
    if state.get('pending') or not planning_available(state):
        raise ValueError('Receptor execution requires an idle current-study preparation plan')
    record = context(state).get('receptor_preparation') or {}
    if record.get('run_id') or record.get('runtime_check') != 'CPU_PREPARATION_PLAN_COMPILED':
        raise ValueError('Receptor execution requires its frozen unsubmitted CPU plan')
    verify_inputs(root, state)
    request = request_for(root, state)
    if normalize_spec(JobManager(root)._prepare_submission(request)) != record.get('spec'):
        raise ValueError('Receptor inputs or execution plan changed after freezing')
    return JobSubmissionRequest.model_validate({**request.model_dump(mode='json'),
        'execution_mode': 'execute', 'human_approved': arguments.get('human_approved', False),
        'approval_note': arguments.get('approval_note')})


def start_request(root, arguments):
    from .ligand_workflow import directory
    with version_lock(directory(root, arguments['workflow_id']), '.workflow.lock'):
        state = load(directory(root, arguments['workflow_id']))
        revision(state, arguments['expected_revision'])
        if (arguments.get('action') != 'start_receptor_preparation' or set(arguments) - {
                'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}):
            raise ValueError('Workflow owns the receptor preparation inputs and budget')
        return execution_request(root, state, arguments)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    record = context(state)['receptor_preparation']
    verify_spec(spec)
    scope = record['approved_scope']
    if (spec.engine != 'library-first-receptor-preparation' or spec.route != 'ligand-first'
            or spec.project_id != state['design_id'] or spec.request_id != record['request_id']
            or normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or any(getattr(result, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('Receptor job differs from the exact approved current-study plan')
    # Keep the task identity even if a later source check fails.
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        approval='APPROVED', compute_submitted=True, receptor_prepared=False,
        output_validation='NOT_EVALUATED')
    state['stage'] = ('RECEPTOR_PREPARATION_REVIEW' if result.state in
        {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'} else 'RECEPTOR_PREPARATION_RUNNING')
    for name, checksum in spec.input_sha256.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != checksum:
            raise ValueError('Receptor frozen input changed: ' + name)
    verify_inputs(root, state)
    validate_binding(root, state)
    record.pop('failure_diagnostics', None)
    if result.state in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
        from .ligand_receptor_evidence import failure_diagnostics
        record['failure_diagnostics'] = failure_diagnostics(root, state, manager, spec, result)
    record.pop('evidence_error', None)
    return {'run_id': run_id, 'job_state': result.state, 'return_code': result.return_code,
        'output_validation': 'NOT_EVALUATED', 'receptor_prepared': False, 'md_ready': False,
        'note': 'Registered completion is not prepared-receptor acceptance; quality and environment review are required.',
        **({'failure_diagnostics': record['failure_diagnostics']} if record.get('failure_diagnostics') else {})}


def reject_evidence(state, error):
    record = context(state)['receptor_preparation']
    record.update(evidence_error=str(error), receptor_prepared=False,
                  evaluation_verification='evidence_rejected', output_validation='EVIDENCE_REJECTED')
    record.pop('report', None)
    record.pop('failure_diagnostics', None)


def review(root, state, output, *, report=False, reuse=False):
    from .ligand_receptor_evidence import evaluate, freeze_review
    from .ligand_receptor_report import freeze
    record = context(state)['receptor_preparation']
    manager = JobManager(root)
    evaluation = evaluate(root, state, manager, manager.registry.load_spec(record['run_id']),
                          manager.registry.load_result(record['run_id']), reuse=reuse)
    witness = freeze_review(root, evaluation)
    record.update(evaluation=evaluation, evaluation_artifact=witness,
                  evaluation_verification='verified', receptor_prepared=evaluation['receptor_prepared'],
                  prepared_scope=evaluation['prepared_scope'], output_validation=evaluation['output_validation'])
    record.pop('evidence_error', None)
    record.pop('evidence_error_action', None)
    output.update(evaluation=evaluation, receptor_prepared=evaluation['receptor_prepared'], prepared_scope=evaluation['prepared_scope'],
                  output_validation=evaluation['output_validation'], covdock_ready=False, md_ready=False)
    if report or record.get('report'):
        record['report'] = freeze(root, state)
        output['report'] = record['report']
    return output


def read_projection(root, state):
    """Observe the registered task in memory, without publishing review artifacts."""
    record = context(state).get('receptor_preparation') or {}
    if not record.get('run_id') or state.get('pending'):
        return None
    evaluation_error = record.get('evidence_error') if record.get('evidence_error_action') in {'evaluate_receptor', 'report_receptor'} else None
    output = observe(root, state, record['run_id'])
    if evaluation_error:
        reject_evidence(state, evaluation_error)
        output.update(output_validation='EVIDENCE_REJECTED', evidence_error=evaluation_error)
    elif record.get('evaluation'):
        from .ligand_receptor_evidence import verify_saved_readonly
        evaluation = verify_saved_readonly(root, state)
        record.update(evaluation_verification='verified',
            receptor_prepared=evaluation['receptor_prepared'],
            prepared_scope=evaluation['prepared_scope'],
            output_validation=evaluation['output_validation'])
        output.update(evaluation=evaluation, receptor_prepared=evaluation['receptor_prepared'],
            prepared_scope=evaluation['prepared_scope'], output_validation=evaluation['output_validation'])
    output.update(observation_scope='live_registered_task_read_only',
                  compute_submitted_by_observation=False)
    return output


def revalidate_saved(root, state):
    record = context(state).get('receptor_preparation') or {}
    returned_to_vs = pre_vs(state) and context(state).get('use_for_vs')
    if ((not record.get('evaluation') and record.get('job_state') not in {'FAILED', 'CANCELLED', 'TIMED_OUT'})
            or not record.get('run_id') or state.get('pending')
            or state['stage'] not in RUN_STAGES and not returned_to_vs):
        return None
    stage = state['stage']
    try:
        output = observe(root, state, record['run_id'])
        if record.get('evaluation'):
            review(root, state, output, reuse=True)
    except (ValueError, KeyError, TypeError, OSError) as error:
        reject_evidence(state, error)
        return str(error)
    finally:
        if returned_to_vs:
            state['stage'] = stage
    return None


def lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response, DESIGN
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    from .submission_identity import lookup_submission
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        before = json.dumps(state, sort_keys=True)
        if action == 'start_receptor_preparation':
            if not CURRENT.get():
                raise NativeApprovalDenied('Receptor preparation requires exact native approval')
            if set(arguments) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns the receptor execution inputs')
            request = execution_request(root, state, arguments)
            record = context(state)['receptor_preparation']
            record['approved_scope'] = decode(CURRENT.get())['scope']
            begin_operation(location, state, DESIGN, action, submission={
                'project_id': request.project_id, 'request_id': request.request_id, 'execution_mode': 'execute'})
            run_id = JobManager(root).submit(request).run_id
        elif action == 'reconcile':
            pending = state.get('pending') or {}
            if arguments or pending.get('action') != 'start_receptor_preparation':
                raise ValueError('No uncertain receptor submission to reconcile')
            output = lookup_submission(root, pending['submission'])
            if not output.get('registered_job_observed'):
                return response(state, output, compact=pre_vs(state))
            run_id = output['run_id']
        elif action in {'receptor_preparation_status', 'refresh_receptor_preparation', 'evaluate_receptor', 'report_receptor'}:
            if arguments or state.get('pending') or state['stage'] not in RUN_STAGES:
                raise ValueError('Observe only the current registered receptor task')
            run_id = context(state)['receptor_preparation']['run_id']
        else:
            raise ValueError('Unknown receptor execution action')
        record = context(state)['receptor_preparation']
        try:
            if action in {'receptor_preparation_status', 'refresh_receptor_preparation'}:
                output = read_projection(root, state)
                output['refresh_required_to_persist_observation'] = before != json.dumps(state, sort_keys=True)
                if action == 'refresh_receptor_preparation':
                    if before != json.dumps(state, sort_keys=True):
                        changed(location, state, action)
                    output['refresh_required_to_persist_observation'] = False
                    output['observation_scope'] = 'explicit_registered_task_refresh'
                return response(state, output, compact=pre_vs(state))
            output = observe(root, state, run_id)
            if action in {'evaluate_receptor', 'report_receptor'} or record.get('evaluation'):
                output = review(root, state, output, report=action == 'report_receptor',
                                reuse=action != 'evaluate_receptor')
        except (ValueError, KeyError, TypeError, OSError) as error:
            if action == 'receptor_preparation_status':
                raise
            reject_evidence(state, error)
            record['evidence_error_action'] = action
            if record.get('run_id') == run_id:
                state['pending'] = None
            if before != json.dumps(state, sort_keys=True):
                changed(location, state, 'receptor_evidence_rejected')
            raise
        state['pending'] = None
        changed(location, state, action)
        return response(state, output, compact=pre_vs(state))


def motion_review_lifecycle(root, action, *, workflow_id, expected_revision, **arguments):
    from .ligand_workflow import directory, response
    from .receptor_environment_review import read_evidence, freeze, load as load_review
    location = directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        record = context(state).get('receptor_preparation') or {}
        if (state.get('pending') or state['stage'] != 'RECEPTOR_PREPARATION_REVIEW'
                or not record.get('run_id')
                or (record.get('spec') or {}).get('parameters', {}).get('relaxation_policy_version') not in {'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'}):
            raise ValueError('Motion review requires the current completed v2 receptor proposal')
        allowed = ({'evidence_path', 'evidence_locator', 'evidence_offset'}
            if action == 'read_receptor_motion' else {'motion_review'})
        if set(arguments) - allowed or action not in {'read_receptor_motion', 'review_receptor_motion'}:
            raise ValueError('Unsupported receptor motion review arguments')
        if action == 'read_receptor_motion':
            read_projection(root, state)
            if not record.get('evaluation_artifact'):
                raise ValueError('Evaluate the receptor explicitly before reading its motion evidence')
        else:
            observe(root, state, record['run_id'])
            review(root, state, {})
        witness = record.get('proposal_evaluation_artifact') or record['evaluation_artifact']
        inventory = {witness['path']: witness['manifest_sha256']}
        bound = {'workflow_id': workflow_id, 'run_id': record['run_id'],
            'evaluation_sha256': witness['manifest_sha256']}
        if action == 'read_receptor_motion':
            output = read_evidence(root, inventory, **arguments)
            return response(state, output, compact=pre_vs(state))
        else:
            ref = freeze(root, bound, inventory, arguments.get('motion_review'), scope='restrained-receptor')
            if record.get('motion_review') and record['motion_review'] != ref:
                record.setdefault('motion_review_history', []).append(record['motion_review'])
            record['motion_review'] = ref
            # Recompute delivery against the new judgment before persisting state.
            # A negative revision must withdraw readiness in this same action.
            review(root, state, {})
            output = {'motion_review': ref, 'assessment': load_review(root, ref, bound, inventory,
                scope='restrained-receptor'), 'receptor_prepared': record['receptor_prepared'],
                'compute_submitted': False}
        changed(location, state, action)
        return response(state, output, compact=pre_vs(state))
