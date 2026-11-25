"""Bind registered Library candidates and the retained receptor to native VS.

Planning never launches a worker. The prescreen approval does not authorize VS;
environment judgments and a separate exact execution approval remain necessary.
"""
from copy import deepcopy
import hashlib
import json
from uuid import uuid4

from .approval_scope import normalize_spec
from .covdock_vs import bound_file
from .covdock_vs_preparation import freeze as freeze_preparation
from .covdock_receptor import CovDockRuntimeUnavailable, inspect_receptor
from .execution import JobManager
from .jobs import JobSubmissionRequest, sha256_file
from .library_vs_prescreen_input import load as load_prescreen, encoded
from . import library_vs_prescreen_execution as prescreen
from .ligand_parent_evidence import version_lock
from .ligand_vs_input import environment_inventory, environment_review, retire_plan
from .workflow_protocol import changed, revision
from .workflow_store import load as load_state

ACTIONS = {'prepare_vs_input', 'read_vs_input', 'review_vs_environment', 'check_vs_runtime'}
STAGES = {'VS_INPUT_REVIEW', 'VS_PLAN_READY'}


def available(state):
    record = state.get('vs_prescreen') or {}
    return (not state.get('pending') and not state.get('vs_execution')
        and state['stage'] in STAGES | {'VS_PRESCREEN_REVIEW'}
        and record.get('job_state') == 'COMPLETED' and record.get('job_identity_verified') is True
        and not record.get('evidence_error')
        and (record.get('evaluation_summary') or {}).get('vs_input_eligible') is True)


def actions(state):
    if not available(state):
        return []
    return ['prepare_vs_input'] if not state.get('vs_input') else sorted(ACTIONS)


def build(root, state):
    from covscreen_library_first import screening_inputs, receptor_handoff
    from .library_vs_prescreen_evidence import evaluate, freeze as freeze_review
    # Re-read native identity and actual products, never trust a cached PASS.
    current = deepcopy(state)
    record = current.get('vs_prescreen') or {}
    if not record.get('evaluation_artifact'):
        raise ValueError('Library VS requires an evaluated registered prescreen')
    prescreen.observe(root, current, record['run_id'])
    manager = JobManager(root)
    review, mapped = evaluate(root, current, manager, manager.registry.load_spec(record['run_id']),
                             manager.registry.load_result(record['run_id']))
    if not review['vs_input_eligible'] or not 1 <= review['candidate_count'] <= 5:
        raise ValueError('Library VS requires nonempty eligible prescreen candidates')
    reference = freeze_review(root, review, mapped)
    if reference != record['evaluation_artifact']:
        raise ValueError('Library VS candidate review differs from its registered study')
    frozen, lineage = load_prescreen(root, record['input'])
    inputs = screening_inputs.load_record(root, state)
    receptor_record = receptor_handoff.load_record(root, state)
    receptor = receptor_record['result']
    identity = receptor['identity']
    if (identity != {'entry_id': '1M17', 'uniprot': 'P00533', 'canonical_cys': 797,
                     'author_chain': 'A', 'author_residue': '773'}
        or state['target'] != {'target': 'EGFR', 'taxon': 9606}
        or state['site_selection']['site'] != 'C797'
        or inputs['binding']['receptor_evidence'] != state['screening_receptor']
        or frozen['screening_inputs'] != state['screening_inputs']):
        raise ValueError('Library VS receptor/site differs from the frozen pilot study')
    receptor_path = frozen['roles']['receptor']
    if (sha256_file(bound_file(root, receptor_path)) != receptor['receptor']['sha256']
        or inputs['research_inputs']['receptor']['sha256'] != receptor['receptor']['sha256']):
        raise ValueError('Library VS receptor differs from the prepared study receptor')
    attachment = identity['author_chain'] + ':' + identity['author_residue']
    inventory = inspect_receptor(bound_file(root, receptor_path), attachment, include_residue_inventory=True)
    receptor_manifest = 'state/screening_receptors/' + state['screening_receptor']['id'][19:] + '/screening-receptor.json'
    lineage.update(receptor['inventory'])
    lineage[receptor_manifest] = state['screening_receptor']['sha256']
    lineage.update(review['output_sha256'])
    lineage.update({reference['directory'] + '/' + name: sha for name, sha in reference['files'].items()})
    # Keep the exact file and row order accepted by prescreen readback.
    sdf = bound_file(root, reference['directory'] + '/vs-candidates.sdf').read_bytes()
    from .library_vs_geometry import Policy, contact_sites
    policy = Policy()
    contacts = contact_sites(bound_file(root, frozen['roles']['reference']), bound_file(root, receptor_path), policy)
    files = {'ligands.sdf': sdf, 'receptor.pdb': bound_file(root, receptor_path).read_bytes(),
        'receptor-inventory.json': encoded(inventory),
        'receptor-context.json': encoded(receptor['context']),
        'geometry-policy.json': encoded(policy.model_dump(mode='json')),
        'reference-contact-sites.json': encoded(contacts)}
    payload = {'schema': 'library-vs-study-input-v1', 'workflow_id': state['workflow_id'],
        'target': state['target'], 'site_selection': state['site_selection'],
        'screening_inputs': state['screening_inputs'], 'screening_receptor': state['screening_receptor'],
        'prescreen_run_id': record['run_id'], 'candidate_review': reference,
        'selected_candidate_ids': [row['candidate_id'] for row in review['candidates']],
        'candidates': review['candidates'], 'selection': review['selection'],
        'attachment_residue': attachment, 'source_site_mapping': identity,
        'reaction_type': 'Michael Addition', 'lineage_sha256': lineage,
        'files': {name: hashlib.sha256(raw).hexdigest() for name, raw in files.items()},
        'review_requirements': {'environment_review_required': True,
            'reference_polar_contact_site_count': len(contacts['sites']),
            'geometry_policy': policy.model_dump(mode='json'),
            'questions': ['Assess the selected chain and biological assembly',
                'Assess removed chains, ligands, waters, ions and cofactors',
                'Assess construct completeness and canonical C797 to author A773 mapping',
                'Assess Michael Addition scope and candidate-specific interaction requirements']},
        'receptor_prepared': True, 'environment_suitability_verified': False,
        'compute_submitted': False, 'scientific_acceptance': False, 'md_ready': False}
    if any(sha256_file(bound_file(root, name)) != sha for name, sha in lineage.items()):
        raise ValueError('Library VS source changed during binding')
    return payload, files


def reference(root, location, payload):
    checksum = sha256_file(location / 'input.json')
    return {'artifact_id': 'library-vs-input-' + checksum[:32], 'manifest_sha256': checksum,
        'directory': str(location.relative_to(root)), 'candidate_count': len(payload['selected_candidate_ids']),
        'attachment_residue': payload['attachment_residue'], 'environment_review_required': True,
        'compute_submitted': False, 'scientific_acceptance': False, 'md_ready': False}


def freeze(root, state):
    payload, files = build(root, state)
    raw = encoded(payload)
    checksum = hashlib.sha256(raw).hexdigest()
    location = root / 'state/library-vs-inputs' / checksum[:32]
    if any(path.is_symlink() for path in (root / 'state', location.parent, location)):
        raise ValueError('Library VS input cannot traverse symlinks')
    location.parent.mkdir(parents=True, exist_ok=True)
    if not location.exists():
        staging = location.with_name('.' + checksum[:32] + '-' + uuid4().hex)
        staging.mkdir()
        for name, contents in {**files, 'input.json': raw}.items():
            (staging / name).write_bytes(contents)
        staging.rename(location)
    verify_files(root, location, payload, files)
    return reference(root, location, payload)


def verify_files(root, location, payload, files):
    if {path.name for path in location.iterdir()} != {*files, 'input.json'}:
        raise ValueError('Library VS input inventory changed')
    for name, raw in {**files, 'input.json': encoded(payload)}.items():
        if bound_file(root, str((location / name).relative_to(root))).read_bytes() != raw:
            raise ValueError('Library VS input bytes changed: ' + name)


def load(root, state):
    ref = state['vs_input']
    payload, files = build(root, state)
    checksum = hashlib.sha256(encoded(payload)).hexdigest()
    location = root / 'state/library-vs-inputs' / checksum[:32]
    if ref['directory'] != str(location.relative_to(root)) or ref['manifest_sha256'] != checksum:
        raise ValueError('Library VS input differs from current study evidence')
    verify_files(root, location, payload, files)
    if ref != reference(root, location, payload):
        raise ValueError('Library VS input reference changed')
    return payload


def request_for(root, state, configuration, *, source_revision=None):
    payload = load(root, state)
    review = environment_review(root, state, payload)
    directory = state['vs_input']['directory']
    manifests = environment_inventory(state, payload)
    ref = state.get('vs_environment_review')
    if review:
        manifests[ref['path']] = ref['manifest_sha256']
    count = len(payload['selected_candidate_ids'])
    from .library_instance import configured, budget
    instance, cpu_limit = configured(root)
    resources = (budget(count, stage='vs', cpu_limit=cpu_limit) if instance else
        {'gpu_count': 0, 'cpu_count': 4, 'memory_gb': 16, 'max_runtime_minutes': 60, 'max_candidates': count})
    packet = freeze_preparation(root, {
        'source': {'route': 'library-first', 'workflow_id': state['workflow_id'],
            'revision': state['revision'] if source_revision is None else source_revision, 'manifests': manifests},
        'original_ligands': directory + '/ligands.sdf', 'receptor': directory + '/receptor.pdb',
        'attachment_residue': payload['attachment_residue'],
        'budget': resources, 'njobs': min(resources['cpu_count'], count),
        **({'execution_instance': instance} if instance else {})}, configuration)
    return JobSubmissionRequest(request_id=state['workflow_id'] + '-vs-' + state['vs_input']['manifest_sha256'][:12]
        + ('-' + ref['manifest_sha256'][:12] if ref else ''), project_id=state['workflow_id'],
        route='library-first', engine='schrodinger-covdock-vs', parameters={'vs_preparation': packet},
        resource_budget=packet['plan']['budget'], execution_mode='plan')


def refresh(root, state):
    if not state.get('vs_input') or state.get('pending') or state.get('vs_execution'):
        return
    try:
        payload = load(root, state)
        environment_review(root, state, payload)
    except (ValueError, KeyError, TypeError, OSError, RuntimeError) as error:
        retire_plan(state, 'Current Library VS sources or environment review failed revalidation')
        state.update(vs_input_error=str(error), stage='VS_INPUT_REVIEW')
    else:
        state.pop('vs_input_error', None)


def step(root, tool, workflow_id, expected_revision, action, args):
    route = prescreen.workflow()
    location = route.directory(root, workflow_id)
    with version_lock(location, '.workflow.lock'):
        state = load_state(location)
        revision(state, expected_revision)
        from .receptor_environment_review import EVIDENCE_READ_SCHEMA, read_evidence
        permitted = ({'environment_review'} if action == 'review_vs_environment' else
                     set(EVIDENCE_READ_SCHEMA) if action == 'read_vs_input' else set())
        valid_fields = not (set(args) - permitted) if action == 'read_vs_input' else set(args) == permitted
        if tool != route.RESEARCH or action not in actions(state) or not valid_fields:
            raise ValueError('Library VS planning requires verified idle prescreen candidates and workflow-owned inputs')
        if action == 'prepare_vs_input':
            ref = freeze(root, state)
            if state.get('vs_input') not in (None, ref):
                raise ValueError('Library VS input changed; reconsider before replacing it')
            if not state.get('vs_input'):
                state['stage'] = 'VS_INPUT_REVIEW'
            state['vs_input'] = ref
        payload = load(root, state)
        if action == 'review_vs_environment':
            from .receptor_environment_review import freeze as freeze_review
            ref = freeze_review(root, state['vs_input'], environment_inventory(state, payload),
                               args['environment_review'], scope='covdock-vs')
            if ref != state.get('vs_environment_review'):
                retire_plan(state, 'Superseded by a source-bound Library VS environment assessment')
                state['stage'] = 'VS_INPUT_REVIEW'
            state['vs_environment_review'] = ref
        reviewed = environment_review(root, state, payload)
        if action == 'check_vs_runtime':
            manager = JobManager(root)
            previous = state.get('vs_plan') or {}
            try:
                request = request_for(root, state, manager.config.get('schrodinger', {}),
                                      source_revision=previous.get('source_revision'))
                spec = normalize_spec(manager._prepare_submission(request))
            except CovDockRuntimeUnavailable:
                assessment = state.get('vs_environment_review')
                retire_plan(state, 'Library VS runtime configuration is unavailable')
                state['vs_environment_review'] = assessment
                state['vs_runtime'] = {'state': 'WAITING_RUNTIME_CONFIGURATION', 'compute_submitted': False}
                state['stage'] = 'VS_INPUT_REVIEW'
            else:
                raw = encoded(spec)
                checksum = hashlib.sha256(raw).hexdigest()
                private = location / 'vs-plans'
                if private.is_symlink():
                    raise ValueError('Library VS private plans cannot traverse symlinks')
                private.mkdir(exist_ok=True, mode=0o700)
                path = private / (checksum + '.json')
                if not path.exists():
                    with path.open('xb') as stream:
                        path.chmod(0o600)
                        stream.write(raw)
                if path.is_symlink() or path.read_bytes() != raw:
                    raise ValueError('Library VS private plan changed')
                ref = {'path': str(path.relative_to(root)), 'sha256': checksum,
                    'source_revision': request.parameters['vs_preparation']['plan']['source']['revision']}
                if previous and previous != ref:
                    raise ValueError('Library VS runtime changed; reconsider before replacing its plan')
                state['vs_plan'] = ref
                eligible = bool(reviewed and reviewed['decision'] == 'suitable_for_exploratory_vs')
                state['stage'] = 'VS_PLAN_READY' if eligible else 'VS_INPUT_REVIEW'
                state['vs_runtime'] = {'state': 'VS_PLAN_COMPILED' if eligible else 'PLAN_COMPILED_ENVIRONMENT_REVIEW_REQUIRED',
                    'plan_sha256': checksum, 'resource_budget': request.resource_budget.model_dump(mode='json'),
                    'mode': 'enrichment', 'worker_backend': 'external-hpc', 'compute_submitted': False,
                    'environment_eligible_for_exploratory_vs': eligible, 'license_checkout_verified': False,
                    'native_execution_available': False}
        evidence = read_evidence(root, environment_inventory(state, payload), **args) if action == 'read_vs_input' else None
        state.pop('vs_input_error', None)
        changed(location, state, action)
        return route.response(state, {'vs_input': state['vs_input'], 'review_requirements': payload['review_requirements'],
            'environment_review': environment_review(root, state, payload),
            **({'evidence': evidence} if evidence is not None else {}),
            'evidence_inventory': environment_inventory(state, payload), 'source_site_mapping': payload['source_site_mapping'],
            'vs_runtime': state.get('vs_runtime'), 'compute_submitted': False, 'scientific_acceptance': False, 'md_ready': False})
