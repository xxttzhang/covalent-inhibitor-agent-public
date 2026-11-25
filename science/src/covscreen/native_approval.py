"""Host-signed MCP approvals. The shared key is never a model/tool argument."""
import base64
from contextlib import contextmanager
from contextvars import ContextVar
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import time

CURRENT = ContextVar('native_approval', default=None)


class NativeApprovalDenied(ValueError):
    """Typed authorization failure; ValueError compatibility for existing callers."""


class NativeApprovalReplay(NativeApprovalDenied):
    """Consumed approval may have launched work: reconcile, never replay."""

# Explicitly mirrored by the OpenScience host bridge.  Adding a new start
# action on either side alone must leave execution unavailable until the two
# reviewed allowlists agree.
START_ACTION_TOOLS = frozenset({
    'covalent_library_receptor',
    'covalent_predict_cysteine_sites',
    'covalent_library_first_prepare_receptor',
    'covalent_library_first_parameterize_adduct',
    'covalent_library_first_prepare_openmm_system',
    'covalent_library_first_run_openmm_short_qc',
    'covalent_library_first_equilibrate_openmm_system',
    'covalent_library_first_run_openmm_production',
    'covalent_library_first_audit_openmm_trajectory',
    'covalent_library_first_analyze_openmm_replicas',
    'covalent_library_first_build_md_report',
    'covalent_library_first_deliver_md_candidates',
    'covalent_library_first_admit_md_candidate',
    'covalent_ligand_first_campaign',
})


def key():
    filename = os.environ.get('COVALENT_APPROVAL_KEY_FILE')
    if not filename:
        raise NativeApprovalDenied('Native approval key is not configured; execution denied')
    path = Path(filename)
    if path.is_symlink() or not path.is_file() or path.stat().st_mode & 0o077:
        raise NativeApprovalDenied('Approval key must be an owner-only regular file')
    data = path.read_bytes()
    if len(data) < 32:
        raise NativeApprovalDenied('Approval key is too short')
    return data


def signed(payload):
    data = base64.urlsafe_b64encode(json.dumps(payload, sort_keys=True, separators=(',', ':')).encode()).decode().rstrip('=')
    return data + '.' + hmac.new(key(), data.encode(), hashlib.sha256).hexdigest()


def decode(token):
    if not isinstance(token, str) or token.count('.') != 1:
        raise NativeApprovalDenied('Invalid native approval envelope')
    data, signature = token.split('.')
    if not hmac.compare_digest(hmac.new(key(), data.encode(), hashlib.sha256).hexdigest(), signature):
        raise NativeApprovalDenied('Invalid native approval signature')
    return json.loads(base64.urlsafe_b64decode(data + '=' * (-len(data) % 4)))


def needs_approval(name, arguments):
    if name == 'covalent_library_research' and arguments.get('action') == 'start_lo':
        return True
    return (name in START_ACTION_TOOLS and arguments.get('action') == 'start'
            or name == 'covalent_ligand_design_step' and arguments.get('action') in {'start_vs', 'start_pose', 'start_af3', 'start_receptor_preparation', 'start_covdock', 'start_md_graph', 'start_md_parameterization', 'start_md_system_preparation', 'start_md_short_qc', 'start_md_equilibration'}
            or name == 'covalent_library_research' and arguments.get('action') in {'start_vs', 'start_vs_prescreen', 'start_md_admission', 'start_md_parameterization', 'start_md_system_preparation', 'start_md_short_qc', 'start_md_equilibration', 'cancel_md_stage'}
            or name in {'covalent_ligand_first_chain', 'covalent_library_first_chain'} and arguments.get('submit') and arguments.get('execution_mode') == 'execute'
            or name in {'covalent_submit_job', 'covalent_retry_job'} and arguments.get('execution_mode', 'execute') == 'execute'
            or name == 'covalent_cancel_job')


@contextmanager
def authorize(root, name, arguments):
    values = dict(arguments)
    proof = values.pop('_native_approval', None)
    if not needs_approval(name, values):
        yield values
        return
    if not isinstance(proof, str):
        raise NativeApprovalDenied('Native user approval receipt required; boolean approval is insufficient')
    payload = decode(proof)
    expected = json.loads(payload['arguments_json'])
    if (payload.get('tool') != name or expected != values
            or not payload.get('session') or not payload.get('message')
            or not str(payload.get('approval_request_id', '')).startswith('per')
            or not 0 < payload['expires'] - time.time() <= 900
            or not re.fullmatch(r'[a-f0-9]{32}', payload.get('nonce', ''))):
        raise NativeApprovalDenied('Native approval scope/expiry mismatch')
    from .approval_scope import approval_scope
    if payload.get('scope') != approval_scope(root, name, values):
        raise ValueError('Inputs, configuration or derived execution plan changed after native approval')
    folder = Path(root) / 'state/native_approvals'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    # At-most-once: an uncertain call requires inspection, never replay.
    try:
        with (folder / (payload['nonce'] + '.json')).open('x') as stream:
            json.dump({'proof': proof}, stream)
    except FileExistsError as error:
        raise NativeApprovalReplay('Native approval already consumed; reconcile the original request before any new execution') from error
    context = CURRENT.set(proof)
    try:
        yield values
    finally:
        CURRENT.reset(context)


def seal_spec(spec):
    proof = CURRENT.get()
    if not proof:
        raise NativeApprovalDenied('A verified native approval is required for external dispatch')
    from .approval_scope import normalize_spec
    scope = decode(proof).get('scope', {})
    if scope.get('kind') not in {'job', 'campaign'}:
        raise NativeApprovalDenied('No native execution scope')
    if scope.get('kind') == 'job' and scope.get('spec') != normalize_spec(spec):
        raise ValueError('Job differs from the user-approved execution plan')
    if scope.get('kind') == 'campaign':
        validate_campaign_spec(scope, spec)
    payload = spec.model_dump(mode='json')
    payload['provenance'].pop('native_approval', None)
    return signed({'kind': 'approved-job-v1', 'spec': payload, 'approval': proof})


def verify_spec(spec):
    proof = spec.provenance.get('native_approval')
    if not proof:
        raise NativeApprovalDenied('Job has no native approval record')
    value = decode(proof)
    payload = spec.model_dump(mode='json')
    payload['provenance'].pop('native_approval', None)
    if value.get('kind') != 'approved-job-v1' or value.get('spec') != payload:
        raise ValueError('Approved job changed')
    decode(value['approval'])  # Verify host provenance; historical receipts do not expire.


def approval_observation(spec):
    """Public historical audit link, never a reusable execution credential."""
    limits = {'evidence_scope': 'historical_job_signature_binding',
              'boolean_or_note_proves_user_approval': False,
              'current_authorization_evaluated': False,
              'host_audit_revalidated': False, 'execution_authorized': False}
    if not spec.provenance.get('native_approval'):
        return {**limits, 'status': 'NOT_RECORDED', 'native_signature_verified': False,
                'meaning': 'This job has no stored signed approval binding. Historical user approval is not established '
                           'or disproved by this observation. human_approved and approval_note are declarations, '
                           'not proof of a native user decision. This is not an expiry or current-permission check.'}
    verify_spec(spec)
    envelope = decode(spec.provenance['native_approval'])
    approval = decode(envelope['approval'])
    scope = approval['scope']
    digest = hashlib.sha256(json.dumps(scope, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    return {**limits, 'status': 'SIGNED_JOB_BINDING_VERIFIED', 'native_signature_verified': True,
            'session_id': approval['session'], 'message_id': approval['message'],
            'approval_request_id': approval['approval_request_id'],
            'tool': approval['tool'], 'scope_sha256': digest,
            'scope_kind': scope['kind'],
            'meaning': 'The stored signature binds this historical job to a host approval reference. '
                       'The host audit record has not been re-read. This does not evaluate current permission '
                       'or authorize replay, a new stage, or execution through a study stop.'}


def validate_campaign_spec(scope, spec):
    wrapper = scope.get('plan', {}) if isinstance(scope, dict) else {}
    plan = wrapper.get('plan', {}) if isinstance(wrapper, dict) else {}
    engines = plan.get('executable_stage_engines', {})
    ceilings = plan.get('candidate_ceiling_by_stage', {})
    resources = plan.get('resource_ceiling_by_stage', {})
    stage = next((name for name, engine in engines.items() if engine == spec.engine), None)
    if (plan.get('route') != 'ligand-first' or spec.route != plan.get('route')
            or spec.project_id != plan.get('project_id') or not stage
            or stage not in plan.get('executable_stage_order', [])):
        raise ValueError('Job is outside the approved campaign route, project or stages')
    parameters = getattr(spec, 'parameters', {})
    if (parameters.get('dispatch_policy') != plan.get('dispatch_policy')
            or parameters.get('dispatch_batch') != plan.get('dispatch_batch')):
        raise ValueError('Job dispatch policy or batch differs from approved campaign plan')
    budget = spec.resource_budget
    stage_resources = resources.get(stage, {})
    comparisons = {
        'gpu_count': (budget.gpu_count, stage_resources.get('gpu_count')),
        'cpu_count': (budget.cpu_count, stage_resources.get('cpu_count')),
        'memory_gb': (budget.memory_gb, stage_resources.get('memory_gb')),
        'max_runtime_minutes': (budget.max_runtime_minutes, plan.get('max_runtime_minutes')),
        'max_candidates': (budget.max_candidates, ceilings.get(stage)),
    }
    exceeded = [name for name, (actual, limit) in comparisons.items()
                if not isinstance(limit, (int, float)) or actual > limit]
    if exceeded:
        raise ValueError('Job exceeds approved campaign budget: ' + ', '.join(exceeded))


@contextmanager
def resume_campaign(record):
    value = json.loads(Path(record).read_text())
    proof = value.get('native_approval')
    payload = decode(proof)
    args = json.loads(payload['arguments_json'])
    digest = hashlib.sha256(json.dumps(value['plan'], sort_keys=True).encode()).hexdigest()
    if payload.get('tool') != 'covalent_ligand_first_campaign' or args.get('approved_plan_sha256') != digest or digest != value.get('plan_sha256'):
        raise ValueError('Campaign approval does not match persisted plan')
    token = CURRENT.set(proof)
    try:
        yield
    finally:
        CURRENT.reset(token)
