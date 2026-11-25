"""Reinspect and gate one retained candidate without LigPrep or CovDock execution.

The interrupted parent remains terminal. Every original pose is copied byte for
byte; a new candidate-level manifest describes this readback, never an invented
successful engine return code.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from covscreen.covdock_retained import validate_retained_candidate, selection_inspection
from covscreen.covdock_gate import gate_covdock_candidates
from covscreen.jobs import sha256_file, utc_now
from run_covdock_lo_registered import prepare_identity_and_sdf
from parse_covdock_scores import parse_inspection


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--project-root', type=Path, required=True)
    parser.add_argument('--descriptor', type=Path, required=True)
    parser.add_argument('--expected-descriptor-sha256', required=True)
    parser.add_argument('--schrodinger-root', type=Path, required=True)
    parser.add_argument('--output-dir', type=Path, required=True)
    args = parser.parse_args()
    if sha256_file(args.descriptor) != args.expected_descriptor_sha256:
        raise ValueError('Retained descriptor changed')
    verified = validate_retained_candidate(args.project_root, args.descriptor)
    source = verified['files']['pose_file']
    data = verified['descriptor']
    spec = json.loads(verified['files']['parent_spec'].read_text())
    parameters = spec['parameters']
    output = args.output_dir.resolve()
    output.relative_to(args.project_root.resolve())
    parent = verified['files']['parent_spec'].parent
    if output == parent or parent in output.parents:
        raise ValueError('Reparse output must not modify the interrupted parent')
    output.mkdir(parents=True, exist_ok=False)
    candidate = output / 'candidates.parquet'
    verified['candidate'].to_parquet(candidate, index=False)
    identity = output / 'af3_top_identity.parquet'
    sdf = output / 'af3_top_for_ligprep.sdf'
    prepare_identity_and_sdf(candidate, identity, sdf, 'af3')
    pose = output / 'covdock_lo/library_first_af3_top_covdock_lo-out.maegz'
    pose.parent.mkdir()
    shutil.copyfile(source, pose)
    if sha256_file(pose) != sha256_file(source):
        raise ValueError('Retained pose copy changed')
    inspection = output / 'inspection'
    inspection.mkdir()
    prefix = inspection / 'retained_raw'
    chain, residue = parameters['attachment_residue'].split(':')
    env = os.environ.copy()
    for key in ('LD_LIBRARY_PATH', 'LD_PRELOAD', 'LD_AUDIT', 'PYTHONHOME', 'PYTHONPATH'):
        env.pop(key, None)
    env['SCHRODINGER'] = str(args.schrodinger_root.resolve())
    command = [str(args.schrodinger_root.resolve() / 'run'), 'python3',
        str(Path(__file__).with_name('inspect_covdock_output.py')),
        '--input', str(pose), '--output-prefix', str(prefix),
        '--attachment-chain', chain, '--attachment-residue', residue,
        '--nucleophilic-atom', parameters.get('nucleophilic_atom', 'SG'),
        '--severe-clash-distance', str(parameters['severe_clash_distance'])]
    with (inspection / 'readback.log').open('w') as log:
        subprocess.run(command, env=env, stdout=log, stderr=subprocess.STDOUT, check=True)
    raw = json.loads(prefix.with_suffix('.json').read_text())
    if len(raw['rows']) != data['pose_count']:
        raise ValueError('SDK readback lost retained poses')
    for index, row in enumerate(raw['rows']):
        if row['pose_index'] != index or not row['title'].endswith(data['title']):
            raise ValueError('SDK readback identity mismatch')
    # The unchanged raw audit preserves all measurements and failure values.
    # Only the derived selection input masks the vendor failure sentinel.
    selection, invalid = selection_inspection(raw)
    usable = inspection / 'retained_selection_input.json'
    usable.write_text(json.dumps(selection, indent=2, allow_nan=False))
    parsed = parse_inspection(usable, identity, 'r_cdock_cdock_affinity',
                              'lower_is_better', engine='schrodinger-covdock-lo')
    if len(parsed) != 1 or parsed.iloc[0]['compound_id'] != data['compound_id']:
        raise ValueError('Retained candidate parsing lost identity')
    parsed['covdock_source_parent_run_id'] = data['parent_run_id']
    parsed['covdock_source_kind'] = 'interrupted_run_candidate_readback'
    parsed['covdock_retained_pose_count'] = data['pose_count']
    scores = output / 'covdock_lo_all_scores.parquet'
    parsed.to_parquet(scores, index=False)
    parsed.to_csv(scores.with_suffix('.csv'), index=False)
    options = {key: parameters[key] for key in ('max_local_strain', 'hard_max_strain',
        'min_covalent_bond_length', 'max_covalent_bond_length', 'max_pose_rmsd', 'min_success_rate')}
    gate = gate_covdock_candidates(parsed, output, final_n=1, **options)
    # Recheck all source evidence after SDK processing before issuing a
    # manifest. This is a candidate reparse, not parent-run completion.
    validate_retained_candidate(args.project_root, args.descriptor)
    files = [p for p in output.rglob('*') if p.is_file()]
    manifest = {'schema_version': '1.0', 'status': gate['status'],
        'execution_kind': 'retained_candidate_reparse', 'generated_at': utc_now(),
        'parent_run_id': data['parent_run_id'], 'parent_state': 'TIMED_OUT',
        'new_docking_or_ligprep_executed': False,
        'input_count': 1, 'original_candidate_count': verified['original_candidate_count'],
        'input_candidate_table': str(candidate), 'input_candidate_table_sha256': sha256_file(candidate),
        'retained_descriptor': str(args.descriptor.resolve()),
        'retained_descriptor_sha256': verified['descriptor_sha256'],
        'pose_files': [str(pose)], 'pose_sha256': sha256_file(pose),
        'retained_pose_count': data['pose_count'], 'sentinel_pose_indices': invalid,
        'inspection_files': [str(usable)], 'raw_inspection_files': [str(prefix.with_suffix('.json'))],
        'gate_quality_gates': gate['quality_gates'], 'gate_quality': gate['quality'],
        'score_property': 'r_cdock_cdock_affinity', 'score_direction': 'lower_is_better',
        'full_workflow_complete': False,
        'outputs': {str(p.relative_to(output)): {'path': str(p), 'sha256': sha256_file(p)} for p in files}}
    with (output / 'covdock_lo_registered_manifest.json').open('x') as stream:
        json.dump(manifest, stream, indent=2)
    print(json.dumps(manifest, indent=2))
    return 0 if manifest['status'] == 'completed' else 4


if __name__ == '__main__':
    raise SystemExit(main())
