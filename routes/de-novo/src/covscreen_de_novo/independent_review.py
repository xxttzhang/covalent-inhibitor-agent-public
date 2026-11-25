"""Native-study independent CovDock review using the shared immutable job ledger.

No start/retry action lives here. Submission is exclusively covalent_submit_job
with its existing exact native approval. The recorded review is a projection of
that job and the original generation, never another computation ledger.
"""
from copy import deepcopy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys

from covscreen.approval_scope import normalize_spec
from covscreen.covdock_gate import STRICT_ALGORITHM_VERSION
from covscreen.execution import JobManager
from covscreen.jobs import JobSubmissionRequest, sha256_file
from covscreen.ligand_covdock_evidence import evaluate_outputs
from covscreen.ligand_parent_evidence import version_lock
from covscreen.native_approval import verify_spec
from covscreen.submission_identity import lookup_submission

SOURCE_PATTERN = r'denovo-review-[a-f0-9]{32}'


def source_api():
    folder = Path(__file__).resolve().parents[2] / 'evaluation'
    # This path is installed code beside the current route, never task-owned code.
    if str(folder) not in sys.path:
        sys.path.insert(0, str(folder))
    spec = importlib.util.spec_from_file_location('covscreen_denovo_source', folder/'source.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def encoded(value):
    return (json.dumps(value, sort_keys=True, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def source(root, source_id, digest, *, validation_release=None):
    if not re.fullmatch(SOURCE_PATTERN, source_id) or not re.fullmatch('[a-f0-9]{64}', digest):
        raise ValueError('Exact native independent-source identity required')
    if source_id != 'denovo-review-' + digest[:32]:
        raise ValueError('Source identity differs from manifest hash')
    folder = Path(root) / 'state/de-novo-inputs' / source_id
    if any(p.is_symlink() for p in (folder, *folder.parents)):
        raise ValueError('Independent source root cannot traverse symlinks')
    # The shared MCP interpreter intentionally retains the other routes' runtime.
    # Recompute chemistry in the explicitly registered, isolated science runtime.
    from covscreen.layout import science_script
    python = Path(JobManager(root).config.get('denovo_prereaction', {}).get('python', ''))
    if not python.is_absolute() or not python.is_file():
        raise ValueError('Register the independent denovo_prereaction Python runtime first')
    environment = {key: value for key, value in os.environ.items() if key in {'HOME', 'PATH', 'LANG', 'LC_ALL'}}
    environment.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                       OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    script = science_script('run_source_validation_registered.py')
    if validation_release is not None:
        from covscreen.source_release import historical_release
        release = historical_release(validation_release)
        name = 'routes/de-novo/scripts/run_source_validation_registered.py'
        if name not in release['files']:
            raise ValueError('Parent release lacks the registered source validator')
        script = Path(release['root'])/name
    result = subprocess.run([str(python), '-B', str(script),
        '--source', str(folder), '--source-sha256', digest], capture_output=True, text=True,
        # Full-census PoseBusters replay is CPU work, not merely file hashing.
        # A real 32-slot source takes ~90 seconds without contention.
        env=environment, timeout=300)
    if result.returncode:
        raise ValueError('Independent source validation failed: '+result.stderr[-1600:])
    verified = json.loads(result.stdout)
    value = verified['source']
    runtime = verified['runtime_sha256']
    if not isinstance(runtime, str) or not re.fullmatch('[a-f0-9]{64}', runtime):
        raise ValueError('Independent source runtime identity is missing')
    value['source_validation_runtime_sha256'] = runtime
    return folder, value


def request_for(root, source_id, digest, *, execution_mode='plan', recovery=False):
    parent, original = None, None
    if recovery:
        original = read_plan(root, source_id, digest)
        found = lookup_submission(root, {'project_id': original['spec']['project_id'],
            'request_id': source_id+'-covdock', 'execution_mode': 'execute'})
        if not found.get('registered_job_observed'):
            raise ValueError('Recovery requires the original registered attempt')
        from covscreen.denovo_dispatch_recovery import confirmed
        parent, _ = confirmed(JobManager(root).registry, found['run_id'])
    # The runtime fingerprint includes its absolute installed source paths.
    # Replay the parent's verified source-validation release so a deployment
    # path change cannot masquerade as changed chemistry or be silently ignored.
    folder, value = source(root, source_id, digest,
        validation_release=parent.provenance.get('source_release') if parent else None)
    rel = folder.relative_to(root).as_posix()
    contract = value['input_contract']
    from .covdock_input import freeze_receptor
    receptor = folder_for(root, source_id) / 'covdock-receptor'
    preprocessing = freeze_receptor(folder/'independent/protein_A.pdb', receptor)
    derived = receptor.relative_to(root).as_posix()
    request = JobSubmissionRequest(request_id=source_id + '-covdock', project_id=value['project_id'],
        route='de-novo', engine='schrodinger-covdock-lo', execution_mode=execution_mode,
        parameters={'workflow_id': value['study_id'], 'denovo_source_id': source_id,
            'dispatch_policy': 'denovo-independent-local-v1',
            'denovo_source_manifest_sha256': digest,
            'source_validation_runtime_sha256': value.get('source_validation_runtime_sha256'),
            'candidate_table': rel + '/candidates.csv',
            'expected_input_count': contract['input_count'], 'expected_input_sha256': contract['input_sha256'],
            'expected_identity_sha256': contract['identity_sha256'],
            'receptor': derived + '/receptor.pdb', 'attachment_residue': 'A:797',
            'receptor_preprocessing': preprocessing,
            'nucleophilic_atom': 'SG', 'reaction_type': 'Michael Addition', 'input_score_source': 'core',
            'gate_algorithm_version': STRICT_ALGORITHM_VERSION,
            'refinement_lineage_files': [*lineage_files(rel, value), derived+'/receptor.pdb', derived+'/receipt.json'],
            'prepare_receptor': True, 'final_n': 2, 'njobs': 1, 'host': 'localhost:1', 'resume': False,
            'environment_review': {'scope': 'registered_4G5J_chain_A_exploratory_adduct_review',
                'biological_suitability_verified': False, 'receptor_sha256': value['receptor_sha256']},
            'evaluation_scope': 'independent_adduct_and_redocking_control_prereaction_pending'},
        resource_budget={'gpu_count': 0, 'cpu_count': 1, 'memory_gb': 8,
                         'max_runtime_minutes': 30, 'max_candidates': 2})
    if not recovery:
        return request
    previous = deepcopy(original['submit_arguments'])
    fresh = request.model_copy(update={'execution_mode': 'execute'}).model_dump(mode='json')
    fresh['parameters'].pop('dispatch_policy')
    if fresh != previous or parent.project_id != request.project_id:
        raise ValueError('Recovery changed the frozen candidate, control, budget or scientific request')
    return request.model_copy(update={'attempt': 2, 'parent_run_id': parent.run_id,
                                     'request_id': source_id+'-covdock-retry-2'})


def lineage_files(relative, source):
    # The generation census can exceed the shared executor's 256-file lineage
    # limit. Retain and replay its complete inventory in source.validate; the
    # job pins its manifest and the exact inputs it actually consumes. Do not
    # silently drop generation evidence from the portable source package.
    required = ('generation/request.json', 'generation/authorization.json', 'generation/status.json',
        'independent/input_manifest.json', 'independent/candidate_000.sdf', 'independent/protein_A.pdb',
        'control/control_manifest.json', 'control/control_afatinib.sdf',
        'control/control_crystal_adduct.sdf', 'control/control_atom_map.json', 'candidates.csv')
    if not set(required).issubset(source['files']):
        raise ValueError('Independent source lacks required bounded lineage files')
    return [relative+'/source.json', *[relative+'/'+name for name in required]]


def validate_request(root, request):
    """Called by the shared executor, including raw submit_job calls."""
    parameters = request.parameters
    expected = request_for(root, parameters['denovo_source_id'],
        parameters['denovo_source_manifest_sha256'], execution_mode=request.execution_mode,
        recovery=request.attempt == 2)
    excluded = {'human_approved', 'approval_note'}
    if request.model_dump(mode='json', exclude=excluded) != expected.model_dump(mode='json', exclude=excluded):
        raise ValueError('De novo execution differs from native study inputs or bounded plan')
    if request.execution_mode == 'execute':
        read_plan(root, parameters['denovo_source_id'], parameters['denovo_source_manifest_sha256'], attempt=request.attempt)


def folder_for(root, source_id):
    if not re.fullmatch(SOURCE_PATTERN, source_id):
        raise ValueError('Invalid independent source ID')
    folder = Path(root) / 'state/de-novo-reviews' / source_id
    if any(p.is_symlink() for p in (folder, *folder.parents)):
        raise ValueError('Review must not traverse symbolic links')
    return folder


def read_plan(root, source_id, digest, *, attempt=1):
    if attempt not in {1, 2}:
        raise ValueError('Unsupported independent evaluation attempt')
    path = folder_for(root, source_id) / ('plan.json' if attempt == 1 else 'recovery-plan.json')
    if path.is_symlink() or not path.is_file():
        raise ValueError('Freeze the native-study independent evaluation plan before submission')
    value = json.loads(path.read_text())
    if (value.get('schema') != 'native-denovo-independent-plan-v1'
            or value.get('source_manifest_sha256') != digest or value.get('source_id') != source_id):
        raise ValueError('Independent plan identity changed')
    expected = hashlib.sha256(encoded(value['spec'])).hexdigest()
    if value.get('spec_sha256') != expected:
        raise ValueError('Independent plan spec checksum changed')
    if attempt == 2:
        from covscreen.denovo_dispatch_recovery import confirmed
        parent, record = confirmed(JobManager(root).registry, value['spec']['parent_run_id'])
        if (value['spec']['attempt'] != 2 or value['spec']['request_id'] != parent.request_id+'-retry-2'
                or value.get('recovery', {}).get('parent_spec_sha256') != record['scope']['spec_sha256']
                or value['spec']['project_id'] != parent.project_id):
            raise ValueError('Recovery plan lost its confirmed undispatched parent')
    return value


def validate_compiled_spec(root, spec):
    saved = read_plan(root, spec.parameters['denovo_source_id'], spec.parameters['denovo_source_manifest_sha256'], attempt=spec.attempt)
    current = normalize_spec(spec)
    current['execution_mode'] = 'plan'
    current['human_approved'] = False
    current['approval_note'] = None
    if current != saved['spec']:
        raise ValueError('De novo runtime, code, inputs or command changed after plan freeze')


def requirements():
    return {'independent_prereaction_evaluation': 'NOT_PERFORMED',
        'control_redocking_validation': 'NOT_PERFORMED',
        'full_chemical_graph_and_stereochemistry_review': 'NOT_PERFORMED',
        'ready_for_scientific_handoff': False, 'native_md_admission': 'NOT_PERFORMED',
        'blocking': ['Independent prereaction evaluation requires a separate source-bound native job and verified output.',
            'The frozen receptor-aligned redocking control must be measured on actual vendor output.',
            'Full reaction graph/stereochemistry must be independently verified before scientific handoff.']}


def observe(root, source_id, digest):
    _, original = source(root, source_id, digest)
    recovery = (folder_for(root, source_id)/'recovery-plan.json').exists()
    request = request_for(root, source_id, digest, execution_mode='execute', recovery=recovery)
    result = {'schema': 'native-denovo-independent-review-v1', 'route': 'de-novo',
        'data_root': str(root),
        'study_id': original['study_id'], 'project_id': original['project_id'],
        'source_id': source_id, 'source_manifest_sha256': digest,
        'generation_task_id': original['task_id'], 'candidate_id': original['candidate_id'],
        'generation_request_sha256': original['request_sha256'], 'receptor_sha256': original['receptor_sha256'],
        'source_candidate_sha256': original['source_candidate_sha256'],
        'compute_submitted': False, 'scientific_result_verified': False,
        'independent_adduct_evaluation': 'NOT_PERFORMED', **requirements()}
    found = lookup_submission(root, {'project_id': request.project_id,
        'request_id': request.request_id, 'execution_mode': 'execute'})
    result['submission'] = found
    if not found.get('registered_job_observed'):
        result['state'] = 'NOT_SUBMITTED' if found['submission_state'] == 'NO_RECEIPT' else 'OUTCOME_UNKNOWN'
        return result, None
    manager = JobManager(root)
    spec = manager.registry.load_spec(found['run_id'])
    job = manager.registry.load_result(found['run_id'])
    verify_spec(spec)
    validate_compiled_spec(root, spec)
    result.update(state=job.state, run_id=spec.run_id, failure_reason=job.failure_reason,
                  return_code=job.return_code)
    return result, (manager, spec, job)


def assessment(review):
    """An engine failure is evidence insufficiency, never molecular inactivity."""
    candidate = next((row for row in review['candidates'] if row['input_sdf_index'] == 0), None)
    if candidate is None or candidate['compound_id'] != 'candidate_000':
        raise ValueError('Independent review lost the frozen candidate identity')
    if candidate['engine_status'] != 'SUCCESS':
        return 'ENGINE_FAILED'
    gate = candidate['gate']
    if gate['physical_validity'] == 'FAIL':
        return 'PHYSICAL_GATE_REJECTED'
    if gate['physical_validity'] != 'PASS' or gate['physical_evidence_complete'] is not True:
        return 'EVIDENCE_INSUFFICIENT'
    return 'ADDUCT_PHYSICAL_CHECKS_PASSED_SCIENTIFIC_REVIEW_PENDING'


def step(root, action, source_id, source_manifest_sha256):
    if action in {'plan_prereaction', 'prereaction_status', 'evaluate_prereaction'}:
        from .prereaction_workflow import step as prereaction_step
        return prereaction_step(root, action, source_id, source_manifest_sha256)
    root = Path(root).resolve(strict=True)
    folder = folder_for(root, source_id)
    folder.mkdir(parents=True, exist_ok=True)
    with version_lock(folder, '.review.lock'):
        if action == 'cancel_plan':
            result, current = observe(root, source_id, source_manifest_sha256)
            if not current:
                raise ValueError('No registered independent attempt to cancel')
            manager, spec, _ = current
            from covscreen.denovo_dispatch_recovery import validate
            validate(manager.registry, spec.run_id)
            return {**result, 'cancel_tool': 'covalent_cancel_job', 'native_approval_required': True,
                    'cancel_arguments': {'run_id': spec.run_id, 'human_approved': True}}
        if action in {'plan', 'replan'}:
            recovery = action == 'replan' or (folder/'recovery-plan.json').exists()
            path = folder/('recovery-plan.json' if recovery else 'plan.json')
            if path.exists():
                return read_plan(root, source_id, source_manifest_sha256, attempt=2 if recovery else 1)
            request = request_for(root, source_id, source_manifest_sha256, recovery=recovery)
            # Planning compiles only. It does not register a job, check out a
            # production license, allocate resources or mint native approval.
            spec = JobManager(root)._prepare_submission(request)
            normalized = normalize_spec(spec)
            value = {'schema': 'native-denovo-independent-plan-v1', 'source_id': source_id,
                'data_root': str(root),
                'source_manifest_sha256': source_manifest_sha256, 'study_id': request.parameters['workflow_id'],
                'spec': normalized, 'spec_sha256': hashlib.sha256(encoded(normalized)).hexdigest(),
                'submit_tool': 'covalent_submit_job',
                'submit_arguments': request.model_copy(update={'execution_mode': 'execute'}).model_dump(mode='json'),
                'native_approval_required': True, 'compute_submitted': False,
                'scope': 'One generated candidate plus afatinib control; independent adduct evaluation only.',
                **requirements()}
            if recovery:
                from covscreen.denovo_dispatch_recovery import confirmed
                parent, record = confirmed(JobManager(root).registry, request.parent_run_id)
                value['recovery'] = {'protocol': record['protocol'], 'parent_run_id': parent.run_id,
                    'parent_spec_sha256': record['scope']['spec_sha256'], 'parent_state': 'CANCELLED',
                    'compute_started': False, 'candidate_and_control_unchanged': True}
            if path.exists():
                if json.loads(path.read_text()) != value:
                    raise ValueError('Existing frozen plan differs; retain it and review a separate attempt')
            else:
                with path.open('xb') as stream:
                    stream.write(encoded(value))
            return value
        if action not in {'status', 'evaluate', 'prepare_md_handoff'}:
            raise ValueError('Unsupported independent review action')
        result, current = observe(root, source_id, source_manifest_sha256)
        if action == 'status' and (folder/'latest.json').is_file():
            pointer = json.loads((folder/'latest.json').read_text())
            prior_path = root/pointer['path']
            expected_path = folder/'observations'/(pointer['sha256']+'.json')
            if prior_path != expected_path or prior_path.is_symlink() or sha256_file(prior_path) != pointer['sha256']:
                raise ValueError('Prior study observation witness changed')
            prior = json.loads(prior_path.read_text())
            reported = prior.get('adduct_review') or prior.get('last_adduct_review')
            if (reported and prior.get('run_id') == result.get('run_id')
                    and prior.get('source_manifest_sha256') == source_manifest_sha256):
                result['last_adduct_review'] = reported
                result['independent_adduct_evaluation'] = prior['independent_adduct_evaluation']
                result['evaluation_receipt_recomputed_by_status'] = False
                if prior.get('control_redocking_review'):
                    result['control_redocking_review'] = prior['control_redocking_review']
                    result['control_redocking_validation'] = prior['control_redocking_validation']
                    if prior['control_redocking_validation'] == 'PASS':
                        result['blocking'] = [item for item in result.get('blocking', []) if
                            item != 'The frozen receptor-aligned redocking control must be measured on actual vendor output.']
                if prior.get('chemical_review'):
                    result['chemical_review'] = prior['chemical_review']
                    result['full_chemical_graph_and_stereochemistry_review'] = prior['full_chemical_graph_and_stereochemistry_review']
                    if prior['full_chemical_graph_and_stereochemistry_review'] == 'PASS':
                        result['blocking'] = [item for item in result.get('blocking', []) if
                            item != 'Full reaction graph/stereochemistry must be independently verified before scientific handoff.']
        if action == 'status' and ((folder/'prereaction-plan.json').exists()
                                  or (folder/'prereaction-observations').exists()):
            from .prereaction_receipt import retained
            try:
                independent = retained(root, result)
                result['prereaction_review'] = independent
                result['independent_prereaction_evaluation'] = independent['independent_prereaction_evaluation']
                result['control_prereaction_evaluation'] = independent.get('control_prereaction_evaluation', 'NOT_PERFORMED')
                result['prereaction_receipt_recomputed_by_status'] = False
                if independent['scientific_result_verified'] is True:
                    result['blocking'] = [item for item in result.get('blocking', []) if item !=
                        'Independent prereaction evaluation requires a separate source-bound native job and verified output.']
                    if (independent['independent_prereaction_evaluation'] != 'PASS'
                            or independent.get('control_prereaction_evaluation') != 'PASS'):
                        result['blocking'].append('Verified prereaction candidate or control failed the frozen scientific gates.')
            except (ValueError, OSError, KeyError, TypeError) as error:
                result['independent_prereaction_evaluation'] = 'EVIDENCE_INSUFFICIENT'
                result['prereaction_review'] = {'state': 'EVIDENCE_REVIEW_REQUIRED', 'reason': str(error),
                    'receipt_recomputed_by_status': False, 'native_md_admission_granted': False}
        if current and current[2].state in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
            result['independent_adduct_evaluation'] = 'ENGINE_FAILED'
        if action != 'status' and current:
            manager, spec, job = current
            if (job.state, job.return_code) in {('COMPLETED', 0), ('FAILED', 2)}:
                state = {'covdock_execution': {}, 'refinement_input': {'artifact_id': source_id},
                         'target': {'target': 'EGFR', 'site': 'C797', 'taxon': 9606}}
                try:
                    review = evaluate_outputs(root, state, manager, spec, job, route='de-novo')
                    result['adduct_review'] = review
                    result['independent_adduct_evaluation'] = assessment(review)
                    from .control_review import evaluate as evaluate_control
                    source_folder, _ = source(root, source_id, source_manifest_sha256)
                    control = evaluate_control(root, source_folder, manager, spec, job, review)
                    # Revalidate source bytes after both coordinate extraction and measurement.
                    source(root, source_id, source_manifest_sha256)
                    result['control_redocking_review'] = control
                    result['control_redocking_validation'] = control['state']
                    if control['state'] == 'PASS':
                        result['blocking'] = [item for item in result['blocking'] if
                            item != 'The frozen receptor-aligned redocking control must be measured on actual vendor output.']
                    from .chemical_review import evaluate as evaluate_chemical
                    chemical = evaluate_chemical(root, source_folder, manager, spec, job, review)
                    source(root, source_id, source_manifest_sha256)
                    result['chemical_review'] = chemical
                    result['full_chemical_graph_and_stereochemistry_review'] = chemical['state']
                    if chemical['state'] == 'PASS':
                        result['blocking'] = [item for item in result['blocking'] if
                            item != 'Full reaction graph/stereochemistry must be independently verified before scientific handoff.']
                except (ValueError, OSError, KeyError, TypeError) as error:
                    result['state'] = 'EVIDENCE_REVIEW_REQUIRED'
                    result['review_error'] = str(error)
                    result['independent_adduct_evaluation'] = 'EVIDENCE_INSUFFICIENT'
                    if result.get('control_redocking_review'):
                        result['full_chemical_graph_and_stereochemistry_review'] = 'EVIDENCE_INSUFFICIENT'
                        result['chemical_review'] = {'state': 'EVIDENCE_INSUFFICIENT',
                            'reason': str(error), 'md_ready': False}
                    elif result.get('adduct_review'):
                        result['control_redocking_validation'] = 'EVIDENCE_INSUFFICIENT'
                        result['control_redocking_review'] = {'state': 'EVIDENCE_INSUFFICIENT',
                            'reason': str(error), 'md_ready': False}
            elif job.state in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
                result['independent_adduct_evaluation'] = 'ENGINE_FAILED'
        if action != 'status' and (folder/'prereaction-plan.json').is_file():
            from .prereaction_workflow import observe as observe_prereaction
            try:
                independent = observe_prereaction(root, source_id, source_manifest_sha256, evaluate=True)
                result['prereaction_review'] = independent
                result['independent_prereaction_evaluation'] = independent['independent_prereaction_evaluation']
                if (independent['scientific_result_verified'] is True
                        and independent['independent_prereaction_evaluation'] == 'PASS'
                        and independent.get('control_prereaction_evaluation') == 'PASS'):
                    result['blocking'] = [item for item in result['blocking'] if item !=
                        'Independent prereaction evaluation requires a separate source-bound native job and verified output.']
            except (ValueError, OSError, KeyError, TypeError) as error:
                result['independent_prereaction_evaluation'] = 'EVIDENCE_INSUFFICIENT'
                result['prereaction_review'] = {'state': 'EVIDENCE_REVIEW_REQUIRED', 'reason': str(error)}
        if action == 'prepare_md_handoff':
            # The existing scientific input contract explicitly requires the
            # prereaction and independent adduct gates. There is deliberately
            # no caller-supplied PASS, external receipt path or override switch.
            result['state'] = 'MD_HANDOFF_BLOCKED'
            result['md_handoff'] = {'state': 'NOT_PERFORMED', 'candidate_delivery': None,
                'reason': 'All independent prereaction/control/chemical gates must pass; the native De novo shared MD delivery adapter is not yet connected.'}
        # Save each observation in the source study projection, including failed
        # batches. An evaluation never erases source failures or a previous review.
        raw = encoded(result)
        checksum = hashlib.sha256(raw).hexdigest()
        history = folder/'observations'
        history.mkdir(exist_ok=True)
        path = history/(checksum+'.json')
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(raw)
        pointer = {'study_id': result['study_id'], 'source_id': source_id,
            'path': str(path.relative_to(root)), 'sha256': checksum}
        temporary = folder/'latest.pending'
        temporary.write_bytes(encoded(pointer))
        temporary.replace(folder/'latest.json')
        return {**result, 'study_observation': pointer}
