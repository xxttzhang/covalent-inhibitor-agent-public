"""Bind shared VS preparation to a registered PocketXMol batch."""
import hashlib
from pathlib import Path

from .batch_input import validate


def evaluate(root, checksum):
    """Re-read signed native products; identity and score are not geometry gates."""
    from covscreen.execution import JobManager
    from covscreen.covdock_vs_evidence import products
    from covscreen import covdock_vs_evidence, covdock_vs_output
    from covscreen.jobs import sha256_file
    from .batch_geometry import measure, POLICY
    from .batch_lo_selection import select
    root = Path(root).resolve(strict=True)
    observed = status(root, checksum)
    execution = observed['execution']
    if execution.get('submission_state') != 'REGISTERED' or execution.get('job_state') not in {'COMPLETED', 'FAILED'}:
        raise ValueError('Batch VS evaluation requires its completed registered request')
    manager = JobManager(root)
    run_id = execution['run_id']
    spec, result = manager.registry.load_spec(run_id), manager.registry.load_result(run_id)
    relative = 'state/de-novo-batches/batch-' + checksum
    manifest, _ = validate(root / relative, checksum)
    state = {'workflow_id': observed['workflow_id'], 'vs_execution': {'run_id': run_id},
        'vs_input': {'directory': relative, 'manifest_sha256': checksum}}
    rejected = []
    rows, counts, hashes = products(root, state, manager, spec, result, reaction_site_rejections=rejected)
    candidates = []
    for original in manifest['candidates']:
        name = original['candidate_id']
        poses = [{'pose_index': row['pose_index'], 'score': row['score'], 'geometry': measure(row),
                  'product_identity_verified': row['product_identity_verified'],
                  'prepared_receptor_identity_verified': row['prepared_receptor_identity_verified']}
                 for row in rows if row['candidate_id'] == name]
        candidates.append({'candidate_id': name, 'parent_id': original['parent_id'],
            'pose_count': counts[name], 'poses': poses,
            'identity_verified_pose_count': len(poses),
            'rejected_poses': [row for row in rejected if row['candidate_id'] == name],
            'best_vs_score': min((pose['score'] for pose in poses), default=None),
            'geometry_evaluated': True, 'lo_eligible': any(p['geometry']['eligible_for_lo'] for p in poses),
            'lo_selected': False})
    for name, digest in {**spec.input_sha256, **hashes}.items():
        if sha256_file(root / name) != digest:
            raise ValueError('Batch VS evidence changed during readback')
    selection = select(candidates, manifest['candidates'])
    selected = {row['candidate_id'] for row in selection['selected']}
    for candidate in candidates:
        candidate['lo_selected'] = candidate['candidate_id'] in selected
    return {'schema': 'denovo-batch-vs-products-v1', 'task_id': manifest['task_id'],
        'manifest_sha256': checksum, 'workflow_id': observed['workflow_id'], 'run_id': run_id,
        'candidate_count': len(candidates), 'candidates': candidates, 'output_sha256': hashes,
        'input_sha256': spec.input_sha256, 'compute_submitted': False, 'geometry_policy': POLICY,
        'lo_selection': selection,
        'original_execution_state': result.state,
        'postprocessing_recovery': result.state == 'FAILED',
        'reaction_site_rejections': rejected,
        'evaluation_policy': 'denovo-vs-per-pose-reaction-identity-v2',
        'evaluation_code_sha256': {'batch_vs': sha256_file(Path(__file__)),
            'covdock_vs_evidence': sha256_file(Path(covdock_vs_evidence.__file__)),
            'covdock_vs_output': sha256_file(Path(covdock_vs_output.__file__))},
        'scientific_acceptance': False, 'md_ready': False,
        'next_required': 'Freeze selected independent LO inputs and obtain native execution approval',
        'limitations': ['VS scores are screening scores, not activity predictions.',
            'Provisional VS geometry is for LO prioritization, not complete physical or scientific acceptance.']}


def status(root, checksum):
    """Observe the exact batch request without recompilation or resubmission."""
    import re
    from covscreen.submission_identity import lookup_submission
    if not re.fullmatch('[a-f0-9]{64}', checksum):
        raise ValueError('Exact registered batch digest required')
    folder = Path(root) / 'state/de-novo-batches' / ('batch-' + checksum)
    manifest, _ = validate(folder, checksum)
    workflow = 'de-flow-' + hashlib.sha256(manifest['task_id'].encode()).hexdigest()[:24]
    observed = lookup_submission(root, {'project_id': workflow,
        'request_id': workflow + '-vs-' + checksum[:12], 'execution_mode': 'execute'})
    return {'schema': 'denovo-batch-vs-status-v1', 'task_id': manifest['task_id'],
        'manifest_sha256': checksum, 'workflow_id': workflow, 'execution': observed,
        'compute_submitted': False, 'scientific_acceptance': False}


def compile_request(root, checksum, configuration, *, cpu_count=55, max_runtime_minutes=60):
    """Compile a native plan only; approval and submission remain host actions."""
    if type(cpu_count) is not int or not 1 <= cpu_count <= 55:
        raise ValueError('De novo VS requires 1 to 55 CPUs')
    import re
    from covscreen.covdock_vs_preparation import freeze
    from covscreen.jobs import JobSubmissionRequest
    from .covdock_input import freeze_receptor
    if not re.fullmatch('[a-f0-9]{64}', checksum):
        raise ValueError('Exact registered batch digest required')
    root = Path(root).resolve(strict=True)
    relative = Path('state/de-novo-batches') / ('batch-' + checksum)
    folder = root / relative
    manifest, _ = validate(folder, checksum)
    freeze_receptor(folder / 'receptor.pdb', folder / 'covdock-receptor')
    workflow = 'de-flow-' + hashlib.sha256(manifest['task_id'].encode()).hexdigest()[:24]
    count = manifest['candidate_count']
    cpu_count = min(cpu_count, count)
    budget = dict(gpu_count=0, cpu_count=cpu_count, memory_gb=max(16, 4 * cpu_count),
                  max_runtime_minutes=max_runtime_minutes, max_candidates=count)
    source = {'route': 'de-novo', 'workflow_id': workflow, 'revision': 0,
        'manifests': {str(relative / name): hashlib.sha256((folder / name).read_bytes()).hexdigest()
                      for name in ('input.json', 'binding.json', 'receptor.pdb',
                                   'covdock-receptor/receptor.pdb', 'covdock-receptor/receipt.json')}}
    packet = freeze(root, {'source': source, 'original_ligands': str(relative / 'ligands.sdf'),
        'receptor': str(relative / 'covdock-receptor/receptor.pdb'), 'attachment_residue': 'A:797',
        'budget': budget, 'njobs': min(cpu_count, count), 'execution_instance': 'asgbie3'}, configuration)
    return JobSubmissionRequest(request_id=workflow + '-vs-' + checksum[:12],
        project_id=workflow, route='de-novo', engine='schrodinger-covdock-vs',
        execution_mode='plan', parameters={'vs_preparation': packet}, resource_budget=budget)


def validate_request(root, request, configuration):
    """Execution must retain the exact registered batch and native budget."""
    manifests = request.parameters['vs_preparation']['plan']['source']['manifests']
    inputs = [name for name in manifests if name.endswith('/input.json')]
    if len(inputs) != 1:
        raise ValueError('De novo VS requires one batch input manifest')
    # Preserve the resource scope of already frozen and approved requests.
    expected = compile_request(root, manifests[inputs[0]], configuration,
                               cpu_count=request.resource_budget.cpu_count)
    declarations = {'human_approved', 'approval_note', 'execution_mode'}
    if request.model_dump(exclude=declarations) != expected.model_dump(exclude=declarations):
        raise ValueError('De novo VS execution differs from the frozen batch request')


def verify_source(root, source, original_ligands, receptor=None):
    from covscreen.covdock_vs import bound_file
    names = [name for name in source.manifests if name.endswith('/input.json')]
    if len(names) != 1:
        raise ValueError('De novo VS requires one batch input manifest')
    manifest_path = bound_file(root, names[0])
    manifest, binding = validate(manifest_path.parent, source.manifests[names[0]])
    expected = 'de-flow-' + hashlib.sha256(manifest['task_id'].encode()).hexdigest()[:24]
    if source.workflow_id != expected:
        raise ValueError('De novo VS workflow differs from the generation task')
    folder = Path(names[0]).parent
    required = {str(folder / 'binding.json'), str(folder / 'receptor.pdb'),
                str(folder / 'covdock-receptor/receptor.pdb'), str(folder / 'covdock-receptor/receipt.json')}
    if not required.issubset(source.manifests):
        raise ValueError('De novo VS must bind its receptor and selection receipt')
    for name in required:
        if hashlib.sha256(bound_file(root, name).read_bytes()).hexdigest() != source.manifests[name]:
            raise ValueError('De novo VS source changed')
    from .covdock_input import freeze_receptor
    freeze_receptor(manifest_path.parent / 'receptor.pdb', manifest_path.parent / 'covdock-receptor')
    if original_ligands != str(folder / 'ligands.sdf'):
        raise ValueError('De novo VS ligands differ from registered batch')
    if receptor is not None and receptor != str(folder / 'covdock-receptor/receptor.pdb'):
        raise ValueError('De novo VS receptor differs from registered batch')
    return manifest, binding
