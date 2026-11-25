"""Library native prescreen planning, exact approval and same-task recovery."""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from .approval_scope import normalize_spec
from .covdock_vs import bound_file
from .execution import JobManager
from .jobs import sha256_file
from .library_vs_prescreen_input import freeze, load as load_input, request as input_request, encoded
from .ligand_parent_evidence import version_lock
from .workflow_protocol import begin_operation, changed, revision, archive_branch
from .workflow_store import load

ACTIONS = {'plan_vs_prescreen', 'start_vs_prescreen', 'vs_prescreen_status', 'release_vs_prescreen', 'evaluate_vs_prescreen'}
STAGES = {'VS_PRESCREEN_PLAN_READY', 'VS_PRESCREEN_EXECUTION', 'VS_PRESCREEN_REVIEW'}
TERMINAL = {'COMPLETED', 'FAILED', 'CANCELLED', 'TIMED_OUT'}


def workflow():
    from .layout import enable_library_first
    enable_library_first()
    from covscreen_library_first import workflow
    return workflow


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def public(state):
    record = state.get('vs_prescreen')
    if not record:
        return None
    return {key: deepcopy(value) for key, value in record.items() if key not in {'plan', 'approved_scope_sha256'}}


def actions(state):
    if state.get('pending'):
        return []
    if state['stage'] == 'VS_PRESCREEN_PLAN_READY':
        return ['plan_vs_prescreen', 'start_vs_prescreen', 'release_vs_prescreen']
    if state['stage'] in {'VS_PRESCREEN_EXECUTION', 'VS_PRESCREEN_REVIEW'}:
        return ['vs_prescreen_status'] + (['evaluate_vs_prescreen'] if state['stage'] == 'VS_PRESCREEN_REVIEW' else [])
    if (state['stage'] in {'SCREENING_PLAN', 'SCREENING_PLAN_READY', 'VS_SCREENING_PLAN'}
        and state.get('screening_inputs') and not state.get('screening_handoff') and not state.get('vs_prescreen')):
        return ['plan_vs_prescreen']
    return []


def planned_request(root, state):
    record = state.get('vs_prescreen') or {}
    plan = record.get('plan') or {}
    if not plan:
        raise ValueError('Library VS prescreen requires its frozen native plan')
    source, _ = load_input(root, record['input'])
    if source['workflow_id'] != state['workflow_id'] or source['screening_inputs'] != state['screening_inputs']:
        raise ValueError('VS prescreen no longer belongs to the current study inputs')
    location = bound_file(root, plan['path'])
    if sha256_file(location) != plan['sha256']:
        raise ValueError('VS prescreen private runtime plan changed')
    return input_request(root, record['input'], record['request_id']), json.loads(location.read_text())


def validate_request(root, request):
    from .docking_policy import POLICY
    if not POLICY['native_vs_workflow_ready']:
        raise ValueError('CovDock-VS native workflow migration is not yet complete')
    state = load(workflow().directory(root, request.project_id))
    expected, _ = planned_request(root, state)
    if (state['stage'] != 'VS_PRESCREEN_PLAN_READY' or state['vs_prescreen'].get('run_id')
        or state.get('pending') and state['pending'].get('action') != 'start_vs_prescreen'
        or any(getattr(request, key) != getattr(expected, key) for key in
               ('project_id', 'request_id', 'route', 'engine', 'parameters', 'resource_budget', 'attempt', 'parent_run_id'))):
        raise ValueError('VS prescreen execution differs from the original unsubmitted study plan')


def execution_request(root, state, args):
    request, frozen = planned_request(root, state)
    validate_request(root, request)
    if normalize_spec(JobManager(root)._prepare_submission(request)) != frozen:
        raise ValueError('VS prescreen source or runtime changed after planning; replan before approval')
    return request.model_copy(update={'execution_mode': 'execute', 'human_approved': args.get('human_approved', False),
                                     'approval_note': args.get('approval_note')})


def start_request(root, args):
    location = workflow().directory(root, args['workflow_id'])
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, args['expected_revision'])
        if (args.get('action') != 'start_vs_prescreen' or state.get('pending') or set(args) -
            {'action', 'workflow_id', 'expected_revision', 'human_approved', 'approval_note'}):
            raise ValueError('Workflow owns VS prescreen input and resource scope')
        return execution_request(root, state, args)


def observe(root, state, run_id):
    from .native_approval import verify_spec, decode
    record = state['vs_prescreen']
    record['job_identity_verified'] = False
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    verify_spec(spec)
    scope = decode(decode(spec.provenance['native_approval'])['approval'])['scope']
    if (digest(scope) != record['approved_scope_sha256'] or normalize_spec(spec) != scope['spec']
        or spec.engine != 'covindb-prescreen' or spec.route != 'library-first'
        or spec.project_id != state['workflow_id'] or spec.request_id != record['request_id']
        or spec.parameters.get('vs_prescreen') != record['input']
        or any(getattr(result, key) != getattr(spec, key) for key in
               ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('VS prescreen registered task differs from native approval and study')
    planned_request(root, state)
    for name, checksum in spec.input_sha256.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('VS prescreen registered source changed: ' + name)
    record.update(run_id=run_id, job_state=result.state, return_code=result.return_code,
        job_identity_verified=True, scientific_acceptance=False, md_ready=False, output_validation='NOT_EVALUATED')
    record.pop('evidence_error', None)
    state['stage'] = 'VS_PRESCREEN_REVIEW' if result.state in TERMINAL else 'VS_PRESCREEN_EXECUTION'
    return public(state)


def review(root, state):
    from .library_vs_prescreen_evidence import evaluate, freeze
    record = state['vs_prescreen']
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(record['run_id']), manager.registry.load_result(record['run_id'])
    evidence, mapped = evaluate(root, state, manager, spec, result)
    reference = freeze(root, evidence, mapped)
    record['evaluation_artifact'] = reference
    record['evaluation_summary'] = {key: evidence[key] for key in ('candidate_count', 'candidates', 'counts',
        'quality_gates', 'selection_outcome', 'selection', 'vs_input_eligible')}
    record['output_validation'] = evidence['output_validation']
    return public(state)


def reject(state, error):
    record = state['vs_prescreen']
    record.update(evidence_error=str(error), output_validation='EVIDENCE_REJECTED', scientific_acceptance=False, md_ready=False)
    record.pop('evaluation_summary', None)


def step(root, tool, workflow_id, expected_revision, action, args):
    from .native_approval import CURRENT, NativeApprovalDenied, decode
    route = workflow()
    location = route.directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load(location)
        revision(state, expected_revision)
        if tool != route.RESEARCH or action not in actions(state):
            raise ValueError('VS prescreen action is unavailable at the current stage')
        if action == 'plan_vs_prescreen':
            if set(args) != {'scientific_review'}:
                raise ValueError('VS prescreen requires scientific review; workflow owns inputs and budget')
            reference = freeze(root, state, args['scientific_review'])
            request = input_request(root, reference, workflow_id + '-prescreen-' + uuid4().hex[:12])
            frozen = normalize_spec(JobManager(root)._prepare_submission(request))
            raw = encoded(frozen)
            name = 'prescreen-plan-' + hashlib.sha256(raw).hexdigest() + '.json'
            path = location / name
            with path.open('xb') as stream:
                path.chmod(0o600)
                stream.write(raw)
            if state.get('vs_prescreen'):
                state.setdefault('vs_prescreen_history', []).append(state['vs_prescreen'])
            archive_branch(state, 'Superseded by a separately approved native VS prescreen plan',
                retain=route.ASSETS, invalidate=('screening_plan',))
            state['vs_prescreen'] = {'input': reference, 'request_id': request.request_id,
                'plan': {'path': str(path.relative_to(root)), 'sha256': sha256_file(path)},
                'resource_budget': request.resource_budget.model_dump(mode='json'), 'source_records': 20000,
                'maximum_shortlist': 5, 'job_state': 'PLANNED', 'run_id': None,
                'job_identity_verified': False, 'scientific_acceptance': False, 'md_ready': False}
            state['stage'] = 'VS_PRESCREEN_PLAN_READY'
            result = {**public(state), 'calculation_submitted': False, 'approval_required': True}
        elif action == 'release_vs_prescreen':
            if set(args) != {'reason'} or not isinstance(args['reason'], str) or not 5 <= len(args['reason'].strip()) <= 2000:
                raise ValueError('Release requires an explicit reason')
            if state['vs_prescreen'].get('run_id') or state['vs_prescreen'].get('approved_scope_sha256'):
                raise ValueError('Registered or approved VS prescreen cannot be released')
            state.setdefault('vs_prescreen_history', []).append({**state['vs_prescreen'], 'release_reason': args['reason'].strip()})
            state.update(vs_prescreen=None, stage='VS_SCREENING_PLAN')
            result = {'status': 'VS_PRESCREEN_PLAN_RELEASED', 'calculation_submitted': False}
        else:
            if action == 'start_vs_prescreen':
                if not CURRENT.get():
                    raise NativeApprovalDenied('VS prescreen requires exact native approval')
                if set(args) - {'human_approved', 'approval_note'}:
                    raise ValueError('Workflow owns VS prescreen execution scope')
                request = execution_request(root, state, args)
                state['vs_prescreen']['approved_scope_sha256'] = digest(decode(CURRENT.get())['scope'])
                begin_operation(location, state, tool, action, submission={
                    'project_id': request.project_id, 'request_id': request.request_id, 'execution_mode': 'execute'})
                run_id = JobManager(root).submit(request).run_id
            else:
                if args:
                    raise ValueError('Observe the original VS prescreen; task identity is bound')
                run_id = state['vs_prescreen']['run_id']
            state['vs_prescreen']['run_id'] = run_id
            try:
                result = observe(root, state, run_id)
                if action == 'evaluate_vs_prescreen' or state['vs_prescreen'].get('evaluation_artifact'):
                    result = review(root, state)
            except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
                reject(state, error)
                state.update(pending=None, stage='VS_PRESCREEN_EXECUTION')
                changed(location, state, 'vs_prescreen_evidence_rejected')
                raise
            state['pending'] = None
        changed(location, state, action)
        return route.response(state, result)


def reconcile(root, state):
    from .submission_identity import lookup_submission
    pending = state.get('pending') or {}
    if pending.get('action') != 'start_vs_prescreen':
        raise ValueError('No uncertain VS prescreen registration to reconcile')
    found = lookup_submission(root, pending['submission'])
    if found.get('registered_job_observed'):
        state['vs_prescreen']['run_id'] = found['run_id']
        observe(root, state, found['run_id'])
        state['pending'] = None
    return found


def refresh(root, state):
    record = state.get('vs_prescreen') or {}
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
