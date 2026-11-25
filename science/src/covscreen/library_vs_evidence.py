"""Registered Library VS product assessment and immutable scientific reports."""
import hashlib
import json
from pathlib import Path

from .covdock_vs import bound_file
from .covdock_vs_evidence import products
from .covdock_vs_preparation import revalidate
from .jobs import sha256_file
from .library_vs_geometry import Policy, measure
from .library_vs_input import load as load_input
from .library_vs_prescreen_input import encoded
from .md_preparation_evidence import registered_output
from .ligand_vs_evidence import report


def implementations():
    return {name: sha256_file(Path(__file__).with_name(name)) for name in
        ('library_vs_evidence.py', 'library_vs_geometry.py', 'covdock_vs_evidence.py',
         'covdock_vs_output.py', 'covdock_vs_geometry.py')}


def evaluate(root, state, manager, spec, result):
    if spec.route != 'library-first':
        raise ValueError('Library VS evaluation requires its own route')
    payload = load_input(root, state)
    rows, counts, hashes = products(root, state, manager, spec, result)
    directory = root / state['vs_input']['directory']
    policy = Policy.model_validate(json.loads((directory / 'geometry-policy.json').read_text()))
    reference = json.loads((directory / 'reference-contact-sites.json').read_text())
    candidates = []
    for index, name in enumerate(payload['selected_candidate_ids']):
        measured, errors = [], []
        for row in (row for row in rows if row['candidate_id'] == name):
            try:
                measured.append(measure(row, reference, directory / 'receptor.pdb', payload['attachment_residue'], policy))
            except (ValueError, KeyError, TypeError, RuntimeError, FloatingPointError) as error:
                errors.append({'pose_index': row['pose_index'], 'error': str(error)})
        passed = [pose for pose in measured if pose['assessment']['review_eligible']]
        eligible = bool(passed) and not errors and len(measured) == counts[name]
        selected = min(passed, key=lambda pose: (float('inf') if pose['covdock_vs_score'] is None
            else pose['covdock_vs_score'], pose['pose_index'])) if eligible else None
        complete = bool(measured) and len(measured) == counts[name] and not errors and all(
            pose['assessment']['evidence_complete'] for pose in measured)
        candidates.append({'candidate_id': name, 'input_index': index, 'pose_count': counts[name],
            'status': 'PASS' if eligible else 'FAIL' if complete else 'UNKNOWN' if counts[name] else 'NOT_EVALUATED',
            'review_eligible': eligible, 'evidence_complete': complete, 'passing_pose_count': len(passed),
            'selected_pose_index': selected['pose_index'] if selected else None, 'poses': measured, 'errors': errors})
    for name, checksum in {**spec.input_sha256, **hashes}.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('Library VS evaluation source changed during measurement')
    return {'schema': 'library-vs-scientific-review-v1', 'engine': spec.engine, 'mode': 'enrichment',
        'run_id': spec.run_id, 'workflow_id': state['workflow_id'], 'candidate_count': len(candidates),
        'prescreen_run_id': payload['prescreen_run_id'], 'candidate_review': payload['candidate_review'],
        'candidates': candidates, 'passing_candidate_count': sum(item['review_eligible'] for item in candidates),
        'policy': policy.model_dump(mode='json'), 'reference_contact_sites': reference,
        'input_sha256': spec.input_sha256, 'output_sha256': hashes, 'implementation_sha256': implementations(),
        'output_validation': 'RAW_PRODUCT_IDENTITY_AND_LIBRARY_GEOMETRY_REPLAYED',
        'scientific_acceptance': False, 'md_ready': False,
        'limitations': ['Thresholds are provisional research QC, not calibrated activity or reactivity predictions.',
            'Reference N/O contact-site retention is a distance proxy, not assigned hydrogen bonding or proven energetic importance.',
            'Library molecules have no declared common scaffold; no reference scaffold RMSD gate or unrelated parent strain subtraction is applied.',
            'Candidate local relaxation energy is not global conformational strain or binding free energy; its provisional bound is not a vendor strain measurement.',
            'Each pose uses its own verified receptor and atom lineage, with receptor-only coordinate alignment.',
            'Scores only choose among jointly passing poses of the same candidate; they do not establish a calibrated cross-candidate activity ranking.',
            'Passing poses are eligible for further review. MD still requires exact pose/graph, parameterization and receptor/system preparation gates.']}


def review_reference(value):
    checksum = hashlib.sha256(encoded(value)).hexdigest()
    return {'path': 'results/library-vs-reviews/' + checksum[:32] + '/review.json',
        'manifest_sha256': checksum, 'artifact_id': 'library-vs-review-' + checksum[:32],
        'run_id': value['run_id'], 'passing_candidate_count': value['passing_candidate_count'], 'md_ready': False}


def save_review(root, value):
    ref = review_reference(value)
    path = root / ref['path']
    if any(p.is_symlink() for p in (root / 'results', path.parent.parent, path.parent, path)):
        raise ValueError('Library VS review cannot traverse symlinks')
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = encoded(value)
    if not path.exists():
        with path.open('xb') as stream:
            stream.write(raw)
    if path.read_bytes() != raw:
        raise ValueError('Library VS saved review changed')
    return ref


def load_review(root, state, manager, spec, result):
    from .source_release import verify_source
    from .native_approval import verify_spec
    verify_source(spec)
    verify_spec(spec)
    load_input(root, state)
    revalidate(root, spec.parameters['vs_preparation'])
    ref = state['vs_execution']['evaluation_artifact']
    path = bound_file(root, ref['path'])
    if sha256_file(path) != ref['manifest_sha256']:
        raise ValueError('Library VS saved evaluation checksum changed')
    value = json.loads(path.read_text())
    if (value.get('schema') != 'library-vs-scientific-review-v1'
        or spec.route != 'library-first' or spec.engine != 'schrodinger-covdock-vs'
        or spec.run_id != state['vs_execution']['run_id'] or spec.project_id != state['workflow_id']
        or result.state != 'COMPLETED' or result.return_code != 0
        or any(getattr(result, key) != getattr(spec, key) for key in
               ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))
        or value['run_id'] != spec.run_id or value['workflow_id'] != state['workflow_id']
        or value['input_sha256'] != spec.input_sha256 or value['implementation_sha256'] != implementations()
        or review_reference(value) != ref):
        raise ValueError('Library VS review no longer binds its original task and implementation')
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts'
    for name, checksum in value['output_sha256'].items():
        relative = Path(name).relative_to(folder.relative_to(root)).as_posix()
        registered_output(root, folder, spec, result, relative, checksum)
    for name, checksum in spec.input_sha256.items():
        if sha256_file(bound_file(root, name)) != checksum:
            raise ValueError('Library VS evaluated input changed')
    return value
