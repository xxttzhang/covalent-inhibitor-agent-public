"""Bind independent Vina to the existing source, job ledger and native approval."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

from covscreen.approval_scope import normalize_spec
from covscreen.execution import JobManager
from covscreen.jobs import JobSubmissionRequest, sha256_file
from covscreen.layout import science_script
from covscreen.ligand_parent_evidence import version_lock
from covscreen.native_approval import verify_spec
from covscreen.submission_identity import lookup_submission
from .independent_review import source, folder_for, encoded, lineage_files

ENGINE = 'de-novo-prereaction-vina'


def request_for(root, source_id, digest, *, execution_mode='plan'):
    _, original = source(root, source_id, digest)
    return JobSubmissionRequest(request_id=source_id+'-prereaction', project_id=original['project_id'],
        route='de-novo', engine=ENGINE, execution_mode=execution_mode,
        parameters={'workflow_id': original['study_id'], 'denovo_source_id': source_id,
            'denovo_source_manifest_sha256': digest,
            'source_validation_runtime_sha256': original.get('source_validation_runtime_sha256')},
        resource_budget={'gpu_count': 0, 'cpu_count': 1, 'memory_gb': 8,
                         'max_runtime_minutes': 30, 'max_candidates': 2})


def read_plan(root, source_id, digest):
    path = folder_for(root, source_id)/'prereaction-plan.json'
    if path.is_symlink() or not path.is_file():
        raise ValueError('Freeze the independent prereaction plan before submission')
    value = json.loads(path.read_text())
    if (value.get('schema') != 'denovo-prereaction-plan-v1' or value.get('source_id') != source_id
            or value.get('source_manifest_sha256') != digest
            or value.get('spec_sha256') != hashlib.sha256(encoded(value['spec'])).hexdigest()):
        raise ValueError('Independent prereaction plan identity changed')
    return value


def validate_request(root, request):
    p = request.parameters
    expected = request_for(root, p['denovo_source_id'], p['denovo_source_manifest_sha256'],
                           execution_mode=request.execution_mode)
    excluded = {'human_approved', 'approval_note'}
    if request.model_dump(mode='json', exclude=excluded) != expected.model_dump(mode='json', exclude=excluded):
        raise ValueError('Prereaction request differs from bounded native study plan')
    if request.execution_mode == 'execute':
        read_plan(root, p['denovo_source_id'], p['denovo_source_manifest_sha256'])


def validate_compiled_spec(root, spec):
    saved = read_plan(root, spec.parameters['denovo_source_id'], spec.parameters['denovo_source_manifest_sha256'])
    current = normalize_spec(spec)
    current.update(execution_mode='plan', human_approved=False, approval_note=None)
    if current != saved['spec']:
        raise ValueError('Prereaction runtime, source or scientific plan changed after freeze')


def environment():
    result = {key: value for key, value in os.environ.items() if key in {'HOME', 'PATH', 'LANG', 'LC_ALL'}}
    result.update(PYTHONDONTWRITEBYTECODE='1', PYTHONNOUSERSITE='1',
                  OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1')
    return result


def build(manager, run_id, parameters):
    root = manager.project_root
    folder, original = source(root, parameters['denovo_source_id'], parameters['denovo_source_manifest_sha256'])
    python = Path(manager.config.get('denovo_prereaction', {}).get('python', ''))
    if not python.is_absolute() or not python.is_file():
        raise ValueError('Register the independent denovo_prereaction Python runtime first')
    script = science_script('run_prereaction_registered.py')
    probe = subprocess.run([str(python), '-B', str(script), '--probe'], capture_output=True,
                           text=True, env=environment(), timeout=60, check=False)
    if probe.returncode:
        raise ValueError('Registered independent runtime unavailable: '+probe.stderr[-1200:])
    binding = json.loads(probe.stdout)
    if binding.get('schema') != 'denovo-prereaction-runtime-v1' or not binding.get('files'):
        raise ValueError('Invalid independent runtime probe')
    raw = encoded(binding)
    digest = hashlib.sha256(raw).hexdigest()
    destination = root/'state/de-novo-runtime'/digest
    if any(p.is_symlink() for p in (destination, *destination.parents)):
        raise ValueError('Runtime descriptor cannot traverse symlinks')
    destination.mkdir(parents=True, exist_ok=True)
    descriptor = destination/'runtime.json'
    if not descriptor.exists():
        try:
            with descriptor.open('xb') as stream:
                stream.write(raw)
        except FileExistsError:
            pass
    if descriptor.is_symlink() or descriptor.read_bytes() != raw:
        raise ValueError('Existing runtime descriptor differs')
    output = manager.registry.job_dir(run_id)/'artifacts/prereaction'
    parameters.update(prereaction_runtime_sha256=digest,
        prereaction_runtime_file=str(descriptor.relative_to(root)),
        prereaction_policy=binding['science'], runtime_versions=binding['versions'])
    command = [str(python), '-B', str(script), '--source', str(folder), '--source-sha256',
        parameters['denovo_source_manifest_sha256'], '--output', str(output),
        '--runtime-file', str(descriptor), '--runtime-sha256', digest]
    inputs = [str(root/name) for name in lineage_files(folder.relative_to(root).as_posix(), original)]
    inputs.append(str(descriptor))
    return command, inputs, [str(output)]


def observe(root, source_id, digest, *, evaluate=False):
    _, original = source(root, source_id, digest)
    request = request_for(root, source_id, digest, execution_mode='execute')
    found = lookup_submission(root, {'project_id': request.project_id,
        'request_id': request.request_id, 'execution_mode': 'execute'})
    result = {'schema': 'denovo-prereaction-observation-v1', 'route': 'de-novo',
        'study_id': original['study_id'], 'source_id': source_id, 'source_manifest_sha256': digest,
        'candidate_id': original['candidate_id'], 'data_root': str(root), 'compute_submitted': False,
        'submission': found, 'independent_prereaction_evaluation': 'NOT_PERFORMED',
        'scientific_result_verified': False, 'native_md_admission_granted': False}
    if not found.get('registered_job_observed'):
        result['state'] = 'NOT_SUBMITTED' if found['submission_state'] == 'NO_RECEIPT' else 'OUTCOME_UNKNOWN'
        return result
    manager = JobManager(root)
    spec = manager.registry.load_spec(found['run_id'])
    job = manager.registry.load_result(found['run_id'])
    verify_spec(spec)
    validate_compiled_spec(root, spec)
    result.update(state=job.state, run_id=spec.run_id, return_code=job.return_code, failure_reason=job.failure_reason)
    if evaluate and job.state == 'COMPLETED' and job.return_code == 0:
        from covscreen.md_preparation_evidence import registered_output
        output = manager.registry.job_dir(spec.run_id)/'artifacts'
        manifest = registered_output(root, output, spec, job, 'prereaction/manifest.json')
        inventory = json.loads(manifest.read_text())
        if (inventory.get('schema') != 'denovo-prereaction-artifacts-v1'
                or inventory.get('source_manifest_sha256') != digest
                or inventory.get('runtime_sha256') != spec.parameters['prereaction_runtime_sha256']):
            raise ValueError('Prereaction output source or runtime differs')
        for name, checksum in inventory['files'].items():
            if not name or Path(name).is_absolute() or any(p in ('', '.', '..') for p in name.split('/')):
                raise ValueError('Invalid prereaction output name')
            registered_output(root, output, spec, job, 'prereaction/'+name, checksum)
        command = list(spec.command) + ['--review-only']
        # Readback replays the complete generation source before and after
        # measuring raw poses; both replays must fit without skipping checks.
        checked = subprocess.run(command, capture_output=True, text=True, env=environment(), timeout=300)
        if checked.returncode:
            raise ValueError('Prereaction independent readback failed: '+checked.stderr[-1200:])
        report = json.loads(checked.stdout)
        if (report['study_id'] != original['study_id'] or report['source_manifest_sha256'] != digest
                or report['candidate_id'] != original['candidate_id']):
            raise ValueError('Prereaction readback belongs to another study or candidate')
        source(root, source_id, digest)
        for name, checksum in inventory['files'].items():
            registered_output(root, output, spec, job, 'prereaction/'+name, checksum)
        result.update(report=report, scientific_result_verified=True,
            independent_prereaction_evaluation=report['independent_prereaction_evaluation'],
            control_prereaction_evaluation=report['control_prereaction_evaluation'],
            manifest_sha256=sha256_file(manifest))
    elif job.state in {'FAILED', 'CANCELLED', 'TIMED_OUT'}:
        result['independent_prereaction_evaluation'] = 'ENGINE_FAILED'
    return result


def step(root, action, source_id, digest):
    root = Path(root).resolve(strict=True)
    folder = folder_for(root, source_id)
    folder.mkdir(parents=True, exist_ok=True)
    with version_lock(folder, '.review.lock'):
        if action == 'plan_prereaction':
            request = request_for(root, source_id, digest)
            spec = normalize_spec(JobManager(root)._prepare_submission(request))
            value = {'schema': 'denovo-prereaction-plan-v1', 'study_id': request.parameters['workflow_id'],
                'source_id': source_id, 'source_manifest_sha256': digest, 'data_root': str(root),
                'spec': spec, 'spec_sha256': hashlib.sha256(encoded(spec)).hexdigest(),
                'submit_tool': 'covalent_submit_job',
                'submit_arguments': request.model_copy(update={'execution_mode':'execute'}).model_dump(mode='json'),
                'native_approval_required': True, 'compute_submitted': False,
                'scope': 'One candidate plus afatinib; independent noncovalent Vina poses; no MD.'}
            path = folder/'prereaction-plan.json'
            if path.exists():
                if path.is_symlink() or json.loads(path.read_text()) != value:
                    raise ValueError('Existing prereaction plan changed; preserve it for explicit replanning')
            else:
                with path.open('xb') as stream:
                    stream.write(encoded(value))
            return value
        if action not in {'prereaction_status', 'evaluate_prereaction'}:
            raise ValueError('Unsupported prereaction action')
        result = observe(root, source_id, digest, evaluate=action == 'evaluate_prereaction')
        raw = encoded(result)
        checksum = hashlib.sha256(raw).hexdigest()
        history = folder/'prereaction-observations'
        history.mkdir(exist_ok=True)
        path = history/(checksum+'.json')
        if not path.exists():
            with path.open('xb') as stream:
                stream.write(raw)
        return {**result, 'study_observation': {'path': str(path.relative_to(root)), 'sha256': checksum}}
