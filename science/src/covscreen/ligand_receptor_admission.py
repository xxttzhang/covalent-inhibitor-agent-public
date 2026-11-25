"""Materialize a reviewed v2 proposal without launching scientific computation."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile

from .jobs import sha256_file
from .ligand_receptor_evidence import freeze_review
from .receptor_environment_review import load as load_review
from .receptor_admission import admit_restrained_receptor
from .receptor_site_context import assess_receptor_site_context
from .receptor_evidence import regular


def materialize(root, state, record, evaluation, loaded, pipeline):
    """Caller independently replays the current registered proposal first."""
    witness = freeze_review(root, evaluation)
    inventory = {witness['path']: witness['manifest_sha256']}
    bound = {'workflow_id': state['workflow_id'], 'run_id': record['run_id'],
        'evaluation_sha256': witness['manifest_sha256']}
    ref = record['motion_review']
    judgment = load_review(root, ref, bound, inventory, scope='restrained-receptor')
    if judgment['decision'] != 'suitable_for_exploratory_receptor_preparation':
        return evaluation
    acquisition_sha = evaluation['parents']['acquisition_sha256']
    receipt_path = pipeline / 'pipeline.json'
    regular(root, receipt_path, evaluation['pipeline_sha256'])
    receipt = json.loads(receipt_path.read_text())
    upstream = {str((pipeline / name).relative_to(root)): digest
        for name, digest in receipt['artifacts'].items()}
    upstream[str(receipt_path.relative_to(root))] = evaluation['pipeline_sha256']
    for name, digest in upstream.items():
        regular(root, root / name, digest)
    quality, geometry = pipeline / 'identity-quality.json', pipeline / 'geometry-quality.json'
    with tempfile.TemporaryDirectory(prefix='reviewed-receptor-') as temporary:
        stage = Path(temporary)
        prepared = stage / 'prepared'
        manifest = admit_restrained_receptor(root, ref, bound, inventory,
            loaded['acquisition_directory'], acquisition_sha, loaded['canonical_sequence'], loaded['taxon'],
            pipeline / 'heavy', pipeline / 'relaxation', sha256_file(pipeline / 'relaxation/receipt.json'),
            quality, sha256_file(quality), geometry, sha256_file(geometry), prepared)
        context = assess_receptor_site_context(loaded['acquisition_directory'], acquisition_sha,
            loaded['acquisition'], prepared, sha256_file(prepared / 'prepared-receptor.json'),
            stage / 'context', review_root=root)
        files = {str(p.relative_to(stage)): sha256_file(p) for p in stage.rglob('*') if p.is_file()}
        identity = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
        destination = root / 'state/reviewed-receptors' / identity
        if any(p.is_symlink() for p in (root / 'state', destination.parent, destination)):
            raise ValueError('Reviewed receptor cannot traverse symbolic links')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if not destination.exists():
            staging = Path(tempfile.mkdtemp(prefix='.staging-', dir=destination.parent))
            try:
                shutil.copytree(stage, staging, dirs_exist_ok=True)
                staging.rename(destination)
            finally:
                if staging.exists():
                    shutil.rmtree(staging)
        if {str(p.relative_to(destination)) for p in destination.rglob('*') if p.is_file()} != set(files):
            raise ValueError('Reviewed receptor inventory differs')
        for name, digest in files.items():
            regular(root, destination / name, digest)
    # Recheck the scientific judgment after expensive replay and publication.
    load_review(root, ref, bound, inventory, scope='restrained-receptor')
    output = json.loads(json.dumps(evaluation))
    lineage = {str((destination / name).relative_to(root)): digest for name, digest in files.items()}
    lineage.update(inventory)
    lineage.update(upstream)
    for name, digest in upstream.items():
        regular(root, root / name, digest)
    lineage[ref['path']] = ref['manifest_sha256']
    output.update(receptor_prepared=True, prepared_scope=manifest['prepared_scope'],
        output_validation='REVIEWED_RESTRAINED_RECEPTOR_REPLAYED', proposal_review=witness,
        motion_review=ref)
    output['result'].update(status='REVIEWED_RESTRAINED_RECEPTOR_REPLAYED',
        identity=context['identity'], context=context, inventory=lineage,
        scope=manifest['prepared_scope'],
        receptor={'path': str((destination / 'prepared/prepared-receptor.pdb').relative_to(root)),
            'sha256': files['prepared/prepared-receptor.pdb']},
        limitations=['Position-restrained computational proposal, not experimental coordinates.',
            'Agent review does not establish biological suitability, reactivity or MD readiness.'])
    output['parents'].update(reviewed_receptor_path=str(destination.relative_to(root)),
        context_artifact_id='reviewed-receptor-context-' + files['context/site-context.json'][:32],
        context_path=str((destination / 'context/site-context.json').relative_to(root)),
        context_sha256=files['context/site-context.json'])
    return output
