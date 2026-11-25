"""Native-approved CovDock lifecycle over the shared compiler and job ledger."""
import json
from .approval_scope import normalize_spec
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .ligand_covdock_input import request_for
from .ligand_parent_evidence import version_lock
from .workflow_protocol import begin_operation, changed, revision
from .workflow_store import load


RUN_STAGES = {'COVDOCK_EXECUTION_RUNNING', 'COVDOCK_EXECUTION_REVIEW'}
READY = 'REGISTERED_COVDOCK_PLAN_COMPILED'


def failure_diagnostics(root, manager, spec, result):
    """Expose bounded, registered producer logs, never structural acceptance."""
    from .md_preparation_evidence import registered_output
    folder = manager.registry.job_dir(spec.run_id)
    names = ('artifacts/covdock_lo/library_first_core_subset_covdock_lo.log',
             'artifacts/covdock_lo/library_first_core_subset_covdock_lo.stderr.log',
             'stderr.log')
    output = {'failure_reason': result.failure_reason, 'sources': [],
        'scientific_result_verified': False,
        'recovery': 'Diagnose the recorded error before reconsidering. Preserve this failed run; '
                    'do not automatically repeat unchanged computation. A new run needs separate native approval.'}
    for name in names:
        relative = str((folder / name).relative_to(root))
        if not any(item.path == relative for item in result.artifacts):
            continue
        try:
            path = registered_output(root, folder, spec, result, name)
            with path.open('rb') as stream:
                stream.seek(max(0, path.stat().st_size - 4096))
                tail = stream.read(4096).decode('utf-8', errors='replace')
            output['sources'].append({'path': relative, 'sha256': sha256_file(path),
                'tail': tail, 'truncated': path.stat().st_size > 4096})
        except ValueError:
            output['sources'].append({'path': relative, 'unavailable_reason':
                'Registered diagnostic artifact is missing or fails integrity verification.'})
    if not output['sources']:
        output['unavailable_reason'] = 'No registered failure logs; exit code alone does not establish the cause.'
    if getattr(result, 'return_code', None) == 4:
        name = 'artifacts/covdock_lo_registered_manifest.json'
        try:
            path = registered_output(root, folder, spec, result, name)
            if path.stat().st_size > 65536:
                raise ValueError('Registered summary exceeds diagnostic budget')
            manifest = json.loads(path.read_text())
            quality = manifest.get('gate_quality', {})
            output['producer_summary'] = {
                'path': str(path.relative_to(root)), 'sha256': sha256_file(path),
                'independently_evaluated': False,
                'counts': {key: quality.get(key) for key in
                    ('input_identity_count', 'covdock_success_count', 'final_candidate_count')
                    if isinstance(quality.get(key), int) and not isinstance(quality.get(key), bool)}}
            output['recovery'] = ('The registered LO wrapper uses exit 4 when batch gates are not met. '
                'Registered outputs exist but are not yet independently verified. Call evaluate_covdock '
                'on this original run, then report_covdock if readback succeeds, including zero-selected '
                'or evidence-insufficient results. Do not infer absent poses from exit 4 or rerun computation '
                'merely to obtain exit 0; do not relax gates.')
        except (ValueError, OSError, AttributeError):
            output['quality_summary_unavailable'] = True
    return output


def validate_request(root, request):
    """A raw current-workflow request cannot replace its frozen scientific inputs."""
    from .ligand_workflow import directory
    state = load(directory(root, request.parameters['workflow_id']))
    expected = request_for(root, state)
    fields = ('request_id', 'project_id', 'route', 'engine', 'parameters',
              'resource_budget', 'parent_run_id', 'attempt')
    if any(getattr(request, key) != getattr(expected, key) for key in fields):
        raise ValueError('CovDock request differs from its current bound workflow input and budget')
    if request.execution_mode == 'execute':
        if not expected.parameters['environment_eligible_for_exploratory_refinement']:
            raise ValueError('CovDock execution requires a source-bound positive environment assessment')
        plan = state['refinement_input'].get('covdock_plan') or {}
        if (state['stage'] != 'RECEPTOR_PREPARATION_REVIEW'
                or (state.get('covdock_execution') or {}).get('run_id')
                or plan.get('runtime_check') != READY
                or plan.get('request_id') != request.request_id
                or state.get('pending') and state['pending'].get('action') != 'start_covdock'):
            raise ValueError('CovDock execution requires the unsubmitted frozen workflow plan')


def execution_request(root, state, arguments):
    if state.get('pending') or state['stage'] != 'RECEPTOR_PREPARATION_REVIEW':
        raise ValueError('CovDock execution requires an idle frozen plan')
    if state.get('covdock_execution'):
        raise ValueError('Observe the original CovDock submission; do not replace it')
    plan = state['refinement_input'].get('covdock_plan') or {}
    if plan.get('runtime_check') != READY:
        raise ValueError('CovDock execution requires runtime readiness and a positive environment assessment')
    request = request_for(root, state)
    if (not request.parameters['environment_eligible_for_exploratory_refinement']
            or normalize_spec(JobManager(root)._prepare_submission(request)) != plan.get('spec')):
        raise ValueError('CovDock inputs, environment assessment or execution plan changed after freezing')
    return JobSubmissionRequest.model_validate({**request.model_dump(mode='json'),
        'execution_mode': 'execute', 'human_approved': arguments.get('human_approved', False),
        'approval_note': arguments.get('approval_note')})


def start_request(root, arguments):
    from .ligand_workflow import directory
    location = directory(root, arguments['workflow_id'])
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, arguments['expected_revision'])
        if (arguments.get('action') != 'start_covdock' or set(arguments) - {
                'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}):
            raise ValueError('Workflow owns the CovDock inputs and budget')
        return execution_request(root, state, arguments)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    record = state['covdock_execution']
    verify_spec(spec)
    scope = record['approved_scope']
    if (spec.engine != 'schrodinger-covdock-lo' or spec.route != 'ligand-first'
            or spec.project_id != state['design_id'] or spec.request_id != record['request_id']
            or normalize_spec(spec) != scope['spec']
            or decode(decode(spec.provenance['native_approval'])['approval'])['scope'] != scope
            or any(getattr(result, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('CovDock job differs from the exact approved current-study plan')
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        approval='APPROVED', compute_submitted=True, output_validation='NOT_EVALUATED',
        scientific_result_verified=False)
    state['stage'] = ('COVDOCK_EXECUTION_REVIEW' if result.state in
        {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'} else 'COVDOCK_EXECUTION_RUNNING')
    for name, checksum in spec.input_sha256.items():
        path = root / name
        if path.is_symlink() or not path.is_file() or sha256_file(path) != checksum:
            raise ValueError('CovDock frozen input changed: ' + name)
    expected = request_for(root, state)
    if (expected.request_id != spec.request_id or any(
            spec.parameters.get(key) != value for key, value in expected.parameters.items())
            or expected.resource_budget != spec.resource_budget):
        raise ValueError('CovDock approved source binding differs from current evidence')
    record.pop('evidence_error', None)
    diagnostics = {}
    if result.state in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
        diagnostics = {'failure_diagnostics': failure_diagnostics(root, manager, spec, result)}
        record.update(failure_reason=result.failure_reason, **diagnostics)
    return {'run_id': run_id, 'job_state': result.state, 'return_code': result.return_code,
        **diagnostics,
        **({'execution_instance': spec.parameters['execution_instance']}
           if spec.parameters.get('execution_instance') else {}),
        'output_validation': 'NOT_EVALUATED', 'scientific_result_verified': False,
        'md_ready': False, 'note': 'Task status is not structural acceptance or inhibition evidence.'}


def review(root, state, output, *, report=False, reuse=False):
    from .ligand_covdock_evidence import evaluate_outputs, witness
    from .ligand_covdock_report import freeze
    record = state['covdock_execution']
    manager = JobManager(root)
    value = evaluate_outputs(root, state, manager, manager.registry.load_spec(record['run_id']),
                             manager.registry.load_result(record['run_id']), reuse=reuse)
    record.update(evaluation=value, evaluation_artifact=witness(root, value), evaluation_verification='verified',
                  output_validation=value['output_validation'])
    record.pop('readback_wait', None)
    record.pop('evidence_error', None)
    output.update(evaluation=value, output_validation=value['output_validation'])
    if report or record.get('report'):
        record['report'] = freeze(root, state)
        output['report'] = record['report']
    if record.get('md_handoff'):
        from .ligand_md_handoff import prepare
        try:
            record['md_handoff'] = prepare(root, state)
            record.pop('md_handoff_error', None)
            output['md_handoff'] = record['md_handoff']
        except (ValueError, KeyError, TypeError, OSError) as error:
            record.pop('md_handoff', None)
            record['md_handoff_error'] = str(error)
            output['md_handoff_error'] = str(error)
    from .ligand_md_selection import refresh
    refresh(root, state)
    return output


def reject_evidence(state, error):
    from .covdock_readback import CovDockReadbackUnavailable
    record = state['covdock_execution']
    waiting = isinstance(error, CovDockReadbackUnavailable)
    record.pop('evidence_error' if waiting else 'readback_wait', None)
    record.update(evaluation_verification='verification_unavailable' if waiting else 'evidence_rejected',
        scientific_result_verified=False, output_validation='READBACK_RUNTIME_REQUIRED' if waiting else 'EVIDENCE_REJECTED')
    record['readback_wait' if waiting else 'evidence_error'] = str(error)
    record.pop('report', None)
    record.pop('md_handoff', None)
    if record.get('md_selection'):
        record['md_selection_verification'] = 'evidence_rejected'
        record['md_selection_error'] = 'Current CovDock source could not be verified: ' + str(error)
        if record.get('md_graph_plan'):
            record['md_graph_plan_error'] = 'Current CovDock source could not be verified'


def revalidate_saved(root, state):
    record = state.get('covdock_execution') or {}
    if state.get('pending') or not record.get('run_id'):
        return None
    try:
        output = observe(root, state, record['run_id'])
        if record.get('evaluation'):
            review(root, state, output, reuse=True)
    except (ValueError, KeyError, TypeError, OSError) as error:
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
        if action == 'start_covdock':
            if not CURRENT.get():
                raise NativeApprovalDenied('CovDock requires exact native approval')
            if set(arguments) - {'human_approved', 'approval_note'}:
                raise ValueError('Workflow owns the CovDock execution inputs')
            request = execution_request(root, state, arguments)
            state['covdock_execution'] = {'request_id': request.request_id,
                'approved_scope': decode(CURRENT.get())['scope']}
            begin_operation(location, state, DESIGN, action, submission={
                'project_id': request.project_id, 'request_id': request.request_id, 'execution_mode': 'execute'})
            run_id = JobManager(root).submit(request).run_id
        elif action == 'reconcile':
            pending = state.get('pending') or {}
            if arguments or pending.get('action') != 'start_covdock':
                raise ValueError('No uncertain CovDock submission to reconcile')
            output = lookup_submission(root, pending['submission'])
            if not output.get('registered_job_observed'):
                return response(state, output)
            run_id = output['run_id']
        elif action in {'covdock_status', 'evaluate_covdock', 'report_covdock', 'prepare_md_handoff'}:
            if arguments or state.get('pending') or state['stage'] not in RUN_STAGES:
                raise ValueError('Observe only the current registered CovDock task')
            run_id = state['covdock_execution']['run_id']
        else:
            raise ValueError('Unknown CovDock execution action')
        record = state['covdock_execution']
        # Retain a returned/registered identity even if its signed evidence is invalid.
        record.setdefault('run_id', run_id)
        try:
            output = observe(root, state, run_id)
            if action in {'evaluate_covdock', 'report_covdock', 'prepare_md_handoff'} or record.get('evaluation'):
                output = review(root, state, output, report=action == 'report_covdock', reuse=action != 'evaluate_covdock')
        except (ValueError, KeyError, TypeError, OSError) as error:
            reject_evidence(state, error)
            state['pending'] = None
            if state['stage'] not in RUN_STAGES:
                # A registration was found, but no authoritative terminal
                # state was verified. Keep replacement/reconsideration closed.
                state['stage'] = 'COVDOCK_EXECUTION_RUNNING'
            changed(location, state, 'covdock_evidence_rejected')
            raise
        if action == 'prepare_md_handoff':
            from .ligand_md_handoff import prepare
            try:
                record['md_handoff'] = prepare(root, state)
                record.pop('md_handoff_error', None)
                output['md_handoff'] = record['md_handoff']
            except (ValueError, KeyError, TypeError, OSError) as error:
                record.pop('md_handoff', None)
                record['md_handoff_error'] = str(error)
                changed(location, state, 'md_handoff_evidence_rejected')
                raise
        state['pending'] = None
        changed(location, state, action)
        return response(state, output)
