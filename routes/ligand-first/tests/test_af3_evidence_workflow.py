"""Real parsers, native test approvals and durable workflow; no AF3/GPU run.

The three molecular candidates are real local graph outputs. Their coordinates
and confidences are artificial software fixtures, never scientific predictions.
"""
import copy
import json
from pathlib import Path
import runpy

import numpy as np
import pandas as pd
import pytest
from Bio.PDB import MMCIFIO
from Bio.PDB.Atom import Atom
from Bio.PDB.Chain import Chain
from Bio.PDB.Model import Model
from Bio.PDB.Residue import Residue
from Bio.PDB.Structure import Structure

from covscreen.af3_adduct import transform_adduct
from covscreen.af3_results import af3_atom_name_for_rdkit_index
from covscreen.af3_selection import select_af3_candidates
from covscreen.execution import JobManager
from covscreen.jobs import sha256_file
from covscreen.ligand_design import design_folder, write
from covscreen.ligand_workflow import DESIGN, navigate
from covscreen.mcp.server import dispatch
from covscreen.refinement_output import freeze_refinement_outputs
from test_af3_result_validation import confidence_files
from test_af3_execution_workflow import SUBMIT, design, graph_design, pose, planned, approval, jobs
from test_refinement_target import positive_review
from test_pose_execution_workflow import args, completed_pose_files


@pytest.fixture
def inputs(pose, monkeypatch):
    completed_pose_files(pose, monkeypatch)
    review = positive_review(pose)
    state = json.loads(pose[2].read_text())
    binding = design_folder(pose[0], state['design_id']) / 'pose-plan/input-binding.json'
    identities = json.loads(binding.read_text())['selected_candidate_ids']
    assert len(identities) == 3
    template = review['candidates'][0]
    review['candidates'] = [{**copy.deepcopy(template), 'candidate_id': name,
                            'selected_pose_record_index': index} for index, name in enumerate(identities)]
    review['passing_candidate_count'] = 3
    monkeypatch.setattr('covscreen.ligand_pose_evidence.evaluate_outputs', lambda *args: copy.deepcopy(review))
    dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement'))
    dispatch(pose[0], DESIGN, args(pose, 'prepare_refinement_target'))
    monkeypatch.setattr(JobManager, 'submit', SUBMIT)
    return pose


def model_file(directory, row, site, *, link=True):
    structure, model = Structure('software_fixture'), Model(0)
    structure.add(model)
    protein, ligand = Chain('A'), Chain('L')
    model.add(protein)
    model.add(ligand)
    cysteine, component = Residue((' ', site, ' '), 'CYS', ' '), Residue(('H_LIG', 1, ' '), 'LIG', ' ')
    protein.add(cysteine)
    ligand.add(component)
    cysteine.add(Atom('SG', np.zeros(3), 20., 1., ' ', ' SG ', 1, element='S'))
    molecule, electrophile, _ = transform_adduct(row['standardized_smiles'], row['warhead_class'])
    reactive = af3_atom_name_for_rdkit_index(molecule, electrophile)
    for atom in molecule.GetAtoms():
        if atom.GetAtomicNum() <= 1:
            continue
        name = af3_atom_name_for_rdkit_index(molecule, atom.GetIdx())
        point = [1.8, 0., 0.] if name == reactive else [4. + atom.GetIdx(), 2., 1.]
        component.add(Atom(name, np.array(point), 20., 1., ' ', name.ljust(4), atom.GetIdx() + 2,
                           element=atom.GetSymbol().upper()))
    path = directory / (directory.name + '_model.cif')
    writer = MMCIFIO()
    writer.set_structure(structure)
    writer.save(str(path))
    if link:
        headers = ['_struct_conn.conn_type_id'] + [f'_struct_conn.ptnr{number}_{field}'
            for number in (1, 2) for field in ('auth_asym_id', 'auth_seq_id', 'label_atom_id')]
        path.write_text(path.read_text() + '\nloop_\n' + '\n'.join(headers)
                        + f'\ncovale A {site} SG L 1 {reactive}\n#\n')
    summary, confidence = confidence_files(directory)
    summary.rename(directory / (directory.name + '_summary_confidences.json'))
    confidence.rename(directory / (directory.name + '_confidences.json'))


def registered_results(planned, *, successes=2, link=True):
    response = dispatch(planned[0], DESIGN, approval(planned))
    manager = JobManager(planned[0])
    run = response['af3_execution']['run_id']
    spec, result = manager.registry.load_spec(run), manager.registry.load_result(run)
    folder = manager.registry.job_dir(run) / 'artifacts'
    folder.mkdir(exist_ok=True)
    rows = pd.read_csv(planned[0] / spec.parameters['candidate_table']).to_dict(orient='records')
    runtime = spec.parameters['af3_target_seed_check']
    for index, row in enumerate(rows):
        row.update(job_name='fixture_' + str(index), status='SUCCESS')
        directory = folder / 'predictions' / row['job_name']
        directory.mkdir(parents=True)
        if index < successes:
            model_file(directory, row, runtime['cysteine_sequence_index'], link=link)
    batch = folder / 'batch.json'
    write(batch, rows)
    from covscreen import af3_results
    script = Path(af3_results.__file__).parents[2] / 'scripts/parse_af3_batch.py'
    parse = runpy.run_path(str(script))['parse_batch']
    frame = parse(None, folder / 'predictions', planned[0] / spec.parameters['target_config'], batch_manifest=batch)
    manifest = select_af3_candidates(frame, folder, top_n=len(rows))
    manifest['inputs'] = {'refinement_input_contract': spec.parameters['refinement_input_contract'],
        'candidate_table': {'sha256': spec.parameters['expected_input_sha256']},
        'source_data_json': {'sha256': runtime['source_data_sha256']},
        'target_config': {'sha256': runtime['target_config_sha256']}}
    manifest['execution'] = {'resume_enabled': spec.parameters['resume'], 'reparse_only': False,
        'total': len(rows), 'run_data_pipeline': spec.parameters['run_data_pipeline']}
    write(folder / 'stage4_selection_manifest.json', manifest)
    freeze_refinement_outputs(folder, 'af3', spec.parameters['refinement_input_contract'])
    result.state, result.return_code = ('COMPLETED', 0) if successes == len(rows) else ('FAILED', 2)
    result.artifacts = [manager.registry.register_file(run, path, 'manifest')
                        for path in folder.rglob('*') if path.is_file()]
    manager.registry.write_result(result)
    dispatch(planned[0], DESIGN, args(planned, 'af3_status'))
    return manager, spec, folder


def test_real_parsers_report_partial_batch_without_losing_failed_denominator(planned):
    manager, spec, folder = registered_results(planned)
    before = jobs(planned)
    response = dispatch(planned[0], DESIGN, args(planned, 'report_af3'))
    value = response['af3_execution']['evaluation']
    assert value['candidate_count'] == 3
    assert value['measured_candidate_count'] == 2
    assert value['failed_candidate_count'] == 1
    assert response['execution']['state'] == 'failed'  # Coverage exit 2 is not erased.
    assert response['assessment']['state'] == 'review_required'
    assert value['candidates'][2]['confidence'] is None
    assert value['candidates'][2]['geometry'] is None
    assert value['candidates'][2]['score'] is None
    assert value['candidates'][2]['failure_reason'] == 'missing_af3_output'
    first = value['candidates'][0]
    assert first['confidence']['mpae'] == 7.5
    assert first['geometry']['reactive_distance_angstrom'] == pytest.approx(1.8)
    assert first['geometry']['af3_sequence_residue'] == 102
    assert first['geometry']['source_residue'] == 102  # The source config uses the projected construct.
    assert value['target_mapping']['source']['uniprot_residue'] == 797
    assert value['target_mapping']['source']['author_residue_id'] == '797'
    assert first['geometry']['model_has_exact_covalent_link'] is True
    assert not value['scientific_result_verified'] and not value['md_ready']
    report = response['af3_execution']['report']
    serialized = (planned[0] / report['report_json']).read_text()
    assert len(serialized.splitlines()) > 2
    assert max(map(len, serialized.splitlines())) < 2000
    assert json.loads(serialized)['evaluation'] == value
    assert '共价连接本来就是预测输入的约束' in (planned[0] / report['report_markdown']).read_text()
    assert len([item for item in response['artifacts'] if item['kind'] == 'af3_measurement_report']) == 1
    recovered = navigate(planned[0], 'status', workflow_id=planned[1])
    assert recovered['af3_execution']['report'] == report
    again = dispatch(planned[0], DESIGN, args(planned, 'report_af3'))
    assert again['af3_execution']['report'] == report
    assert jobs(planned) == before


@pytest.mark.parametrize('successes', [0, 3])
def test_complete_census_can_be_all_failed_or_all_measured(planned, successes):
    registered_results(planned, successes=successes)
    response = dispatch(planned[0], DESIGN, args(planned, 'evaluate_af3'))
    value = response['af3_execution']['evaluation']
    assert value['candidate_count'] == 3 and value['measured_candidate_count'] == successes
    assert response['assessment']['state'] == ('review_required' if successes else 'evidence_insufficient')
    assert all(row['physical_acceptance'] == 'not_established' for row in value['candidates'])


def test_absent_connectivity_remains_a_negative_measurement(planned):
    registered_results(planned, link=False)
    response = dispatch(planned[0], DESIGN, args(planned, 'evaluate_af3'))
    row = response['af3_execution']['evaluation']['candidates'][0]
    assert row['engine_status'] == 'SUCCESS'
    assert row['geometry']['model_has_exact_covalent_link'] is False
    assert row['physical_acceptance'] == 'not_established'


def test_completed_job_with_no_outputs_remains_evidence_insufficient(planned):
    response = dispatch(planned[0], DESIGN, approval(planned))
    manager = JobManager(planned[0])
    result = manager.registry.load_result(response['af3_execution']['run_id'])
    result.state, result.return_code = 'COMPLETED', 0
    manager.registry.write_result(result)
    dispatch(planned[0], DESIGN, args(planned, 'af3_status'))
    with pytest.raises(ValueError, match='missing'):
        dispatch(planned[0], DESIGN, args(planned, 'report_af3'))
    status = navigate(planned[0], 'status', workflow_id=planned[1])
    assert status['execution']['state'] == 'completed'
    assert status['assessment']['state'] == 'evidence_insufficient'
    assert 'report' not in status['af3_execution']
    assert len(jobs(planned)) == 2


@pytest.mark.parametrize('change', ['structure', 'confidence', 'table', 'manifest', 'report'])
def test_status_rejects_changed_evidence_and_retracts_report(planned, change):
    manager, spec, folder = registered_results(planned)
    response = dispatch(planned[0], DESIGN, args(planned, 'report_af3'))
    report = response['af3_execution']['report']
    path = {'structure': folder / 'predictions/fixture_0/fixture_0_model.cif',
        'confidence': folder / 'predictions/fixture_0/fixture_0_confidences.json',
        'table': folder / 'af3_all_scores.parquet', 'manifest': folder / 'refinement_artifacts.json',
        'report': planned[0] / report['report_markdown']}[change]
    path.write_bytes(path.read_bytes() + b'\n')
    response = navigate(planned[0], 'status', workflow_id=planned[1])
    assert response['assessment']['state'] == 'evidence_insufficient'
    assert response['af3_execution']['evaluation_verification'] == 'evidence_rejected'
    assert 'report' not in response['af3_execution']
    assert not any(item['kind'] == 'af3_measurement_report' for item in response['artifacts'])
    assert response['af3_execution']['run_id'] == spec.run_id
    assert len(jobs(planned)) == 2


def test_initial_collection_requires_registered_manifest(planned):
    manager, spec, folder = registered_results(planned)
    result = manager.registry.load_result(spec.run_id)
    result.artifacts = [item for item in result.artifacts if not item.path.endswith('refinement_artifacts.json')]
    manager.registry.write_result(result)
    with pytest.raises(ValueError, match='registered evidence'):
        dispatch(planned[0], DESIGN, args(planned, 'evaluate_af3'))
    assert len(jobs(planned)) == 2


def rewrite_software_receipt(manager, spec, folder):
    """Simulate a self-consistent but incorrect worker claim, not a real job."""
    path = folder / 'stage4_selection_manifest.json'
    manifest = json.loads(path.read_text())
    for item in manifest['outputs'].values():
        item['sha256'] = sha256_file(Path(item['path']))
    write(path, manifest)
    (folder / 'refinement_artifacts.json').unlink()
    freeze_refinement_outputs(folder, 'af3', spec.parameters['refinement_input_contract'])
    result = manager.registry.load_result(spec.run_id)
    result.artifacts = [manager.registry.register_file(spec.run_id, path, 'manifest')
                        for path in folder.rglob('*') if path.is_file()]
    manager.registry.write_result(result)


@pytest.mark.parametrize('change', ['confidence', 'geometry', 'target'])
def test_self_consistent_worker_claim_still_requires_independent_measurement(planned, change):
    manager, spec, folder = registered_results(planned)
    if change == 'target':
        path = folder / 'stage4_selection_manifest.json'
        manifest = json.loads(path.read_text())
        manifest['inputs']['target_config']['sha256'] = '0' * 64
        write(path, manifest)
    else:
        path = folder / 'af3_all_scores.parquet'
        frame = pd.read_parquet(path)
        if change == 'confidence':
            frame.loc[0, ['raw_score', 'mpae']] = 1.
        else:
            frame.loc[0, 'reactive_distance_angstrom'] = .1
        frame.to_parquet(path, index=False)
    rewrite_software_receipt(manager, spec, folder)
    with pytest.raises(ValueError, match='differs'):
        dispatch(planned[0], DESIGN, args(planned, 'report_af3'))
    response = navigate(planned[0], 'status', workflow_id=planned[1])
    assert response['assessment']['state'] == 'evidence_insufficient'
    assert not response['af3_execution'].get('report')
    assert len(jobs(planned)) == 2


def test_retained_witness_rejects_rewritten_engine_snapshot_and_registry(planned):
    manager, spec, folder = registered_results(planned)
    dispatch(planned[0], DESIGN, args(planned, 'evaluate_af3'))
    path = folder / 'predictions/fixture_0/fixture_0_model.cif'
    path.write_bytes(path.read_bytes() + b'\n')
    rewrite_software_receipt(manager, spec, folder)
    response = navigate(planned[0], 'status', workflow_id=planned[1])
    assert response['assessment']['state'] == 'evidence_insufficient'
    assert response['af3_execution']['evaluation_verification'] == 'evidence_rejected'
    assert len(jobs(planned)) == 2
