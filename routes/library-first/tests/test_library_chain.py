import json
from pathlib import Path

from covscreen.jobs import ArtifactRegistry, JobSpec, ResourceBudget, RunResult, sha256_file
from covscreen.library_chain import (
    LibraryFirstChainConfig,
    _recoverable_af3_artifacts,
    compile_next_request,
    inspect_chain,
)


def _seed_prescreen(root: Path, run_id: str) -> None:
    registry = ArtifactRegistry(root)
    artifacts = registry.job_dir(run_id) / "artifacts"
    source = root / "source.csv"
    source.write_text("SMILES\nCC\nCCC\n")
    spec = JobSpec(
        run_id=run_id,
        request_id="seed-prescreen",
        project_id="egfr-c797",
        route="library-first",
        engine="covindb-prescreen",
        command=["true"],
        input_sha256={"source.csv": sha256_file(source)},
        expected_output_paths=[str(artifacts)],
        parameters={"source_sha256": sha256_file(source), "max_rows": 2, "seed": 1, "record_selection": "reservoir"},
        working_directory=str(root),
        output_directory=str(artifacts),
        resource_budget=ResourceBudget(max_runtime_minutes=10, max_candidates=2),
    )
    result = RunResult(
        run_id=run_id,
        state="COMPLETED",
        return_code=0,
        engine="covindb-prescreen",
        route="library-first",
        project_id="egfr-c797",
        submitted_at="2026-09-01T00:00:00+00:00",
        message="done",
        stdout_path=str(registry.job_dir(run_id) / "stdout.log"),
        stderr_path=str(registry.job_dir(run_id) / "stderr.log"),
    )
    registry.create(spec, result)
    artifacts.mkdir(parents=True)
    (artifacts / "stage2_selection_manifest.json").write_text(
        json.dumps({"status": "completed", "selection_outcome": "CANDIDATES_SELECTED",
                    "source": {"path": str(source), "sha256": sha256_file(source), "max_rows": 2, "sampling_seed": 1, "record_selection": "reservoir"},
                    "selection": {"top_n_selected": 2}, "counts": {"selected": 2},
                    "quality_gates": dict.fromkeys(["identity_loss_zero", "rdkit_parse_rate_ge_0_99", "selected_warhead_mapping_rate_ge_0_98", "failure_table_present"], True)})
    )
    import pandas as pd

    frame = pd.DataFrame(
        {"input_sdf_index": [0, 1], "compound_id": ["A", "B"], "standardized_smiles": ["CC", "CCC"]}
    )
    frame.to_parquet(artifacts / "stage2_candidates.parquet", index=False)
    frame.to_csv(artifacts / "stage2_candidates.csv", index=False)
    pd.DataFrame({"reason": []}).to_parquet(artifacts / "covindb_rejected.parquet", index=False)
    from rdkit import Chem
    with Chem.SDWriter(str(artifacts / "stage2_candidates.sdf")) as writer:
        for index, row in frame.iterrows():
            mol = Chem.MolFromSmiles(row.standardized_smiles)
            mol.SetProp("COMPOUND_ID", row.compound_id)
            mol.SetIntProp("INPUT_SDF_INDEX", index)
            writer.write(mol)
    result.artifacts = [registry.register_file(run_id, path, "other") for path in artifacts.iterdir()]
    registry.write_result(result)


def test_chain_compiles_carsidock_from_pinned_quality_passed_prescreen(tmp_path: Path):
    for relative in (
        "configs/target.yaml",
        "protein.pdb",
        "reference.sdf",
        "source_data.json",
        "receptor.mae",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    run_id = "20260901T000000-covindb-prescreen-seed00"
    _seed_prescreen(tmp_path, run_id)
    config = LibraryFirstChainConfig(
        campaign_id="pilot",
        project_id="egfr-c797",
        target_config="configs/target.yaml",
        pdb="protein.pdb",
        reference_ligand="reference.sdf",
        af3_source_data_json="source_data.json",
        covdock_receptor="receptor.mae",
        attachment_residue="A:773",
        covres="A:CYS:773",
        seed_run_ids={"prescreen": run_id},
    )
    status = inspect_chain(tmp_path, config)
    assert status.terminal_state == "ready"
    assert status.next_stage == "carsidock"
    request = compile_next_request(tmp_path, config, status)
    assert request is not None
    assert request.engine == "carsidock-cov"
    assert request.parent_run_id == run_id
    assert request.parameters["ligand_table"].endswith("stage2_candidates.parquet")
    assert request.resource_budget.max_candidates == 2


def test_chain_compiles_cpu_recovery_from_retained_carsidock_poses(tmp_path: Path):
    for relative in (
        "configs/target.yaml",
        "protein.pdb",
        "reference.sdf",
        "source_data.json",
        "receptor.mae",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x")
    prescreen_id = "20260901T000000-covindb-prescreen-seed00"
    _seed_prescreen(tmp_path, prescreen_id)
    registry = ArtifactRegistry(tmp_path)
    failed_id = "20260901T010000-carsidock-cov-failed00"
    failed_artifacts = registry.job_dir(failed_id) / "artifacts"
    spec = JobSpec(
        run_id=failed_id,
        request_id="failed-carsidock",
        project_id="egfr-c797",
        route="library-first",
        engine="carsidock-cov",
        command=["false"],
        working_directory=str(tmp_path),
        output_directory=str(failed_artifacts),
        resource_budget=ResourceBudget(
            max_runtime_minutes=10, gpu_count=1, max_candidates=2
        ),
        parent_run_id=prescreen_id,
    )
    result = RunResult(
        run_id=failed_id,
        state="FAILED",
        engine="carsidock-cov",
        route="library-first",
        project_id="egfr-c797",
        parent_run_id=prescreen_id,
        submitted_at="2026-09-01T01:00:00+00:00",
        message="upstream returned non-zero after writing poses",
        stdout_path=str(registry.job_dir(failed_id) / "stdout.log"),
        stderr_path=str(registry.job_dir(failed_id) / "stderr.log"),
    )
    registry.create(spec, result)
    failed_artifacts.mkdir()
    for filename in ("carsidock.sdf", "carsidock_nodumat.sdf", "carsidock_withpckt.pdb"):
        (failed_artifacts / filename).write_text("retained")
    config = LibraryFirstChainConfig(
        campaign_id="pilot",
        project_id="egfr-c797",
        target_config="configs/target.yaml",
        pdb="protein.pdb",
        reference_ligand="reference.sdf",
        af3_source_data_json="source_data.json",
        covdock_receptor="receptor.mae",
        attachment_residue="A:773",
        covres="A:CYS:773",
        seed_run_ids={"prescreen": prescreen_id},
    )

    status = inspect_chain(tmp_path, config)
    request = compile_next_request(tmp_path, config, status)

    assert status.terminal_state == "ready"
    assert status.next_stage == "carsidock"
    assert status.stages[-1].state == "FAILED"
    assert request is not None
    assert request.parameters["recovery_source_run_id"] == failed_id
    assert request.parameters["reuse_output_prefix"].endswith(
        f"{failed_id}/artifacts/carsidock"
    )
    assert request.parameters["device"] == "cpu"
    assert request.resource_budget.gpu_count == 0


def test_quality_failed_seed_stops_chain(tmp_path: Path):
    run_id = "20260901T000000-covindb-prescreen-seed00"
    _seed_prescreen(tmp_path, run_id)
    manifest = tmp_path / "state" / "jobs" / run_id / "artifacts" / "stage2_selection_manifest.json"
    manifest.write_text(json.dumps({"status": "quality_gate_failed", "quality_gates": {"identity": False}}))
    config = LibraryFirstChainConfig(
        campaign_id="pilot",
        project_id="egfr-c797",
        target_config="x",
        pdb="x",
        reference_ligand="x",
        af3_source_data_json="x",
        covdock_receptor="x",
        attachment_residue="A:773",
        covres="A:CYS:773",
        seed_run_ids={"prescreen": run_id},
    )
    status = inspect_chain(tmp_path, config)
    assert status.terminal_state == "quality_gate_failed"
    assert status.next_stage is None


def test_completed_af3_predictions_are_recoverable_for_cpu_reparse(tmp_path: Path):
    run_id = "20260902T034028-alphafold3-mpae-failed00"
    artifacts = tmp_path / "state" / "jobs" / run_id / "artifacts"
    (artifacts / "prepared").mkdir(parents=True)
    (artifacts / "predictions").mkdir()
    (artifacts / "prepared" / "batch_manifest.json").write_text("[]")
    (artifacts / "progress.json").write_text(
        json.dumps({"total": 130, "completed": 130, "failed": 0})
    )

    assert _recoverable_af3_artifacts(tmp_path, run_id) == artifacts


def test_incomplete_af3_predictions_are_not_recoverable(tmp_path: Path):
    run_id = "20260902T034028-alphafold3-mpae-failed00"
    artifacts = tmp_path / "state" / "jobs" / run_id / "artifacts"
    (artifacts / "prepared").mkdir(parents=True)
    (artifacts / "predictions").mkdir()
    (artifacts / "prepared" / "batch_manifest.json").write_text("[]")
    (artifacts / "progress.json").write_text(
        json.dumps({"total": 130, "completed": 129, "failed": 1})
    )

    assert _recoverable_af3_artifacts(tmp_path, run_id) is None


def test_chain_rejects_changed_prescreen_even_with_success_flag(tmp_path):
    run_id = '20260901T000000-covindb-prescreen-tamper'
    _seed_prescreen(tmp_path, run_id)
    config = LibraryFirstChainConfig(
        campaign_id='tamper-test', project_id='egfr-c797', target_config='target.yaml',
        pdb='protein.pdb', reference_ligand='reference.sdf', af3_source_data_json='af3.json',
        covdock_receptor='receptor.mae', attachment_residue='A:773', covres='A:CYS:773',
        seed_run_ids={'prescreen': run_id})
    assert inspect_chain(tmp_path, config).next_stage == 'carsidock'
    (tmp_path / 'source.csv').write_text('changed after completion')
    state = inspect_chain(tmp_path, config)
    assert state.terminal_state == 'quality_gate_failed'
    assert state.next_stage is None
    assert compile_next_request(tmp_path, config, state) is None


def test_workflow_observation_survives_reload_without_granting_approval(tmp_path):
    from covscreen.workflow_state import persist_chain_observation
    run_id = '20260901T000000-covindb-prescreen-resume'
    _seed_prescreen(tmp_path, run_id)
    config = LibraryFirstChainConfig(
        campaign_id='resume-test', project_id='egfr-c797', target_config='target.yaml',
        pdb='protein.pdb', reference_ligand='reference.sdf', af3_source_data_json='af3.json',
        covdock_receptor='receptor.mae', attachment_residue='A:773', covres='A:CYS:773',
        seed_run_ids={'prescreen': run_id})
    config_path = tmp_path / 'chain.json'
    config_path.write_text(config.model_dump_json())
    response = {'chain': inspect_chain(tmp_path, config).model_dump(mode='json'),
                'next_request': {'engine': 'carsidock-cov', 'human_approved': True, 'approval_note': 'not authority'}}
    receipt = persist_chain_observation(tmp_path, config_path, response)
    snapshot = json.loads((tmp_path / receipt['path']).read_text())
    assert snapshot['chain']['next_stage'] == 'carsidock'
    assert snapshot['inputs'][0]['run_id'] == run_id
    assert snapshot['approval_required_for_execution'] is True
    assert 'human_approved' not in snapshot['next_request']
    assert 'approval_note' not in snapshot['next_request']
    assert snapshot['resume']['arguments']['submit'] is False
    second = persist_chain_observation(tmp_path, config_path, response)
    assert second['workflow_id'] == receipt['workflow_id']
    assert second['revision'] == 2
    assert (tmp_path / receipt['path']).with_name('00000001.json').is_file()


def test_resume_discovers_workflow_and_revalidates_without_extending_scope(tmp_path):
    from covscreen.mcp.server import dispatch
    from covscreen.tool_api import library_first_chain_tool
    run_id = '20260901T000000-covindb-prescreen-bounded'
    _seed_prescreen(tmp_path, run_id)
    config = LibraryFirstChainConfig(
        campaign_id='bounded-test', project_id='egfr-c797', terminal_stage='prescreen', target_config='target.yaml',
        pdb='protein.pdb', reference_ligand='reference.sdf', af3_source_data_json='af3.json',
        covdock_receptor='receptor.mae', attachment_residue='A:773', covres='A:CYS:773',
        seed_run_ids={'prescreen': run_id})
    path = tmp_path / 'chain.json'
    path.write_text(config.model_dump_json())
    first = library_first_chain_tool(tmp_path, str(path))
    assert first['chain']['terminal_state'] == 'completed'
    assert first['next_request'] is None
    assert len(first['route_plan']['stages']) == 1
    listed = dispatch(tmp_path, 'covalent_list_workflows', {})
    identity = listed['workflows'][0]['workflow_id']
    assert listed['workflows'][0]['requires_live_revalidation']
    resumed = dispatch(tmp_path, 'covalent_resume_workflow', {'workflow_id': identity})
    assert resumed['resumed_from_revision'] == 1
    assert resumed['submitted'] is False
    assert resumed['calculation_submitted'] is False
    (tmp_path / 'source.csv').write_text('changed while disconnected')
    rejected = dispatch(tmp_path, 'covalent_resume_workflow', {'workflow_id': identity})
    assert rejected['chain']['terminal_state'] == 'quality_gate_failed'
    assert rejected['next_request'] is None
    path.write_text(config.model_copy(update={'terminal_stage': 'pareto'}).model_dump_json())
    import pytest
    with pytest.raises(ValueError, match='configuration changed'):
        dispatch(tmp_path, 'covalent_resume_workflow', {'workflow_id': identity})
