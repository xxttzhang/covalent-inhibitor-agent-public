"""Current Ligand study to the shared portable MD candidate delivery contract."""
import hashlib
import json
from pathlib import Path
import shutil
from uuid import uuid4

from .execution import JobManager
from .jobs import sha256_file
from .md_review_delivery import PROFILE, freeze, md_not_evaluated
from .md_candidate_delivery import load_shared_candidate_delivery
from .receptor_evidence import regular


def prepare(root, state):
    """Caller freshly observes/reviews CovDock under the workflow lock."""
    record = state['covdock_execution']
    if record.get('evaluation_verification') != 'verified' or record.get('evidence_error'):
        raise ValueError('MD handoff requires a verified current CovDock readback')
    manager = JobManager(root)
    af3 = state.get('af3_execution') or {}
    if af3.get('run_id'):
        from .ligand_af3_execution import observe, review, reject_evidence
        stage = state['stage']
        try:
            output = observe(root, state, af3['run_id'])
            review(root, state, output)
        except (ValueError, KeyError, TypeError, OSError) as error:
            reject_evidence(state, error)
            raise
        finally:
            state['stage'] = stage
    handoff = state['refinement_input']
    prepared = handoff['receptor_preparation']['evaluation']
    parents = prepared['parents']
    table, _ = regular(root, root/handoff['candidate_table'], handoff['input_sha256'])
    receptor, _ = regular(root, manager.registry.job_dir(prepared['run_id']) /
        'artifacts/receptor-preparation/prepared/prepared-receptor.json', parents['prepared_sha256'])
    context, _ = regular(root, root/'state/receptor_site_context'/parents['context_artifact_id'][17:] /
                         'site-context.json', parents['context_sha256'])
    spec = manager.registry.load_spec(record['run_id'])
    source = {'schema': PROFILE, 'route': 'ligand-first', 'workflow_id': state['workflow_id'],
        'design_id': state['design_id'], 'evidence_id': state['evidence_id'],
        'task_id': record['run_id'], 'candidate_artifact_id': handoff['artifact_id'],
        'target_artifact_id': handoff['target']['artifact_id'],
        'reaction_type': spec.parameters['reaction_type'],
        'receptor_sha256': spec.parameters['covdock_receptor_input_check']['receptor_sha256'],
        'covdock': record['evaluation'], 'af3': af3.get('evaluation') if af3.get('run_id') else None,
        'producer_source_sha256': {name: sha256_file(Path(__file__).parent/name) for name in
            ('ligand_md_handoff.py', 'md_review_delivery.py', 'candidate_delivery.py', 'md_candidate_pose.py', 'metric_contracts.py')}}
    identity = {'source': source, 'candidate_table_sha256': sha256_file(table),
                'prepared_receptor_sha256': sha256_file(receptor), 'receptor_context_sha256': sha256_file(context)}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, allow_nan=False).encode()).hexdigest()
    parent = root/'results/ligand-md-handoffs'
    destination = parent/digest[:32]
    for path in (root/'results', parent, destination):
        if path.is_symlink():
            raise ValueError('MD handoff cannot traverse symbolic links')
    previous = record.get('md_handoff')
    path = destination/'candidate-results.json'
    if previous:
        if previous['artifact_id'] != 'ligand-md-handoff-'+digest[:32] or previous['path'] != str(path.relative_to(root)):
            raise ValueError('MD handoff differs from its previously frozen study evidence')
        regular(root, path, previous['manifest_sha256'])
    def resolve(name):
        method, relative = name.split('/', 1)
        evaluation = source[method]
        job = manager.registry.job_dir(evaluation['run_id'])/'artifacts'
        return regular(root, job/relative, evaluation['output_sha256'][relative])[0]
    if not destination.exists():
        staging = parent/('.'+digest[:32]+'-'+uuid4().hex[:12])
        try:
            freeze(source, table, receptor, context, staging, resolve)
            # Revalidate bytes copied from the live evidence before publishing.
            for evaluation in (source['covdock'], source['af3']):
                if evaluation:
                    for row in evaluation['candidates']:
                        if row['structure']:
                            resolve(('covdock' if evaluation is source['covdock'] else 'af3')+'/'+row['structure']['path'])
            load_shared_candidate_delivery(staging/'candidate-results.json', sha256_file(staging/'candidate-results.json'),
                expected_route='ligand-first', expected_task_id=source['task_id'])
            staging.rename(destination)
        except BaseException:
            if staging.exists():
                shutil.rmtree(staging)
            raise
    loaded = load_shared_candidate_delivery(path, sha256_file(path),
        expected_route='ligand-first', expected_task_id=source['task_id'])
    if (json.loads((destination/'source.json').read_text()) != source
            or loaded['manifest']['parents'] != {'candidate_table': identity['candidate_table_sha256'],
                'prepared_receptor': identity['prepared_receptor_sha256'],
                'receptor_context': identity['receptor_context_sha256']}):
        raise ValueError('Existing MD handoff does not match current verified sources')
    return {'artifact_id': 'ligand-md-handoff-'+digest[:32], 'manifest_sha256': sha256_file(path),
        'path': str(path.relative_to(root)), 'route': 'ligand-first', 'task_id': source['task_id'],
        'candidate_count': loaded['manifest']['candidate_count'],
        'pose_selection_assessment': loaded['manifest']['pose_selection_assessment'],
        'md_admission': md_not_evaluated(), 'compute_submitted_by_handoff': False}
