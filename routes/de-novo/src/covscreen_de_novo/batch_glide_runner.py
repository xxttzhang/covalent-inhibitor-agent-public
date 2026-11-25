"""Run a signed independent Glide recipe inside the shared instance process tree."""
import json
import subprocess
import time
import zipfile
from pathlib import Path


def run(root, registry, spec, tree=None):
    from covscreen.covdock_vs_runner import environment
    from covscreen.job_cancellation import requested
    from covscreen.jobs import sha256_file
    from .batch_prereaction_input import load
    from .batch_control_states import export as control_states
    from .batch_glide_recipe import commands, docking_input
    root = Path(root).resolve(strict=True)
    parameters = spec.parameters
    value, source = load(root, parameters['prereaction_source_sha256'], parameters['prereaction_manifest_sha256'])
    runtime = parameters['glide_runtime']
    for name, checksum in runtime['files'].items():
        if sha256_file(Path(name)) != checksum:
            raise ValueError('Approved Glide runtime changed')
    output = registry.job_dir(spec.run_id) / 'artifacts/prereaction'
    output.mkdir(parents=True, exist_ok=False)
    scratch = output / 'scratch'
    scratch.mkdir()
    env = environment({'runtime': runtime['paths']}, scratch)
    deadline = time.monotonic() + spec.resource_budget.max_runtime_minutes * 60
    history = []
    def execute(stage, command):
        with (output / (stage + '.log')).open('xb') as log:
            options = dict(cwd=output, env=env, stdin=subprocess.DEVNULL,
                stdout=log, stderr=subprocess.STDOUT, start_new_session=True)
            if tree is None:
                # The outer single-threaded supervisor owns this entire descendant tree.
                code = subprocess.run(command, timeout=max(0.01, deadline-time.monotonic()), **options).returncode
            else:
                code = tree.run(command, deadline=deadline, cancelled=lambda: requested(registry, spec.run_id),
                    cpu_count=spec.resource_budget.cpu_count, **options)
        history.append({'stage': stage, 'command': command, 'return_code': code})
        (output / 'steps.json').write_text(json.dumps(history, indent=2))
        if code:
            raise subprocess.CalledProcessError(code, command)
    recipe = commands(runtime['paths']['root'], source / 'reference/fresh_afatinib.sdf',
        cpu_count=spec.resource_budget.cpu_count, input_count=spec.resource_budget.max_candidates)
    execute('control_ligprep', recipe['control_ligprep'])
    controls = control_states(output / 'control_states.mae', value['reference']['canonical_smiles'], output / 'controls')
    (output / 'control-states.json').write_text(json.dumps(controls, indent=2))
    (output / 'prereaction_grid.in').write_bytes((source / 'prereaction_grid.in').read_bytes())
    execute('grid', recipe['grid'])
    grid = output / 'prereaction_grid.zip'
    if grid.is_symlink() or not grid.is_file():
        raise ValueError('Glide did not produce the expected grid')
    with zipfile.ZipFile(grid) as archive:
        if not archive.namelist() or archive.testzip() is not None:
            raise ValueError('Glide grid archive is empty or corrupt')
    candidates = [row for row in value['states'] if row['state'] == 'PREPARED_NOT_DOCKED']
    states = [*candidates, *controls['states']]
    if len(states) > spec.resource_budget.max_candidates:
        raise ValueError('Prepared Glide states exceed approved budget')
    with (output / 'fresh_selected.sdf').open('xb') as stream:
        for row in candidates:
            stream.write((source / row['file']).read_bytes())
        for row in controls['states']:
            stream.write((output / 'controls' / row['file']).read_bytes())
    (output / 'independent_prereaction.in').write_text(docking_input(grid, output / 'fresh_selected.sdf'))
    recipe = commands(runtime['paths']['root'], source / 'reference/fresh_afatinib.sdf',
        cpu_count=spec.resource_budget.cpu_count, input_count=len(states))
    execute('docking', recipe['dock'])
    raw = sorted(output.glob('independent_prereaction-*_raw.maegz'))
    if not 1 <= len(raw) <= len(states) or any(p.is_symlink() or not p.is_file() or not p.stat().st_size for p in raw):
        raise ValueError('Glide did not produce bounded original subjob pose outputs')
    receipt = {'schema': 'denovo-batch-glide-execution-v1', 'run_id': spec.run_id,
        'state': 'FINISHED_REQUIRES_INDEPENDENT_EVALUATION', 'states': states,
        'source_manifest_sha256': parameters['prereaction_manifest_sha256'],
        'raw_outputs': [{'path': str(path.relative_to(root)), 'sha256': sha256_file(path)} for path in raw],
        'grid_sha256': sha256_file(grid), 'scientific_acceptance': False, 'steps': history}
    (output / 'execution.json').write_text(json.dumps(receipt, indent=2, allow_nan=False))
    return 0


if __name__ == '__main__':
    import argparse
    from covscreen.jobs import ArtifactRegistry
    from covscreen.instance_glide_claim import validate
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--run-id', required=True)
    args = parser.parse_args()
    registry = ArtifactRegistry(args.root)
    spec = registry.load_spec(args.run_id)
    validate(args.root, spec, 'asgbie3')
    raise SystemExit(run(args.root, registry, spec))
