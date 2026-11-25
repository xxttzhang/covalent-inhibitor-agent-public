"""Shared signed-registry to raw VS products, independent of route QC policy."""
import json
import os
from pathlib import Path
import subprocess
import sys

from rdkit import rdBase

from .covdock_vs import molecules
from .covdock_vs_output import read
from .covdock_vs_preparation import revalidate, inspect_prepared
from .jobs import sha256_file
from .md_preparation_evidence import registered_output


def products(root, state, manager, spec, result, *, reaction_site_rejections=None):
    from .source_release import verify_source, historical_release
    from .native_approval import verify_spec
    verify_spec(spec)
    recovery = (reaction_site_rejections is not None and spec.route == 'de-novo'
        and result.state == 'FAILED' and result.return_code is None
        and result.failure_reason == 'ValueError: VS target SG is not attached to the declared beta carbon')
    if reaction_site_rejections is not None and spec.route != 'de-novo':
        raise ValueError('Per-pose reaction rejection is enabled only for De novo')
    if recovery:
        historical_release(spec.provenance['source_release'])
    else:
        verify_source(spec)
    if (spec.engine != 'schrodinger-covdock-vs' or spec.route not in {'ligand-first', 'library-first', 'de-novo'}
        or spec.project_id != state['workflow_id'] or spec.run_id != state['vs_execution']['run_id']
        or (not recovery and (result.state != 'COMPLETED' or result.return_code != 0))
        or any(getattr(result, key) != getattr(spec, key) for key in
               ('run_id', 'project_id', 'engine', 'route', 'attempt', 'parent_run_id'))):
        raise ValueError('VS evaluation requires its completed registered current-study task')
    # Historical recipe verification runs only its read-only preparation checker.
    historical_witness = None
    if recovery:
        release = Path(spec.provenance['source_release']['root'])
        paths = [release / 'science/src', *(release / 'routes' / route / 'src'
            for route in ('de-novo', 'ligand-first', 'library-first'))]
        checked = subprocess.run([sys.executable, '-c',
            'import json,sys; from pathlib import Path; '
            'from covscreen.jobs import ArtifactRegistry; '
            'from covscreen.source_release import verify_source; '
            'from covscreen.covdock_vs_preparation import revalidate,inspect_prepared; '
            'root=Path(sys.argv[1]); registry=ArtifactRegistry(root); spec=registry.load_spec(sys.argv[2]); '
            'verify_source(spec); packet=revalidate(root,spec.parameters["vs_preparation"]); '
            'print(json.dumps(inspect_prepared(root,packet,registry.job_dir(spec.run_id)/"artifacts/prepared")))',
            str(root), spec.run_id], check=True, capture_output=True, text=True, timeout=60,
            env={**os.environ, 'PYTHONPATH': os.pathsep.join(map(str, paths)),
                 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONNOUSERSITE': '1'})
        historical_witness = json.loads(checked.stdout)
        packet = spec.parameters['vs_preparation']
    else:
        packet = revalidate(root, spec.parameters['vs_preparation'])
    source = packet['plan']['source']
    ref = state['vs_input']
    if (source['route'] != spec.route or source['workflow_id'] != state['workflow_id']
        or source['manifests'].get(ref['directory'] + '/input.json') != ref['manifest_sha256']):
        raise ValueError('VS products do not bind the current study input')
    output = manager.registry.job_dir(spec.run_id) / 'artifacts'
    hashes = {}
    def registered(name):
        path = registered_output(root, output, spec, result, name)
        hashes[str(path.relative_to(root))] = sha256_file(path)
        return path
    execution = json.loads(registered('vs-execution.json').read_text())
    if any(execution.get(key) != value for key, value in {
        'schema': 'covdock-vs-execution-result-v1', 'run_id': spec.run_id,
        'engine': spec.engine, 'mode': 'enrichment', 'state': 'FAILED' if recovery else 'COMPLETED'}.items()):
        raise ValueError('VS execution receipt differs from the registered task')
    expected = 'docking/vs_' + sha256_file(manager.registry.job_dir(spec.run_id) / 'spec.json')[:20] + '-out.maegz'
    if recovery and (execution.get('error_type') != 'ValueError'
            or execution.get('error') != 'VS target SG is not attached to the declared beta carbon'
            or execution.get('steps') != [{'stage': stage, 'return_code': 0}
                for stage in ('prepwizard', 'ligprep', 'covdock_vs')]):
        raise ValueError('VS recovery requires all vendor stages successful and only the recorded reaction-site readback failure')
    name = expected if recovery else Path(execution['raw_output']['path']).relative_to(output.relative_to(root)).as_posix()
    if name != expected:
        raise ValueError('VS raw output does not have the original signed-job identity')
    raw = registered(name)
    if not recovery and sha256_file(raw) != execution['raw_output']['sha256']:
        raise ValueError('VS raw output differs from its execution receipt')
    receptor = registered('prepared/receptor.mae')
    prepared = registered('prepared/ligands.mae')
    witness = json.loads(registered('prepared-input-witness.json').read_text())
    if witness != (historical_witness if recovery else inspect_prepared(root, packet, output / 'prepared')):
        raise ValueError('VS prepared input witness differs from current registered inputs')
    rows, counts = read(raw, molecules(root / packet['plan']['original_ligands'], False),
        molecules(prepared, True), molecules(receptor, True)[0], packet['plan']['attachment_residue'],
        expected_sha256=sha256_file(raw), reaction_site_rejections=reaction_site_rejections)
    measured = {'schema': 'covdock-vs-output-identity-v1', 'rdkit_version': rdBase.rdkitVersion,
        'raw_sha256': sha256_file(raw), 'candidate_pose_counts': counts,
        'poses': [{key: row[key] for key in ('pose_index', 'candidate_id', 'input_index', 'score',
            'product_identity_verified', 'prepared_receptor_identity_verified')} for row in rows],
        'scientific_acceptance': False, 'md_ready': False}
    if reaction_site_rejections:
        measured['reaction_site_rejections'] = reaction_site_rejections
    if recovery:
        if not reaction_site_rejections:
            raise ValueError('Historical reaction-site failure was not reproduced')
        return rows, counts, hashes
    identity = json.loads(registered('vs-output-identity.json').read_text())
    if identity != measured:
        raise ValueError('VS raw structure re-read differs from registered identity measurements')
    return rows, counts, hashes
