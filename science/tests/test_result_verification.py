import json

import pytest

from covscreen.jobs import ArtifactRegistry, JobSpec, ResourceBudget, RunResult, sha256_file, utc_now
from covscreen.result_verification import verify_job


def fixture(root):
    source = root / 'source.csv'
    source.write_text('SMILES\nCCO\n')
    output = root / 'output'
    output.mkdir()
    manifest = {'status': 'completed', 'selection_outcome': 'CANDIDATES_SELECTED',
                'source': {'path': str(source), 'sha256': sha256_file(source), 'max_rows': 1,
                           'sampling_seed': 1, 'record_selection': 'reservoir'},
                'selection': {'top_n_selected': 1}, 'counts': {'selected': 1},
                'quality_gates': dict.fromkeys(['rdkit_parse_rate_ge_0_99', 'selected_warhead_mapping_rate_ge_0_98',
                                              'identity_loss_zero', 'failure_table_present'], True)}
    (output / 'stage2_selection_manifest.json').write_text(json.dumps(manifest))
    from rdkit import Chem
    import pyarrow as pa
    import pyarrow.parquet as pq
    (output / 'stage2_candidates.csv').write_text('compound_id,standardized_smiles\ncandidate-1,CCO\n')
    pq.write_table(pa.Table.from_pylist([{'compound_id': 'candidate-1', 'standardized_smiles': 'CCO'}]), output / 'stage2_candidates.parquet')
    pq.write_table(pa.table({'reason': pa.array([], type=pa.string())}), output / 'covindb_rejected.parquet')
    molecule = Chem.MolFromSmiles('CCO')
    molecule.SetProp('COMPOUND_ID', 'candidate-1')
    molecule.SetIntProp('INPUT_SDF_INDEX', 0)
    with Chem.SDWriter(str(output / 'stage2_candidates.sdf')) as writer:
        writer.write(molecule)
    spec = JobSpec(run_id='verify-test-001', request_id='verify-request', project_id='test', route='library-first',
                   engine='covindb-prescreen', command=['not-executed'], working_directory=str(root),
                   output_directory=str(output), input_sha256={'source.csv': sha256_file(source)},
                   expected_output_paths=[str(output)], resource_budget=ResourceBudget(max_runtime_minutes=1),
                   parameters={'source_sha256': sha256_file(source), 'max_rows': 1, 'seed': 1, 'record_selection': 'reservoir'})
    registry = ArtifactRegistry(root)
    result = RunResult(run_id=spec.run_id, project_id=spec.project_id, route=spec.route, engine=spec.engine,
                       state='COMPLETED', return_code=0, submitted_at=utc_now(), message='fixture', stdout_path='', stderr_path='')
    registry.create(spec, result)
    refresh(registry, result, output)
    return registry, result, output


def refresh(registry, result, output):
    result.artifacts = [registry.register_file(result.run_id, p, 'other') for p in output.iterdir()]
    registry.write_result(result)


def test_verification_is_recomputed_after_reconnect_and_tampering(tmp_path):
    registry, result, output = fixture(tmp_path)
    assert verify_job(tmp_path, result.run_id)['verified'] is True
    (output / 'stage2_candidates.csv').write_text('changed')
    response = verify_job(tmp_path, result.run_id)
    assert response['verified'] is False
    assert response['next_action'] == 'stop_dependent_stages'


@pytest.mark.parametrize('mutation', ['input', 'missing', 'gate', 'budget', 'identity', 'sampling', 'duplicate', 'sdf_corrupt'])
def test_success_exit_does_not_override_invalid_evidence(tmp_path, mutation):
    registry, result, output = fixture(tmp_path)
    manifest_path = output / 'stage2_selection_manifest.json'
    manifest = json.loads(manifest_path.read_text())
    if mutation == 'input':
        (tmp_path / 'source.csv').write_text('changed')
    elif mutation == 'missing':
        (output / 'stage2_candidates.sdf').unlink()
    elif mutation == 'gate':
        manifest['quality_gates'] = {}
    elif mutation == 'budget':
        manifest['selection']['top_n_selected'] = 2
    elif mutation == 'identity':
        result.project_id = 'different'
    elif mutation == 'sampling':
        manifest['source']['sampling_seed'] = 2
    elif mutation == 'sdf_corrupt':
        (output / 'stage2_candidates.sdf').write_text('invalid')
    elif mutation == 'duplicate':
        (output / 'stage2_candidates.csv').write_text('compound_id\nsame\nsame\n')
    manifest_path.write_text(json.dumps(manifest))
    refresh(registry, result, output)
    assert verify_job(tmp_path, result.run_id)['verified'] is False


def test_running_is_blocked_not_verified(tmp_path):
    registry, result, _ = fixture(tmp_path)
    result.state = 'RUNNING'
    registry.write_result(result)
    response = verify_job(tmp_path, result.run_id)
    assert response['status'] == 'BLOCKED'
    assert response['next_action'] == 'wait_same_job'


def test_direct_docking_cannot_bypass_parent_verification(tmp_path):
    from covscreen.execution import JobManager
    from covscreen.jobs import JobSubmissionRequest
    from covscreen.stage_boundary import require_prescreen_parent
    registry, result, output = fixture(tmp_path)
    request = JobSubmissionRequest(request_id='dock-boundary', project_id='test', route='library-first',
        engine='carsidock-cov', parent_run_id=result.run_id,
        parameters={'ligand_table': str(output / 'stage2_candidates.parquet'),
                    'ligand_sdf': str(output / 'stage2_candidates.sdf')},
        resource_budget=ResourceBudget(max_runtime_minutes=1, gpu_count=1))
    require_prescreen_parent(tmp_path, request)
    with pytest.raises(ValueError, match='parent_run_id'):
        JobManager(tmp_path).build_spec(request.model_copy(update={'parent_run_id': None}))
    with pytest.raises(ValueError, match='differs'):
        require_prescreen_parent(tmp_path, request.model_copy(update={'parameters': {'ligand_table': 'other.parquet'}}))
    (tmp_path / 'source.csv').write_text('changed')
    with pytest.raises(ValueError, match='failed verification'):
        JobManager(tmp_path).build_spec(request)


def test_matching_wrong_run_ids_cannot_verify_requested_job(tmp_path):
    registry, result, _ = fixture(tmp_path)
    assert verify_job(tmp_path, result.run_id)['verified'] is True
    for name in ('spec.json', 'result.json'):
        path = registry.job_dir(result.run_id) / name
        payload = json.loads(path.read_text())
        payload['run_id'] = 'different-valid-run-001'
        path.write_text(json.dumps(payload))
    response = verify_job(tmp_path, result.run_id)
    assert response['verified'] is False
    assert response['status'] == 'FAIL'
    assert 'identity_mismatch:requested_run_id' in response['errors']
