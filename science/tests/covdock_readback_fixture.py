"""Artificial CovDock outputs for protocol tests; never scientific evidence."""
import json
from pathlib import Path

import pandas as pd
from rdkit import Chem

from covscreen.covdock_gate import gate_covdock_candidates
from covscreen.covdock_receptor import read_effective_receptor
from covscreen.jobs import RunResult, sha256_file, utc_now
from covscreen.refinement_input import read_candidate_bytes
from covscreen.refinement_output import SPECS, freeze_refinement_outputs


def outputs(root, manager, spec, *, partial=True, all_failed=False, missing_strain=False, policy_override=None,
            producer_reader_version=None):
    folder = manager.registry.job_dir(spec.run_id) / 'artifacts'
    folder.mkdir(parents=True, exist_ok=True)
    for name in ('ligprep', 'covdock_lo'):
        (folder / name).mkdir(exist_ok=True)
    ligand = folder / 'ligprep/core_subset_ligprep.mae'
    ligand.write_text('ARTIFICIAL LIGPREP INPUT: NOT A SCIENTIFIC RESULT\n')
    invocation = folder / 'covdock_lo/library_first_core_subset_covdock_lo.run_manifest.json'
    prepared = invocation.with_name(invocation.name[:-len('.run_manifest.json')] + '_receptor_prepared.mae')
    source = root / spec.parameters['receptor']
    molecule = Chem.MolFromPDBFile(str(source), sanitize=False, removeHs=False)
    with Chem.MaeWriter(str(prepared)) as writer:
        writer.write(molecule)
    receipt = {'input_receptor_sha256': sha256_file(source), 'input_ligand_sha256': sha256_file(ligand),
        'attachment_residue': spec.parameters['attachment_residue'], 'reaction_type': spec.parameters['reaction_type'],
        'mode': 'leadopt', 'prepare_receptor': True, 'grid_sha256': None,
        'effective_receptor_sha256': sha256_file(prepared)}
    invocation.write_text(json.dumps(receipt))
    effective = read_effective_receptor(invocation, source, spec.parameters['attachment_residue'])
    input_check = dict(spec.parameters['covdock_receptor_input_check'])
    if producer_reader_version is not None:
        input_check['reader_version'] = producer_reader_version
        effective['reader_version'] = producer_reader_version
    pose = folder / 'covdock_lo/artificial_poses.maegz'
    pose.write_text('ARTIFICIAL MULTIPOSE CONTAINER: SDK BOUNDARY REPLACED IN TEST\n')
    audit_path = folder / 'covdock_lo/artificial_audit.json'
    table = root / spec.parameters['candidate_table']
    inputs = read_candidate_bytes(table, table.read_bytes()).to_dict(orient='records')
    rows, poses = [], []
    for position, item in enumerate(inputs):
        failed = all_failed or partial and position == len(inputs) - 1
        row = {key: item[key] for key in ('input_sdf_index', 'compound_id')}
        row.update(engine_status='FAILED' if failed else 'SUCCESS', raw_score=None if failed else -7.0-position,
            failure_reason='artificial_engine_failure' if failed else None, score_direction='lower_is_better')
        if not failed:
            inspected = {'pose_index': len(poses), 'title': item['compound_id'] + '__i' + str(item['input_sdf_index']),
                'r_test_score': row['raw_score'], 'covalent_link_present': True, 'covalent_bond_length_angstrom': 1.8,
                'severe_clash': False, 'minimum_nonbonded_heavy_atom_distance_angstrom': 2.4,
                'geometry_inspection_status': 'SUCCESS', 'target_atom_match_count': 1, 'external_target_bond_count': 1,
                'ligand_heavy_atom_count': 3, 'r_strain_Local_Strain_Energy': None if missing_strain and position == 0 else 4.0}
            row.update({key: value for key, value in inspected.items() if key not in ('pose_index', 'title', 'r_test_score')})
            row.update(covdock_pose_file=str(pose), covdock_pose_index=inspected['pose_index'],
                covdock_title=inspected['title'], covdock_inspection_file=str(audit_path))
            poses.append(inspected)
        rows.append(row)
    audit = {'input': str(pose), 'rows': poses, 'geometry_requested': True, 'pose_count': len(poses)}
    audit_path.write_text(json.dumps(audit))
    for row in rows:
        if row['engine_status'] == 'SUCCESS':
            row['covdock_inspection_sha256'] = sha256_file(audit_path)
    def value(flag):
        return float(spec.command[spec.command.index(flag)+1])
    policy = {'final_n': int(value('--final-n')), 'min_success_rate': value('--min-success-rate'),
        'max_local_strain': value('--max-local-strain'), 'hard_max_strain': value('--hard-max-strain'),
        'min_covalent_bond_length': value('--min-covalent-bond-length'),
        'max_covalent_bond_length': value('--max-covalent-bond-length'), 'max_pose_rmsd': value('--max-pose-rmsd'),
        'algorithm_version': spec.parameters['gate_algorithm_version']}
    policy.update(policy_override or {})
    frame = pd.DataFrame(rows)
    gate_covdock_candidates(frame, folder, **policy)
    manifest_name, table_name = SPECS['covdock']
    frame.to_parquet(folder / table_name, index=False)
    manifest = {'refinement_input_contract': spec.parameters['refinement_input_contract'],
        'input_score_source': spec.parameters['input_score_source'], 'input_candidate_table_sha256': sha256_file(table),
        'input_count': len(inputs), 'input_receptor_sha256': sha256_file(source),
        'receptor_input_check': input_check,
        'effective_receptor_input_check': effective, 'attachment_residue': spec.parameters['attachment_residue'],
        'nucleophilic_atom': spec.parameters['nucleophilic_atom'], 'reaction_type': spec.parameters['reaction_type'],
        'pose_consistency_table': None, 'pose_consistency_table_sha256': None,
        'resume': {'requested': False, 'ligprep_reused': False, 'covdock_reused': False, 'source_artifact_dir': None},
        'score_property': 'r_test_score', 'outputs': {name: {'path': str(folder/name), 'sha256': sha256_file(folder/name)}
            for name in (table_name, 'stage5_gate_manifest.json')}}
    (folder / manifest_name).write_text(json.dumps(manifest))
    freeze_refinement_outputs(folder, 'covdock', spec.parameters['refinement_input_contract'],
                              expected_gate_version=spec.parameters['gate_algorithm_version'])
    result = RunResult(run_id=spec.run_id, state='FAILED' if partial else 'COMPLETED',
        engine=spec.engine, route=spec.route, project_id=spec.project_id, attempt=spec.attempt, parent_run_id=spec.parent_run_id,
        submitted_at=utc_now(), return_code=2 if partial else 0, message='ARTIFICIAL PROTOCOL FIXTURE',
        stdout_path=str(folder/'stdout.log'), stderr_path=str(folder/'stderr.log'), artifacts=[
            manager.registry.register_file(spec.run_id, path, 'other') for path in sorted(folder.rglob('*')) if path.is_file()])
    manager.registry.write_result(result)
    def sdk(request, runtime, script):
        assert request['files'] == [str(pose)]
        return {'version': request['version'], 'attachment_residue': request['attachment_residue'],
                'audits': [json.loads(audit_path.read_text())], 'scientific_quality_verified': False}
    return result, sdk
