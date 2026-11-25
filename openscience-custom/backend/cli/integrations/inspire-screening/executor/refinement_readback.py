"""Read frozen refinement artifacts, without running engines or accepting quality."""
import argparse
import hashlib
import json
import sys
from pathlib import Path

import refinement_stage


def inspect(root, parent, task, expected, witness=None):
    staged = refinement_stage.inspect(root, parent, task, expected)
    root = Path(root).resolve(strict=True)
    contents = refinement_stage.read(root, f'tasks/{parent}/refinements/{task}/plan.json')
    if hashlib.sha256(contents).hexdigest() != staged['plan_sha256']:
        raise ValueError('Plan changed during output readback')
    plan = json.loads(contents)['plan']
    release = root / 'releases' / plan['code']['release_id']
    # This controller is executed only from the verified immutable release.
    if Path(__file__).resolve() != release / 'refinement_readback.py':
        raise ValueError('Readback controller must come from the frozen release')
    if plan['assets'].get(str(release / 'refinement_readback.py')) != hashlib.sha256(Path(__file__).read_bytes()).hexdigest():
        raise ValueError('Frozen readback controller changed')
    sys.path.insert(0, str(release / 'science/src'))
    from covscreen import refinement_output, refinement_input
    for module in (refinement_output, refinement_input):
        if Path(module.__file__).resolve() != release / 'science/src/covscreen' / (module.__name__.split('.')[-1] + '.py'):
            raise ValueError('Output verifier imported from another code version')
    output = Path(staged['output'])
    if output.resolve(strict=True) != output:
        raise ValueError('Output directory cannot be redirected')
    relative = str(output.relative_to(root) / refinement_output.MANIFEST)
    saved = refinement_stage.read(root, relative)
    checksum = hashlib.sha256(saved).hexdigest()
    if witness is not None and checksum != refinement_stage.identifier(witness, r'[a-f0-9]{64}'):
        raise ValueError('Result manifest differs from independently retained witness')
    contract = {'input_count': len(plan['input']['candidates']), 'input_sha256': plan['input']['sha256'],
                'identity_sha256': plan['input']['identity_sha256'], 'verified': True, 'grants_resource_approval': False}
    options = ({'expected_gate_version': 'covdock-lo-pose-gate-v3.0-provisional'}
               if plan['engine']['name'] == 'covdock' and
               plan.get('quality_policy', {}).get('version') == 'refinement-structural-qc-v2-provisional' else {})
    evidence = refinement_output.read_refinement_outputs(output, plan['engine']['name'], contract, checksum, **options)
    if plan['engine']['name'] == 'af3':
        refinement_output.verify_af3_confidence(output, evidence,
            protein_chain=plan['runtime']['protein_chain'], ligand_chain=plan['runtime']['ligand_chain'])
    if plan['engine']['name'] == 'covdock' and plan.get('result_contract') == 'screening-refinement-outcome-v2':
        refinement_output.verify_covdock_structures(output, evidence, runtime=plan['runtime'],
            script=release / 'science/scripts/inspect_covdock_output.py', assets=plan['assets'])
    geometry = None
    if plan.get('geometry_contract') == 'af3-geometry-evidence-v1':
        target = release / 'science/configs/covindb/EGFR_C797_mvp_v1.yaml'
        geometry = refinement_output.verify_af3_geometry(output, evidence, candidate_table=plan['input']['path'],
            target_config=target, target_sha256=plan['assets'][str(target)], runtime=plan['runtime'])
    quality = None
    if 'quality_policy' in plan:
        if (plan.get('result_contract') != 'screening-refinement-outcome-v2' or
                plan['engine']['name'] == 'af3' and geometry is None):
            raise ValueError('Structural QC requires actual classified geometry readback')
        from covscreen import refinement_quality
        helper = release / 'science/src/covscreen/refinement_quality.py'
        if Path(refinement_quality.__file__).resolve() != helper:
            raise ValueError('Quality measurements imported from another code version')
        quality = refinement_quality.read_measurements(output, evidence, plan['quality_policy'])
    # Recheck the parent/input chain after the potentially longer artifact read.
    if refinement_stage.inspect(root, parent, task, expected) != staged:
        raise ValueError('Frozen inputs changed during output verification')
    return {'version': 'refinement-readback-v1', 'id': task, 'digest': expected,
            'code': plan['code'], 'selection_id': plan['selection_id'],
            'selection_manifest_digest': plan['selection_manifest_digest'],
            'output': str(output), 'manifest_sha256': checksum, 'evidence': evidence,
            **({'geometry': geometry} if geometry is not None else {}),
            **({'quality': quality} if quality is not None else {})}


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('outputs',))
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--digest', required=True)
    parser.add_argument('--parent', required=True)
    parser.add_argument('--task', required=True)
    parser.add_argument('--manifest-sha256')
    args = parser.parse_args()
    print(json.dumps(inspect(args.root, args.parent, args.task, args.digest, args.manifest_sha256),
                     ensure_ascii=False, allow_nan=False), flush=True)
