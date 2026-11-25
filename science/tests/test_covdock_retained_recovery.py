"""Routing and provenance tests; temporary files are not scientific fixtures."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pandas as pd
import pytest
import yaml

from covscreen.covdock_retained import (
    RETAINED_POLICY, compile_retained_request, validate_retained_recovery,
    validate_retained_spec,
)
from covscreen.execution import JobManager
from covscreen.external_hpc import validate_boundary
from covscreen.jobs import ArtifactRegistry, JobSpec, RunResult, ResourceBudget, utc_now, sha256_file
from test_covdock_retained_candidate import fixture, rebind
from receptor_fixture import write_receptor


def storage_case(tmp_path):
    import shutil
    original = tmp_path / 'original'
    original.mkdir()
    records, files, _ = case(original)
    request = compile_retained_request(original, 'parent', records)
    spec = JobManager(original).build_spec(request)
    spec.provenance['worker_backend'] = 'external-hpc'
    registry = ArtifactRegistry(original)
    registry.write_spec(spec)
    registry.write_result(RunResult(run_id=spec.run_id, state='FAILED', engine=spec.engine,
        route=spec.route, project_id=spec.project_id, attempt=3, parent_run_id='parent',
        submitted_at=utc_now(), failure_reason='engine_return_code_1',
        stdout_path='stdout.log', stderr_path='stderr.log', message='Pre-docking storage failure fixture'))
    (registry.job_dir(spec.run_id) / 'stderr.log').write_text(
        'subset.to_parquet(group_table, index=False)\nOSError: [Errno 122] Disk quota exceeded\n')
    relocated = tmp_path / 'relocated'
    shutil.copytree(original, relocated)
    return original, relocated, records, spec


def test_storage_retry_preserves_candidate_budget_and_freezes_failure_evidence(tmp_path):
    original, root, records, parent = storage_case(tmp_path)
    request = compile_retained_request(root, parent.run_id, records)
    assert request.attempt == 4 and request.parent_run_id == parent.run_id
    assert request.parameters == parent.parameters
    assert request.resource_budget == parent.resource_budget
    spec = JobManager(root).build_spec(request)
    validate_retained_spec(root, spec)
    for name in ('spec.json', 'result.json', 'stderr.log'):
        path = root / 'state/jobs' / parent.run_id / name
        assert spec.input_sha256[str(path.relative_to(root))] == sha256_file(path)
    assert not (root / 'state/jobs' / spec.run_id).exists()


def test_storage_retry_requires_relocation(tmp_path):
    original, root, records, parent = storage_case(tmp_path)
    with pytest.raises(ValueError, match='relocated'):
        compile_retained_request(original, parent.run_id, records)


def test_storage_retry_rejects_nonstorage_failure(tmp_path):
    original, root, records, parent = storage_case(tmp_path)
    (root / 'state/jobs' / parent.run_id / 'stderr.log').write_text('Docking failed')
    with pytest.raises(ValueError, match='EDQUOT'):
        compile_retained_request(root, parent.run_id, records)


def test_storage_retry_rejects_already_started_shard(tmp_path):
    original, root, records, parent = storage_case(tmp_path)
    path = root / 'state/jobs' / parent.run_id / 'artifacts/shard_000001'
    path.mkdir(parents=True)
    with pytest.raises(ValueError, match='pre-docking'):
        compile_retained_request(root, parent.run_id, records)


def case(tmp_path):
    descriptor, files = fixture(tmp_path)
    # The real compiler now inspects receptor identity before freezing a plan.
    # Use the existing software-only fragment instead of placeholder text.
    write_receptor(files['receptor'])
    rebind(descriptor, 'receptor', files['receptor'])
    frame = pd.read_parquet(files['candidate_table'])
    frame['covdock_reaction_type'] = 'Michael Addition'
    frame['af3_mpae_rank'] = [1, 2, 3]
    frame.to_parquet(files['candidate_table'])
    sch = tmp_path / 'vendor'
    sch.mkdir()
    for name in ('run', 'covalent_docking', 'license'):
        (sch / name).write_text('not executed by this provenance fixture')
    config = tmp_path / 'configs/local.inspire.yaml'
    config.parent.mkdir()
    config.write_text(yaml.safe_dump({'runtime': {'enable_ligand_hpc_sharded_dispatch': True},
        'schrodinger': {'enabled': True, 'root': str(sch), 'license_file': str(sch / 'license')}}))
    params = {'candidate_table': 'candidates.parquet', 'receptor': 'receptor.mae',
        'attachment_residue': 'A:797', 'nucleophilic_atom': 'SG',
        'reaction_type_column': 'covdock_reaction_type', 'reaction_type': 'per-candidate-column',
        'dispatch_policy': 'ligand-smoke-v1', 'input_row_count': 3, 'final_n': 3,
        'checkpoint_size': 0, 'checkpoint_workers': 1, 'checkpoint_worker_cpus': 1,
        'njobs': 1, 'host': 'localhost:1', 'resume': False, 'prepare_receptor': False,
        'max_local_strain': 10., 'hard_max_strain': 20., 'min_covalent_bond_length': 1.5,
        'max_covalent_bond_length': 2.2, 'max_pose_rmsd': 3.,
        'severe_clash_distance': 1.2, 'min_success_rate': .8}
    parent = JobSpec(run_id='parent', request_id='original-request-retry-2', project_id='egfr-c797',
        route='ligand-first', engine='schrodinger-covdock-lo', command=['not-executed'],
        working_directory=str(tmp_path), output_directory=str(files['pose_file'].parent),
        parameters=params, input_sha256={str(files[n].relative_to(tmp_path)): sha256_file(files[n])
            for n in ('candidate_table', 'receptor')},
        provenance={'worker_backend': 'external-inspire'}, execution_mode='execute',
        human_approved=True, approval_note='Isolated historical fixture, no execution',
        attempt=2, parent_run_id='previous-attempt',
        resource_budget=ResourceBudget(max_runtime_minutes=180, cpu_count=4, memory_gb=16, max_candidates=3))
    result = RunResult(run_id='parent', engine=parent.engine, route=parent.route, project_id=parent.project_id,
        state='TIMED_OUT', failure_reason='external_scheduler_timed_out', submitted_at=utc_now(),
        stdout_path='stdout', stderr_path='stderr', attempt=2, message='Isolated timeout fixture')
    registry = ArtifactRegistry(tmp_path)
    registry.write_spec(parent)
    registry.write_result(result)
    for name in ('parent_spec', 'parent_result', 'candidate_table'):
        rebind(descriptor, name, files[name])
    records = [{'path': str(descriptor.relative_to(tmp_path)), 'sha256': sha256_file(descriptor)}]
    return records, files, sch


def test_compiler_keeps_actual_unsharded_parent_and_freezes_every_retained_dependency(tmp_path):
    records, files, _ = case(tmp_path)
    request = compile_retained_request(tmp_path, 'parent', records)
    assert request.execution_mode == 'plan' and request.human_approved is False
    assert request.resource_budget.cpu_count == 8 and request.resource_budget.max_candidates == 3
    spec = JobManager(tmp_path).build_spec(request)
    assert spec.parameters['input_row_count'] == 3
    assert spec.parameters['checkpoint_workers'] == 2
    assert '--retained-candidate' in spec.command
    assert spec.input_sha256[records[0]['path']] == records[0]['sha256']
    for path in files.values():
        assert spec.input_sha256[str(path.relative_to(tmp_path))] == sha256_file(path)
    assert ArtifactRegistry(tmp_path).load_spec('parent').parameters['checkpoint_size'] == 0
    assert not (tmp_path / 'state/jobs' / spec.run_id).exists()


@pytest.mark.parametrize('field,value', [
    ('attachment_residue', 'A:800'), ('max_local_strain', 50.),
    ('score_property', 'different-score'), ('reaction_type_column', 'changed-chemistry'),
])
def test_reuse_cannot_change_scientific_rules(tmp_path, field, value):
    records, _, _ = case(tmp_path)
    request = compile_retained_request(tmp_path, 'parent', records)
    request.parameters[field] = value
    with pytest.raises(ValueError, match='original chemistry or gate'):
        validate_retained_recovery(tmp_path, request.parameters, records)


def test_duplicate_retained_candidate_rejected(tmp_path):
    records, _, _ = case(tmp_path)
    with pytest.raises(ValueError, match='Duplicate'):
        compile_retained_request(tmp_path, 'parent', records + records)


def test_changed_retained_dependency_invalidates_compiled_scope(tmp_path):
    records, files, _ = case(tmp_path)
    spec = JobManager(tmp_path).build_spec(compile_retained_request(tmp_path, 'parent', records))
    files['pose_file'].write_bytes(b'changed after compilation')
    with pytest.raises(ValueError, match='Retained source changed'):
        validate_retained_spec(tmp_path, spec)


def test_retained_policy_requires_exact_budget_and_ancestry(tmp_path):
    records, _, _ = case(tmp_path)
    request = compile_retained_request(tmp_path, 'parent', records,
        execution_mode='execute', human_approved=True, approval_note='Test envelope only')
    spec = JobManager(tmp_path).build_spec(request)
    spec.provenance['worker_backend'] = 'external-hpc'
    validate_boundary(spec)
    spec.resource_budget.cpu_count = 12
    with pytest.raises(ValueError, match='exact approved boundary'):
        validate_boundary(spec)
    spec.resource_budget.cpu_count = 8
    spec.attempt = 1
    with pytest.raises(ValueError, match='exact approved boundary'):
        validate_boundary(spec)


def test_native_tool_schema_preserves_retry_ancestry_in_real_preparation(tmp_path, monkeypatch):
    records, _, _ = case(tmp_path)
    request = compile_retained_request(tmp_path, 'parent', records,
        execution_mode='execute', human_approved=True, approval_note='Request confirmation, not approval')
    script = Path(__file__).resolve().parents[1] / 'scripts/openscience_mcp_server.py'
    loader = importlib.util.spec_from_file_location('retained_mcp_schema', script)
    server = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(server)
    monkeypatch.setattr(server, 'ROOT', tmp_path)
    schema = next(t['inputSchema'] for t in server.TOOLS if t['name'] == 'covalent_submit_job')
    arguments = request.model_dump(mode='json')
    assert set(arguments).issubset(schema['properties']), 'MCP schema must not drop validated retry fields'
    result = server.dispatch('covalent_prepare_execution',
        {'tool': 'covalent_submit_job', 'arguments': arguments})
    assert result['status'] == 'NATIVE_APPROVAL_PREPARED' and result['compute_submitted'] is False
    assert result['scope']['spec']['attempt'] == 3
    assert result['scope']['spec']['parent_run_id'] == 'parent'
    assert len(list((tmp_path / 'state/jobs').glob('*/spec.json'))) == 1


def test_retained_recovery_rejects_altered_receptor_receipt(tmp_path):
    records, _, _ = case(tmp_path)
    request = compile_retained_request(tmp_path, 'parent', records)
    spec = JobManager(tmp_path).build_spec(request)
    spec.parameters['covdock_receptor_input_check']['receptor_sha256'] = '0' * 64
    with pytest.raises(ValueError, match='bounded request: parameters'):
        validate_retained_spec(tmp_path, spec)


@pytest.mark.parametrize('readback_returncode', [0, 4])
def test_grouped_runner_reparses_one_and_docks_only_other_two(tmp_path, monkeypatch, readback_returncode):
    records, files, sch = case(tmp_path)
    request = compile_retained_request(tmp_path, 'parent', records)
    script = Path(__file__).resolve().parents[1] / 'scripts/run_ligand_first_covdock_grouped.py'
    loader = importlib.util.spec_from_file_location('retained_group_test', script)
    module = importlib.util.module_from_spec(loader)
    loader.loader.exec_module(module)
    calls = []

    # Licensed processes are the sole substitute. The actual routing, manifest
    # merge and on-disk scientific identity checks run unmodified.
    def process(command, check=False, env=None):
        output = Path(command[command.index('--output-dir') + 1])
        if command[1].endswith('reparse_covdock_retained_candidate.py'):
            calls.append(('readback', [2]))
            if readback_returncode:
                return SimpleNamespace(returncode=readback_returncode)
            frame = pd.read_parquet(files['candidate_table']).query('input_sdf_index == 2')
            kind = 'retained_candidate_reparse'
        else:
            frame = pd.read_parquet(command[command.index('--candidate-table') + 1])
            calls.append(('new_docking', frame['input_sdf_index'].tolist()))
            assert 2 not in frame['input_sdf_index'].tolist(), 'Retained candidate must never be redocked'
            kind = 'new_docking'
        output.mkdir(parents=True, exist_ok=True)
        frame = frame.assign(covdock_gate='B', covdock_score=pd.NA)
        for name in ('stage5_gated_candidates', 'covdock_lo_all_scores'):
            frame.to_parquet(output / (name + '.parquet'), index=False)
        frame.iloc[0:0].to_parquet(output / 'stage5_failures.parquet', index=False)
        (output / 'covdock_lo_registered_manifest.json').write_text(json.dumps(
            {'status': 'completed', 'execution_kind': kind}))
        return SimpleNamespace(returncode=0)

    output = tmp_path / 'new-artifacts'
    command = [str(script), '--candidate-table', str(files['candidate_table']),
        '--receptor', str(files['receptor']), '--attachment-residue', 'A:797',
        '--schrodinger-root', str(sch), '--license-file', str(sch / 'license'),
        '--output-dir', str(output), '--checkpoint-size', '1', '--checkpoint-workers', '2',
        '--checkpoint-worker-cpus', '4', '--project-root', str(tmp_path),
        '--retained-candidate', str(tmp_path / records[0]['path']), records[0]['sha256']]
    monkeypatch.setattr(module.subprocess, 'run', process)
    monkeypatch.setattr(sys, 'argv', command)
    assert module.main() == (0 if readback_returncode == 0 else 4)
    assert sorted(calls) == [('new_docking', [0]), ('new_docking', [1]), ('readback', [2])]
    manifest = json.loads((output / 'covdock_lo_registered_manifest.json').read_text())
    assert manifest['input_count'] == 3
    assert manifest['retained_candidate_indices'] == [2]
    assert manifest['requested_new_docking_candidate_indices'] == [0, 1]
    if readback_returncode == 0:
        result = pd.read_parquet(output / 'stage5_gated_candidates.parquet')
        assert result['input_sdf_index'].tolist() == [0, 1, 2]
        assert result['compound_id'].tolist() == ['c', 'b', 'a']
    else:
        assert manifest['status'] == 'quality_gate_failed'
        assert not (output / 'stage5_gated_candidates.parquet').exists()
