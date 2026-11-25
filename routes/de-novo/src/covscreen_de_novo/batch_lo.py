"""Exact batch LO request, independently approved after verified VS selection."""
import hashlib
import json
from .batch_lo_input import load, encoded


def observe(root, digest, *, evaluate=False):
    from covscreen.execution import JobManager
    from covscreen.submission_identity import lookup_submission
    from covscreen.native_approval import verify_spec
    from covscreen.source_release import frozen_release, historical_release
    payload, _ = load(root, digest)
    request = request_for(root, digest, 'execute')
    found = lookup_submission(root, {'project_id': request.project_id,
        'request_id': request.request_id, 'execution_mode': 'execute'})
    response = {'schema': 'denovo-batch-lo-status-v1', 'task_id': payload['task_id'],
        'manifest_sha256': digest, 'workflow_id': payload['workflow_id'], 'execution': found,
        'compute_submitted': False, 'scientific_acceptance': False, 'md_ready': False}
    if not found.get('registered_job_observed'):
        if evaluate:
            raise ValueError('No registered batch LO job to evaluate; do not resubmit')
        return response
    manager = JobManager(root)
    spec = manager.registry.load_spec(found['run_id'])
    result = manager.registry.load_result(found['run_id'])
    verify_spec(spec)
    release = spec.provenance.get('source_release')
    if release:
        historical_release(release)
    validate_compiled_spec(root, spec, recheck_vs=False)
    response.update(run_id=spec.run_id, return_code=result.return_code, failure_reason=result.failure_reason)
    if evaluate:
        from covscreen.ligand_covdock_evidence import evaluate_outputs, witness
        from .batch_input import validate
        from .batch_priority import select
        state = {'covdock_execution': {'run_id': spec.run_id},
            'refinement_input': {'artifact_id': 'denovo-batch-lo-' + digest},
            'target': {'target_id': 'EGFR', 'site_id': 'EGFR_C797'}}
        if release and release != frozen_release():
            from .batch_historical_lo import evaluate as historical_evaluate
            review = historical_evaluate(root, spec)
        else:
            review = evaluate_outputs(root, state, manager, spec, result, route='de-novo')
        from pathlib import Path
        original, _ = validate(Path(root) / 'state/de-novo-batches' / ('batch-' + payload['batch_manifest_sha256']),
            payload['batch_manifest_sha256'])
        response.update(evaluation=review, evaluation_artifact=witness(root, review),
            prereaction_evaluation='NOT_PERFORMED', prereaction_priority=select(review, original['candidates']))
    return response


def validate_evidence(root, digest):
    from .batch_vs import evaluate
    payload, folder = load(root, digest)
    current = evaluate(root, payload['batch_manifest_sha256'])
    if (hashlib.sha256(encoded(current)).hexdigest() != payload['review_sha256']
            or encoded(current) != (folder / 'review.json').read_bytes()):
        raise ValueError('LO source differs from freshly verified signed VS evidence')
    return payload, folder


def plan(root, digest):
    from covscreen.execution import JobManager
    from covscreen.approval_scope import normalize_spec
    # JobManager validates the signed VS evidence while compiling this plan.
    payload, folder = load(root, digest)
    request = request_for(root, digest)
    spec = normalize_spec(JobManager(root)._prepare_submission(request))
    value = {'schema': 'denovo-batch-lo-plan-v1', 'task_id': payload['task_id'],
        'manifest_sha256': digest, 'workflow_id': payload['workflow_id'], 'spec': spec,
        'spec_sha256': hashlib.sha256(encoded(spec)).hexdigest(),
        'submit_arguments': request.model_copy(update={'execution_mode': 'execute'}).model_dump(mode='json'),
        'submit_tool': 'covalent_submit_job', 'native_approval_required': True, 'compute_submitted': False}
    filename = folder / 'plan.json'
    if filename.is_symlink():
        raise ValueError('LO plan must be a regular frozen file')
    raw = encoded(value)
    if not filename.exists():
        with filename.open('xb') as stream:
            stream.write(raw)
    if filename.read_bytes() != raw:
        raise ValueError('LO plan changed; preserve the original approval scope')
    return value


def validate_compiled_spec(root, spec, *, recheck_vs=True):
    from covscreen.approval_scope import normalize_spec
    validator = validate_evidence if recheck_vs else load
    _, folder = validator(root, spec.parameters['denovo_batch_lo_sha256'])
    path = folder / 'plan.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Freeze the batch LO plan before execution')
    saved = json.loads(path.read_text())
    current = normalize_spec(spec)
    current.update(execution_mode='plan', human_approved=False, approval_note=None)
    if (saved.get('schema') != 'denovo-batch-lo-plan-v1' or current != saved['spec']
            or hashlib.sha256(encoded(current)).hexdigest() != saved['spec_sha256']):
        raise ValueError('Batch LO compiled inputs, runtime or budget changed')


def request_for(root, digest, execution_mode='plan'):
    from covscreen.jobs import JobSubmissionRequest
    from covscreen.covdock_gate import STRICT_ALGORITHM_VERSION
    payload, folder = load(root, digest)
    relative = folder.relative_to(root).as_posix()
    count = payload['candidate_count']
    if not 1 <= count <= 12:
        raise ValueError('Batch LO requires one to twelve selected parents')
    cpus = count
    return JobSubmissionRequest(request_id=payload['workflow_id'] + '-lo-' + digest[:12],
        project_id=payload['workflow_id'], route='de-novo', engine='schrodinger-covdock-lo',
        attempt=2 if payload.get('retry_of') else 1,
        parent_run_id=payload.get('retry_of', {}).get('run_id'),
        execution_mode=execution_mode, resource_budget={'gpu_count': 0, 'cpu_count': cpus,
            'memory_gb': max(8, 4 * cpus), 'max_runtime_minutes': 240, 'max_candidates': count},
        parameters={'workflow_id': payload['workflow_id'], 'denovo_batch_lo_sha256': digest,
            'dispatch_policy': 'denovo-batch-lo-instance-v1', 'execution_instance': 'asgbie3',
            'candidate_table': relative + '/candidates.csv', 'expected_input_count': count,
            'expected_input_sha256': payload['candidate_table_sha256'],
            'expected_identity_sha256': payload['identity_sha256'], 'input_score_source': 'core',
            'receptor': payload['receptor'], 'attachment_residue': 'A:797', 'nucleophilic_atom': 'SG',
            'reaction_type': 'Michael Addition', 'gate_algorithm_version': STRICT_ALGORITHM_VERSION,
            'refinement_lineage_files': sorted(set(payload['lineage_sha256']) |
                {relative + '/input.json', relative + '/review.json'}),
            'prepare_receptor': True, 'final_n': count, 'njobs': cpus, 'host': f'localhost:{cpus}',
            'resume': False, 'evaluation_scope': 'exploratory_batch_adduct_prereaction_pending',
            'environment_review': {'scope': 'registered_EGFR_C797_batch_exploratory_adduct_review',
                'vs_run_id': payload['vs_run_id'], 'vs_review_sha256': payload['review_sha256'],
                'biological_suitability_verified': False}})


def validate_request(root, request):
    expected = request_for(root, request.parameters['denovo_batch_lo_sha256'], request.execution_mode)
    excluded = {'human_approved', 'approval_note'}
    if request.model_dump(mode='json', exclude=excluded) != expected.model_dump(mode='json', exclude=excluded):
        raise ValueError('Batch LO request differs from frozen selected inputs or budget')
