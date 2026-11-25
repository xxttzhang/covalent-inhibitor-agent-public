#!/usr/bin/env python3
"""Orchestrate registered receptor repair, independent audits and scoped admission."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
import traceback

from covscreen.receptor_admission import admit_isolated_receptor
from covscreen.receptor_chirality_plan import build_chirality_plan
from covscreen.receptor_geometry import audit_receptor_geometry, audit_restrained_geometry
from covscreen.receptor_quality import audit_relaxed_receptor, audit_restrained_receptor
from covscreen.receptor_relaxation import RELAXATION_POLICY_V2
from covscreen.receptor_input import load_receptor_preparation_directory


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, value):
    with Path(path).open('x') as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.write('\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--receptor-input-dir', type=Path, required=True)
    parser.add_argument('--manifest-sha256', required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    parser.add_argument('--openmm-python', type=Path, required=True)
    parser.add_argument('--openmm-pythonpath', type=Path, required=True)
    parser.add_argument('--pdbfixer-site-packages', type=Path, required=True)
    parser.add_argument('--heavy-seed', type=int, required=True)
    parser.add_argument('--max-missing-residues', type=int, required=True)
    parser.add_argument('--altlocs-json', required=True)
    parser.add_argument('--relaxation-policy', choices=('v1', 'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'), default='v1')
    parser.add_argument('--relaxation-policy-json')
    args = parser.parse_args()
    restrained = args.relaxation_policy in {'v2', 'v2-local', 'v2-local-bounded', 'v2-local-block'}
    selected_policy = RELAXATION_POLICY_V2
    if args.relaxation_policy in {'v2-local', 'v2-local-bounded', 'v2-local-block'}:
        from covscreen.receptor_local_policy import validate_policy_version
        selected_policy = json.loads(args.relaxation_policy_json or 'null')
        validate_policy_version(selected_policy, args.relaxation_policy)
    elif args.relaxation_policy_json is not None:
        raise ValueError('Custom policy bytes require the explicit local policy version')
    root = args.project_root.resolve()
    loaded = load_receptor_preparation_directory(root, args.receptor_input_dir, args.manifest_sha256)
    acquisition = loaded['acquisition_directory']
    if (not args.openmm_python.is_file() or not os.access(args.openmm_python, os.X_OK)
            or not args.openmm_pythonpath.is_dir()
            or not args.pdbfixer_site_packages.is_dir()):
        raise ValueError('Registered OpenMM/PDBFixer runtime is unavailable')
    target_id, fasta = loaded['target_artifact_id'], loaded['canonical_fasta']
    sequence, taxon, acquired = loaded['canonical_sequence'], loaded['taxon'], loaded['acquisition']
    mapping, = acquired['acquisition']['mappings']
    entity = acquisition / f"entity_{mapping['entity_id']}.json"
    instance = acquisition / f"chain_{mapping['label_asym_id']}.json"
    source = acquisition / 'structure.pdb'
    altlocs = json.loads(args.altlocs_json)
    if (not isinstance(altlocs, dict) or any(not isinstance(k, str) or not isinstance(v, str)
                                             for k, v in altlocs.items())):
        raise ValueError('altlocs-json must map author residue identifiers to one-character labels')
    args.output_dir.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    state = {'schema': 'receptor-preparation-pipeline-v1', 'status': 'running',
        'target_acquisition_sha256': args.manifest_sha256, 'target_artifact_id': target_id,
        'input_source_kind': loaded['source_kind'], 'input_source_route': loaded['source_route'],
        'heavy_seed': args.heavy_seed, 'max_missing_residues': args.max_missing_residues,
        'altlocs': altlocs, 'phases': [], 'receptor_prepared': False,
        'campaign_ready': False, 'artifacts': {}}
    if restrained:
        state.update(schema='receptor-preparation-pipeline-v2',
            observed_motion_acceptance='not_evaluated', relaxation_policy=selected_policy)
    worker = Path(__file__).with_name('receptor_preparation_worker.py')
    source_root = Path(__file__).resolve().parents[3] / 'science/src'
    environment = {key: value for key, value in os.environ.items()
                   if key in {'HOME', 'PATH', 'LANG', 'LC_ALL'}}
    environment.update(PYTHONDONTWRITEBYTECODE='1', OPENMM_CPU_THREADS='1',
        OMP_NUM_THREADS='1', OPENBLAS_NUM_THREADS='1', MKL_NUM_THREADS='1',
        NUMEXPR_NUM_THREADS='1', PYTHONPATH=os.pathsep.join(
            [str(source_root), str(args.openmm_pythonpath), str(args.pdbfixer_site_packages)]))
    def run_phase(mode, request, output, timeout):
        request_path = args.output_dir / f'{mode}-request.json'
        write(request_path, request)
        log = args.output_dir / f'{mode}.log'
        with log.open('x') as stream:
            subprocess.run([str(args.openmm_python), '-B', str(worker), '--mode', mode,
                '--request', str(request_path), '--output', str(output)], cwd=root,
                env=environment, stdout=stream, stderr=subprocess.STDOUT,
                check=True, timeout=timeout)
        state['phases'].append({'phase': mode, 'status': 'completed',
            'request_sha256': sha(request_path), 'log_sha256': sha(log)})
    try:
        heavy = args.output_dir / 'heavy'
        run_phase('heavy', {'source': str(source), 'source_sha256': sha(source),
            'entity': str(entity), 'instance': str(instance), 'fasta': str(fasta),
            'uniprot': mapping['uniprot'], 'cys': mapping['uniprot_residue'],
            'taxon': taxon, 'altlocs': altlocs,
            'model': acquired['selection'].get('model'), 'seed': args.heavy_seed,
            'max_missing_residues': args.max_missing_residues}, heavy, 240)
        heavy_receipt = sha(heavy / 'receipt.json')
        plan = build_chirality_plan(heavy, heavy_receipt, source)
        plan_path = args.output_dir / 'chirality-plan.json'
        write(plan_path, plan)
        state['phases'].append({'phase': 'chirality_plan', 'status': 'completed',
                                'artifact_sha256': sha(plan_path)})
        relaxation = args.output_dir / 'relaxation'
        relaxation_parameters = {}
        if restrained:
            relaxation_parameters = {'relaxation_policy': selected_policy,
                'relaxation_policy_sha256': hashlib.sha256(json.dumps(selected_policy,
                    sort_keys=True, separators=(',', ':'), allow_nan=False).encode()).hexdigest()}
        run_phase('relax', {'parent': str(heavy), 'parent_receipt_sha256': heavy_receipt,
            'chirality_plan': str(plan_path), 'chirality_plan_sha256': sha(plan_path),
            'source': str(source), **relaxation_parameters}, relaxation, 720)
        relaxation_receipt = sha(relaxation / 'receipt.json')
        quality = (audit_restrained_receptor if restrained else audit_relaxed_receptor)(relaxation, relaxation_receipt, heavy, source)
        quality_path = args.output_dir / 'identity-quality.json'
        write(quality_path, quality)
        state['phases'].append({'phase': 'identity_quality', 'status': quality['status'],
                                'artifact_sha256': sha(quality_path)})
        if restrained:
            geometry = audit_restrained_geometry(relaxation, relaxation_receipt,
                quality_path, sha(quality_path), source, heavy)
        else:
            geometry = audit_receptor_geometry(relaxation, relaxation_receipt,
                quality_path, sha(quality_path), source)
        geometry_path = args.output_dir / 'geometry-quality.json'
        write(geometry_path, geometry)
        state['phases'].append({'phase': 'geometry_quality', 'status': geometry['status'],
                                'artifact_sha256': sha(geometry_path)})
        if restrained:
            if geometry['status'] != 'geometry_passed_environment_review_pending':
                raise ValueError('Restrained receptor geometry did not pass')
            state.update(status='observed_motion_review_pending', receptor_prepared=False)
            return
        prepared = args.output_dir / 'prepared'
        admitted = admit_isolated_receptor(acquisition, args.manifest_sha256, sequence,
            taxon, heavy, relaxation, relaxation_receipt,
            quality_path, sha(quality_path), geometry_path, sha(geometry_path), prepared)
        state.update(status='prepared_isolated_protein_scope', receptor_prepared=True,
            prepared_manifest_sha256=sha(prepared / 'prepared-receptor.json'),
            prepared_scope=admitted['prepared_scope'])
        state['phases'].append({'phase': 'admission', 'status': admitted['status'],
            'artifact_sha256': state['prepared_manifest_sha256']})
    except Exception as exc:
        state.update(status='preparation_failed', error=f'{type(exc).__name__}: {exc}',
                     traceback=traceback.format_exc())
        raise
    finally:
        state['elapsed_wall_seconds'] = time.monotonic() - started
        state['artifacts'] = {str(path.relative_to(args.output_dir)): sha(path)
            for path in args.output_dir.rglob('*') if path.is_file()
            and path.name != 'pipeline.json'}
        write(args.output_dir / 'pipeline.json', state)


if __name__ == '__main__':
    main()
