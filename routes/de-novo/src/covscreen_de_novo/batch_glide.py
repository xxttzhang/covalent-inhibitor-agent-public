"""Frozen native request for independent post-LO Glide SP, never covalent docking."""
from .batch_prereaction_input import load
from .batch_lo_input import encoded
import hashlib
import json

ENGINE = 'de-novo-prereaction-glide'


def observe(root, source_digest, manifest_digest, *, evaluate=False):
    from covscreen.execution import JobManager
    from covscreen.submission_identity import lookup_submission
    from covscreen.native_approval import verify_spec
    from covscreen.source_release import historical_release
    inputs, folder = load(root, source_digest, manifest_digest)
    request = request_for(root, source_digest, manifest_digest, 'execute')
    found = lookup_submission(root, {'project_id': request.project_id, 'request_id': request.request_id,
        'execution_mode': 'execute'})
    response = {'schema': 'denovo-batch-glide-status-v1', 'task_id': inputs['task_id'],
        'manifest_sha256': manifest_digest, 'source_sha256': source_digest,
        'execution': found, 'compute_submitted': False, 'scientific_acceptance': False}
    if not found.get('registered_job_observed'):
        if evaluate:
            raise ValueError('No registered Glide task to evaluate; do not resubmit')
        return response
    manager = JobManager(root)
    spec, result = manager.registry.load_spec(found['run_id']), manager.registry.load_result(found['run_id'])
    verify_spec(spec)
    if spec.provenance.get('source_release'):
        historical_release(spec.provenance['source_release'])
    validate_compiled_spec(root, spec)
    response.update(run_id=spec.run_id, return_code=result.return_code, failure_reason=result.failure_reason)
    if evaluate:
        from .batch_glide_evidence import read
        response['evaluation'] = read(root, manager, spec, result, inputs, folder)
    return response


def plan(root, source_digest, manifest_digest):
    from covscreen.execution import JobManager
    from covscreen.approval_scope import normalize_spec
    value, folder = load(root, source_digest, manifest_digest)
    request = request_for(root, source_digest, manifest_digest)
    spec = normalize_spec(JobManager(root)._prepare_submission(request))
    result = {'schema': 'denovo-batch-glide-plan-v1', 'task_id': value['task_id'],
        'workflow_id': value['workflow_id'], 'source_sha256': source_digest, 'manifest_sha256': manifest_digest,
        'spec': spec, 'spec_sha256': hashlib.sha256(encoded(spec)).hexdigest(),
        'submit_arguments': request.model_copy(update={'execution_mode': 'execute'}).model_dump(mode='json'),
        'submit_tool': 'covalent_submit_job', 'native_approval_required': True, 'compute_submitted': False}
    path = folder / 'plan.json'
    if path.is_symlink():
        raise ValueError('Glide plan cannot be a symlink')
    raw = encoded(result)
    if not path.exists():
        with path.open('xb') as stream:
            stream.write(raw)
    if path.read_bytes() != raw:
        raise ValueError('Glide plan changed; retain the original approval scope')
    return result


def validate_compiled_spec(root, spec):
    from covscreen.approval_scope import normalize_spec
    p = spec.parameters
    _, folder = load(root, p['prereaction_source_sha256'], p['prereaction_manifest_sha256'])
    path = folder / 'plan.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Freeze Glide plan before execution')
    saved = json.loads(path.read_text())
    current = normalize_spec(spec)
    current.update(execution_mode='plan', human_approved=False, approval_note=None)
    if (saved.get('schema') != 'denovo-batch-glide-plan-v1' or current != saved['spec']
            or hashlib.sha256(encoded(current)).hexdigest() != saved['spec_sha256']):
        raise ValueError('Glide compiled runtime, inputs or budget changed')


def build(manager, run_id, parameters):
    from covscreen.covdock_receptor import runtime_paths
    from covscreen.jobs import sha256_file
    from covscreen.layout import science_script
    value, folder = load(manager.project_root, parameters['prereaction_source_sha256'], parameters['prereaction_manifest_sha256'])
    paths = runtime_paths(manager.config.get('schrodinger', {}))
    runtime_files = [paths['root'] / 'glide', paths['root'] / 'ligprep', paths['license_file']]
    if any(not path.is_file() for path in runtime_files):
        raise ValueError('Register Glide, LigPrep and vendor license before planning')
    parameters['glide_runtime'] = {'paths': {key: str(path) if path is not None else None for key, path in paths.items()},
        'files': {str(path): sha256_file(path) for path in runtime_files}}
    inputs = [folder / 'input.json', folder / 'prereaction_grid.in', folder / 'reference/fresh_afatinib.sdf',
        manager.project_root / value['source']['receptor']]
    inputs += [folder / row['file'] for row in value['states'] if row['state'] == 'PREPARED_NOT_DOCKED']
    script = science_script('run_batch_glide_registered.py')
    output = manager.registry.job_dir(run_id) / 'artifacts/prereaction'
    return [manager._python(), str(script), '--root', str(manager.project_root), '--run-id', run_id], [str(path) for path in inputs], [str(output)]


def request_for(root, source_digest, manifest_digest, execution_mode='plan'):
    from covscreen.jobs import JobSubmissionRequest
    value, folder = load(root, source_digest, manifest_digest)
    candidates = [row for row in value['states'] if row['state'] == 'PREPARED_NOT_DOCKED']
    if not 1 <= len(candidates) <= 5:
        raise ValueError('Independent Glide requires one to five freshly prepared candidates')
    return JobSubmissionRequest(request_id=value['workflow_id'] + '-prereaction-' + manifest_digest[:12],
        project_id=value['workflow_id'], route='de-novo', engine=ENGINE, execution_mode=execution_mode,
        parameters={'workflow_id': value['workflow_id'], 'prereaction_source_sha256': source_digest,
            'prereaction_manifest_sha256': manifest_digest, 'execution_instance': 'asgbie3',
            'dispatch_policy': 'denovo-prereaction-glide-instance-v1',
            'prepared_candidate_ids': [row['candidate_id'] for row in candidates],
            'control_parent': 'CONTROL_AFATINIB', 'max_control_states': 2,
            'input_directory': folder.relative_to(root).as_posix()},
        resource_budget={'cpu_count': 4, 'gpu_count': 0, 'memory_gb': 16,
            'max_runtime_minutes': 120, 'max_candidates': len(candidates) + 2})


def validate_request(root, request):
    p = request.parameters
    expected = request_for(root, p['prereaction_source_sha256'], p['prereaction_manifest_sha256'], request.execution_mode)
    excluded = {'human_approved', 'approval_note'}
    if request.model_dump(mode='json', exclude=excluded) != expected.model_dump(mode='json', exclude=excluded):
        raise ValueError('Glide request differs from frozen independent inputs and budget')


def validate_boundary(spec, instance='asgbie3'):
    p, budget = spec.parameters, spec.resource_budget
    candidates = p.get('prepared_candidate_ids')
    if (spec.engine != ENGINE or spec.route != 'de-novo' or instance != 'asgbie3'
            or p.get('execution_instance') != instance
            or p.get('dispatch_policy') != 'denovo-prereaction-glide-instance-v1'
            or spec.project_id != p.get('workflow_id') or not spec.project_id.startswith('de-flow-')
            or not isinstance(candidates, list) or not 1 <= len(candidates) <= 5
            or any(not isinstance(name, str) or not name.startswith('PXM_') for name in candidates)
            or len(set(candidates)) != len(candidates)
            or p.get('control_parent') != 'CONTROL_AFATINIB' or p.get('max_control_states') != 2
            or budget.model_dump() != {'cpu_count': 4, 'gpu_count': 0, 'memory_gb': 16,
                'max_runtime_minutes': 120, 'max_candidates': len(candidates) + 2}
            or spec.attempt != 1 or spec.parent_run_id is not None
            or spec.provenance.get('worker_backend') != 'external-instance'):
        raise ValueError('Glide instance task differs from its bounded independent recipe')
