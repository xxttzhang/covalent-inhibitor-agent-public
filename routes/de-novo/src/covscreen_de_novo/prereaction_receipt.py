"""Read prior independent Vina verification without launching or remeasuring.

This is a historical projection, not MD admission or fresh scientific review.
The source identity comes from the caller's current verified source observation.
"""
import hashlib
import json
from pathlib import Path

from covscreen.branch_guard import require_unquarantined
from covscreen.jobs import ArtifactRegistry, sha256_file
from covscreen.md_preparation_evidence import registered_output
from covscreen.native_approval import verify_spec
from covscreen.submission_identity import lookup_submission
from .independent_review import folder_for
from .prereaction_workflow import ENGINE, validate_compiled_spec


def retained(root, current):
    root = Path(root).resolve(strict=True)
    source_id = current['source_id']
    digest = current['source_manifest_sha256']
    folder = folder_for(root, source_id)
    found = lookup_submission(root, {'project_id': current['project_id'],
        'request_id': source_id+'-prereaction', 'execution_mode': 'execute'})
    result = {'schema': 'denovo-prereaction-retained-observation-v1',
        **{key: current[key] for key in ('study_id', 'source_id', 'source_manifest_sha256', 'candidate_id')},
        'submission': found, 'compute_submitted': False,
        'scientific_result_verified': False, 'receipt_recomputed_by_status': False,
        'native_md_admission_granted': False, 'independent_prereaction_evaluation': 'NOT_PERFORMED'}
    if not found.get('registered_job_observed'):
        result['state'] = 'NOT_SUBMITTED' if found['submission_state'] == 'NO_RECEIPT' else 'OUTCOME_UNKNOWN'
        return result
    registry = ArtifactRegistry(root, create=False)
    spec = registry.load_spec(found['run_id'])
    job = registry.load_result(spec.run_id)
    verify_spec(spec)
    validate_compiled_spec(root, spec)
    if (spec.engine != ENGINE or spec.route != 'de-novo'
            or spec.project_id != current['project_id']
            or spec.parameters.get('workflow_id') != current['study_id']
            or spec.parameters.get('denovo_source_id') != source_id
            or spec.parameters.get('denovo_source_manifest_sha256') != digest
            or any(getattr(job, key) != getattr(spec, key) for key in
                   ('run_id', 'project_id', 'route', 'engine', 'attempt', 'parent_run_id'))):
        raise ValueError('Retained prereaction job identity differs from the current study')
    result.update(state=job.state, run_id=spec.run_id, return_code=job.return_code,
                  failure_reason=job.failure_reason)
    if job.state in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
        result['independent_prereaction_evaluation'] = 'ENGINE_FAILED'
        return result
    if (job.state, job.return_code) != ('COMPLETED', 0):
        return result
    require_unquarantined(root, spec)
    history = folder/'prereaction-observations'
    if history.is_symlink():
        raise ValueError('Retained prereaction history cannot be a symlink')
    reviews = []
    for path in sorted(history.glob('*.json')):
        if path.is_symlink():
            raise ValueError('Retained prereaction observation cannot be a symlink')
        raw = path.read_bytes()
        checksum = hashlib.sha256(raw).hexdigest()
        if path.name != checksum+'.json':
            raise ValueError('Retained prereaction observation witness changed')
        prior = json.loads(raw)
        if prior.get('scientific_result_verified') is not True:
            continue
        if (prior.get('schema') != 'denovo-prereaction-observation-v1'
                or prior.get('run_id') != spec.run_id
                or any(prior.get(key) != current[key] for key in
                       ('study_id', 'source_id', 'source_manifest_sha256', 'candidate_id'))
                or (prior.get('state'), prior.get('return_code')) != ('COMPLETED', 0)):
            raise ValueError('Retained prereaction review belongs to another study, candidate or job')
        reviews.append((path, checksum, prior))
    if not reviews:
        return result
    output = registry.job_dir(spec.run_id)/'artifacts'
    manifest = registered_output(root, output, spec, job, 'prereaction/manifest.json')
    inventory = json.loads(manifest.read_text())
    if (inventory.get('schema') != 'denovo-prereaction-artifacts-v1'
            or inventory.get('source_manifest_sha256') != digest
            or inventory.get('runtime_sha256') != spec.parameters['prereaction_runtime_sha256']
            or not isinstance(inventory.get('files'), dict) or 'report.json' not in inventory['files']):
        raise ValueError('Retained prereaction inventory identity differs')
    for name, checksum in inventory['files'].items():
        if not name or Path(name).is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
            raise ValueError('Invalid retained prereaction output name')
        registered_output(root, output, spec, job, 'prereaction/'+name, checksum)
    report = json.loads((output/'prereaction/report.json').read_text())
    for key in ('study_id', 'source_manifest_sha256', 'candidate_id'):
        if report.get(key) != current[key]:
            raise ValueError('Retained prereaction report identity differs')
    for _, _, prior in reviews:
        if (prior.get('report') != report or prior.get('manifest_sha256') != sha256_file(manifest)
                or any(prior.get(key) != report.get(key) or report.get(key) not in {'PASS', 'FAIL'}
                       for key in ('independent_prereaction_evaluation', 'control_prereaction_evaluation'))):
            raise ValueError('Retained independent review differs from registered prereaction evidence')
    result.update(report=report, manifest_sha256=sha256_file(manifest),
        independent_prereaction_evaluation=report['independent_prereaction_evaluation'],
        control_prereaction_evaluation=report['control_prereaction_evaluation'],
        scientific_result_verified=True, evidence_revalidated_by_status=True,
        verification_scope='Prior independent readback retained; identity and artifact hashes checked, poses not remeasured.',
        prior_review_observations=[{'path':str(path.relative_to(root)), 'sha256':checksum}
                                  for path, checksum, _ in reviews])
    return result
