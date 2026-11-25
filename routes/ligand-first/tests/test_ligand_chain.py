from pathlib import Path

import json
import pandas as pd
import pytest
from types import SimpleNamespace

from covscreen.external_inspire import STAGES as EXTERNAL_STAGES, validate_boundary
from covscreen.ligand_chain import (
    LigandFirstChainConfig,
    compile_next_ligand_request,
    inspect_ligand_chain,
    ligand_chain_provenance,
    load_ligand_chain_config,
)
from covscreen.jobs import ArtifactRegistry, RunResult, sha256_file, utc_now


ROOT = (Path(__file__).resolve().parents[3] / "science")


def test_registered_egfr_ligand_chain_compiles_only_the_first_legal_stage():
    config = load_ligand_chain_config(
        ROOT / "configs/ligand_first/EGFR_C797_erlotinib_v1.yaml"
    )
    status = inspect_ligand_chain(ROOT, config)
    request = compile_next_ligand_request(ROOT, config, status)

    assert status.terminal_state == "ready"
    assert status.next_stage == "structure_intake"
    assert request is not None
    assert request.route == "ligand-first"
    assert request.engine == "ligand-first-structure-intake"
    assert request.parent_run_id is None
    assert request.parameters["restore_missing"] is True
    assert request.resource_budget.gpu_count == 0
    assert request.resource_budget.max_candidates == 1


def _complete(root: Path, run_id: str, engine: str, parent: str | None) -> Path:
    registry = ArtifactRegistry(root)
    directory = registry.job_dir(run_id)
    (directory / "artifacts").mkdir(parents=True)
    result = RunResult(
        run_id=run_id,
        state="COMPLETED",
        engine=engine,
        route="ligand-first",
        project_id="test-ligand",
        parent_run_id=parent,
        submitted_at=utc_now(),
        finished_at=utc_now(),
        message="completed test stage",
        stdout_path="stdout.log",
        stderr_path="stderr.log",
    )
    registry._write_json(directory / "result.json", result.model_dump(mode="json"))
    return directory / "artifacts"


def test_chain_requires_every_parent_quality_gate_before_completion(tmp_path: Path):
    chain_config = tmp_path / "configs" / "ligand_first" / "test.yaml"
    chain_config.parent.mkdir(parents=True)
    chain_config.write_text("schema_version: '1.0'\n")
    for name in ("protein.pdb", "parent.sdf", "af3.json", "receptor.mae", "target.yaml"):
        (tmp_path / name).write_text(f"test {name}\n")
    config = LigandFirstChainConfig(
        campaign_id="test-ligand-chain",
        project_id="test-ligand",
        campaign_path="campaign.json",
        trace_chain_config="configs/ligand_first/test.yaml",
        target_config="target.yaml",
        pdb="protein.pdb",
        reference_ligand="parent.sdf",
        af3_source_data_json="af3.json",
        covdock_receptor="receptor.mae",
        attachment_residue="A:797",
        covres="A:CYS:773",
        carsidock_top_n=2,
        af3_top_n=2,
        covdock_top_n=2,
        final_n=2,
    )
    parent = None
    artifacts = _complete(tmp_path, "test-structure-0001", "ligand-first-structure-intake", parent)
    (artifacts / "ligand_first_input_audit.json").write_text(
        json.dumps({"status": "PASS", "asset_checks": {"protein.pdb": True}})
    )
    (artifacts / "parent_ligand_pose.sdf").write_text("pose\n")
    parent = "test-structure-0001"

    artifacts = _complete(tmp_path, "test-covalentize-0001", "ligand-first-covalentization", parent)
    (artifacts / "ligand_first_candidates.json").write_text(
        json.dumps({"site_discovery_mode": "covalentizer-rdkit-rule-v1", "advancing_site_count": 1, "chemistry_pass_count": 2})
    )
    pd.DataFrame({"input_sdf_index": [0, 1]}).to_parquet(
        artifacts / "ligand_first_candidates.parquet", index=False
    )
    (artifacts / "ligand_first_candidates.sdf").write_text("candidates\n")
    parent = "test-covalentize-0001"

    carsidock_status = inspect_ligand_chain(tmp_path, config)
    assert carsidock_status.next_stage == "carsidock"
    carsidock_request = compile_next_ligand_request(
        tmp_path, config, carsidock_status
    )
    assert carsidock_request is not None
    assert carsidock_request.parameters["reference_ligand"] == (
        "state/jobs/test-structure-0001/artifacts/parent_ligand_pose.sdf"
    )

    artifacts = _complete(tmp_path, "test-carsidock-0001", "carsidock-cov", parent)
    pd.DataFrame({"input_sdf_index": [0, 1], "engine_status": ["SUCCESS", "SUCCESS"], "raw_score": [-2.0, -1.0]}).to_parquet(
        artifacts / "carsidock_scores.parquet", index=False
    )
    (artifacts / "carsidock_scores_audit.json").write_text("{}\n")
    parent = "test-carsidock-0001"

    artifacts = _complete(tmp_path, "test-selection-0001", "carsidock-pool-selection", parent)
    (artifacts / "stage3_selection_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"identity": True}})
    )
    pd.DataFrame({"input_sdf_index": [0, 1]}).to_parquet(
        artifacts / "stage3_recall_pool.parquet", index=False
    )
    parent = "test-selection-0001"

    artifacts = _complete(tmp_path, "test-af3-0001", "alphafold3-mpae", parent)
    (artifacts / "stage4_selection_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"coverage": True}})
    )
    pd.DataFrame({"input_sdf_index": [0, 1]}).to_parquet(
        artifacts / "stage4_top_candidates.parquet", index=False
    )
    parent = "test-af3-0001"

    covdock_status = inspect_ligand_chain(tmp_path, config)
    assert covdock_status.next_stage == "covdock_lo"
    covdock_request = compile_next_ligand_request(
        tmp_path, config, covdock_status
    )
    assert covdock_request is not None
    assert covdock_request.engine == "schrodinger-covdock-lo"
    assert covdock_request.parameters["checkpoint_size"] == 1
    assert covdock_request.parameters["reaction_type_column"] == "covdock_reaction_type"

    artifacts = _complete(tmp_path, "test-covdock-0001", "schrodinger-covdock-lo", parent)
    (artifacts / "covdock_lo_registered_manifest.json").write_text(
        json.dumps({"status": "completed", "gate_quality_gates": {"identity": True}})
    )
    pd.DataFrame({"input_sdf_index": [0, 1]}).to_parquet(
        artifacts / "stage5_gated_candidates.parquet", index=False
    )
    parent = "test-covdock-0001"

    artifacts = _complete(tmp_path, "test-evaluation-0001", "ligand-first-evaluation", parent)
    (artifacts / "ligand_first_evaluation_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"identity": True}})
    )
    pd.DataFrame({"input_sdf_index": [0, 1]}).to_parquet(
        artifacts / "ligand_first_evaluated_candidates.parquet", index=False
    )
    status = inspect_ligand_chain(tmp_path, config)
    assert status.next_stage == "pareto"
    request = compile_next_ligand_request(tmp_path, config, status)
    assert request is not None
    assert request.engine == "ligand-first-pareto"
    assert request.parent_run_id == "test-evaluation-0001"
    assert request.parameters["_pareto_profile"] == "ligand-first"
    assert request.resource_budget.max_runtime_minutes == 15
    assert request.resource_budget.cpu_count == 2
    assert EXTERNAL_STAGES["ligand-first-pareto"] == (0, 2, 16, 15)

    artifacts = _complete(tmp_path, "test-pareto-0001", "ligand-first-pareto", "test-evaluation-0001")
    manifest_path = artifacts / "stage6_pareto_manifest.json"
    manifest = {
        "status": "completed",
        "selection_policy": {"route_profile": "ligand-first"},
        "quality_gates": {"selection": True},
        "outputs": {},
    }
    manifest_path.write_text(json.dumps(manifest))
    incomplete = inspect_ligand_chain(tmp_path, config)
    assert incomplete.terminal_state == "quality_gate_failed"

    trace_json = artifacts / "ligand_first_traceability_report.json"
    trace_md = artifacts / "ligand_first_traceability_report.md"
    trace_json.write_text(
        json.dumps(
            {
                "route": "ligand-first",
                "chain_config": {
                    "path": config.trace_chain_config,
                    "sha256": sha256_file(chain_config),
                },
                "stage_count": 8,
                "stages": [
                    {"run_id": "test-structure-0001", "engine": "ligand-first-structure-intake", "state": "COMPLETED"},
                    {"run_id": "test-covalentize-0001", "engine": "ligand-first-covalentization", "state": "COMPLETED"},
                    {"run_id": "test-carsidock-0001", "engine": "carsidock-cov", "state": "COMPLETED"},
                    {"run_id": "test-selection-0001", "engine": "carsidock-pool-selection", "state": "COMPLETED"},
                    {"run_id": "test-af3-0001", "engine": "alphafold3-mpae", "state": "COMPLETED"},
                    {"run_id": "test-covdock-0001", "engine": "schrodinger-covdock-lo", "state": "COMPLETED"},
                    {"run_id": "test-evaluation-0001", "engine": "ligand-first-evaluation", "state": "COMPLETED"},
                    {"run_id": "test-pareto-0001", "engine": "ligand-first-pareto", "state": "COMPLETED"},
                ],
            }
        )
    )
    trace_md.write_text("# Ligand-first traceability report\n")
    manifest["outputs"] = {
        trace_json.name: {"sha256": sha256_file(trace_json)},
        trace_md.name: {"sha256": sha256_file(trace_md)},
    }
    manifest_path.write_text(json.dumps(manifest))
    complete = inspect_ligand_chain(tmp_path, config)
    assert complete.terminal_state == "completed"
    assert complete.next_stage is None


def test_independent_evaluation_request_fits_external_inspire_stage_ceiling(
    tmp_path: Path,
):
    """The continuation must not compile a job the dispatcher will reject."""
    for name in (
        "campaign.json",
        "protein.pdb",
        "parent.sdf",
        "af3.json",
        "receptor.mae",
        "target.yaml",
    ):
        (tmp_path / name).write_text(f"test {name}\n")
    config_path = tmp_path / "continuation.yaml"
    config_path.write_text("schema_version: '1.0'\n")
    config = LigandFirstChainConfig(
        campaign_id="continuation",
        project_id="test-ligand",
        campaign_path="campaign.json",
        trace_chain_config="continuation.yaml",
        target_config="target.yaml",
        pdb="protein.pdb",
        reference_ligand="parent.sdf",
        af3_source_data_json="af3.json",
        covdock_receptor="receptor.mae",
        attachment_residue="A:797",
        covres="A:CYS:773",
        stage_order=["af3", "covdock_lo", "independent_evaluation", "pareto"],
        seed_run_ids={"af3": "test-af3-seed", "covdock_lo": "test-covdock-seed"},
        dispatch_policy="ligand-smoke-v1",
        dispatch_batch="ligand-smoke-test",
        carsidock_top_n=3,
        af3_top_n=3,
        covdock_top_n=3,
        final_n=3,
        covdock_cpu_count=4,
    )
    af3_artifacts = _complete(
        tmp_path, "test-af3-seed", "alphafold3-mpae", None
    )
    (af3_artifacts / "stage4_selection_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"coverage": True}})
    )
    pd.DataFrame({"input_sdf_index": [58, 60, 66]}).to_parquet(
        af3_artifacts / "stage4_top_candidates.parquet", index=False
    )
    covdock_artifacts = _complete(
        tmp_path, "test-covdock-seed", "schrodinger-covdock-lo", None
    )
    (covdock_artifacts / "covdock_lo_registered_manifest.json").write_text(
        json.dumps({"status": "completed", "gate_quality_gates": {"identity": True}})
    )
    pd.DataFrame(
        {
            "input_sdf_index": [58, 60, 66],
            "compound_id": ["a", "b", "c"],
        }
    ).to_parquet(
        covdock_artifacts / "stage5_gated_candidates.parquet", index=False
    )

    status = inspect_ligand_chain(tmp_path, config)
    assert status.next_stage == "independent_evaluation"
    request = compile_next_ligand_request(tmp_path, config, status)
    assert request is not None
    assert request.engine == "ligand-first-evaluation"
    assert request.parent_run_id is None
    assert request.parameters["upstream_run_id"] == "test-covdock-seed"
    assert request.parameters["dispatch_policy"] == "ligand-smoke-v1"
    assert request.parameters["dispatch_batch"] == "ligand-smoke-test"
    assert request.parameters["recheck_coordinates"] is True
    assert request.parameters["attachment_residue"] == "A:797"
    assert request.resource_budget.max_runtime_minutes == 15
    validate_boundary(
        SimpleNamespace(
            engine=request.engine,
            route=request.route,
            parameters={**request.parameters, "input_row_count": 3},
            resource_budget=request.resource_budget,
            provenance={"worker_backend": "external-inspire"},
            attempt=1,
            parent_run_id=request.parent_run_id,
        )
    )


def test_scientific_upstream_is_discovered_without_retry_parent(tmp_path: Path):
    """A crash before controller state write must not orphan a registered stage."""
    for name in (
        "campaign.json", "protein.pdb", "parent.sdf", "af3.json",
        "receptor.mae", "target.yaml",
    ):
        (tmp_path / name).write_text(f"test {name}\n")
    (tmp_path / "continuation.yaml").write_text("schema_version: '1.0'\n")
    config = LigandFirstChainConfig(
        campaign_id="continuation", project_id="test-ligand",
        campaign_path="campaign.json", trace_chain_config="continuation.yaml",
        target_config="target.yaml", pdb="protein.pdb",
        reference_ligand="parent.sdf", af3_source_data_json="af3.json",
        covdock_receptor="receptor.mae", attachment_residue="A:797",
        covres="A:CYS:773",
        stage_order=["af3", "covdock_lo", "independent_evaluation", "pareto"],
        seed_run_ids={"af3": "test-af3-seed", "covdock_lo": "test-covdock-seed"},
        carsidock_top_n=3, af3_top_n=3, covdock_top_n=3, final_n=3,
        covdock_cpu_count=4,
    )
    af3_artifacts = _complete(tmp_path, "test-af3-seed", "alphafold3-mpae", None)
    (af3_artifacts / "stage4_selection_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"coverage": True}})
    )
    pd.DataFrame({"input_sdf_index": [58, 60, 66]}).to_parquet(
        af3_artifacts / "stage4_top_candidates.parquet", index=False
    )
    covdock_artifacts = _complete(
        tmp_path, "test-covdock-seed", "schrodinger-covdock-lo", None
    )
    (covdock_artifacts / "covdock_lo_registered_manifest.json").write_text(
        json.dumps({"status": "completed", "gate_quality_gates": {"identity": True}})
    )
    pd.DataFrame({"input_sdf_index": [58, 60, 66]}).to_parquet(
        covdock_artifacts / "stage5_gated_candidates.parquet", index=False
    )
    evaluation = _complete(
        tmp_path, "test-evaluation-untracked", "ligand-first-evaluation", None
    )
    registry = ArtifactRegistry(tmp_path)
    # _complete writes a result only; add the minimal immutable spec used by
    # chain discovery to separate scientific lineage from retry ancestry.
    from covscreen.jobs import JobSpec, ResourceBudget
    registry.write_spec(
        JobSpec(
            run_id="test-evaluation-untracked", request_id="eval-untracked",
            project_id="test-ligand", route="ligand-first",
            engine="ligand-first-evaluation", command=["python"],
            working_directory=str(tmp_path), output_directory=str(evaluation),
            parameters={"upstream_run_id": "test-covdock-seed"},
            resource_budget=ResourceBudget(max_runtime_minutes=15, cpu_count=4,
                                           memory_gb=16, max_candidates=3),
            execution_mode="execute", human_approved=True,
            approval_note="approved bounded continuation test",
        )
    )
    (evaluation / "ligand_first_evaluation_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"identity": True}})
    )
    pd.DataFrame({"input_sdf_index": [58, 60, 66]}).to_parquet(
        evaluation / "ligand_first_evaluated_candidates.parquet", index=False
    )

    status = inspect_ligand_chain(tmp_path, config)
    observed = next(
        stage for stage in status.stages if stage.stage == "independent_evaluation"
    )
    assert observed.run_id == "test-evaluation-untracked"
    assert status.next_stage == "pareto"


def test_continuation_scope_adopts_only_explicit_seeded_runs(tmp_path: Path):
    for name in ("campaign.json", "protein.pdb", "parent.sdf", "af3.json", "receptor.mae", "target.yaml"):
        (tmp_path / name).write_text(f"test {name}\n")
    config_path = tmp_path / "continuation.yaml"
    config_path.write_text("schema_version: '1.0'\n")
    config = LigandFirstChainConfig(
        campaign_id="continuation",
        project_id="test-ligand",
        campaign_path="campaign.json",
        trace_chain_config="continuation.yaml",
        target_config="target.yaml",
        pdb="protein.pdb",
        reference_ligand="parent.sdf",
        af3_source_data_json="af3.json",
        covdock_receptor="receptor.mae",
        attachment_residue="A:797",
        covres="A:CYS:773",
        stage_order=["af3", "covdock_lo", "independent_evaluation", "pareto"],
        seed_run_ids={"af3": "test-af3-seed", "covdock_lo": "test-covdock-seed"},
    )
    artifacts = _complete(tmp_path, "test-af3-seed", "alphafold3-mpae", None)
    (artifacts / "stage4_selection_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"coverage": True}})
    )
    pd.DataFrame({"input_sdf_index": [58, 60, 66]}).to_parquet(
        artifacts / "stage4_top_candidates.parquet", index=False
    )
    registry = ArtifactRegistry(tmp_path)
    run_dir = registry.job_dir("test-covdock-seed")
    run_dir.mkdir(parents=True)
    queued = RunResult(
        run_id="test-covdock-seed", state="QUEUED", engine="schrodinger-covdock-lo",
        route="ligand-first", project_id="test-ligand", submitted_at=utc_now(),
        message="queued continuation", stdout_path="stdout.log", stderr_path="stderr.log",
    )
    registry._write_json(run_dir / "result.json", queued.model_dump(mode="json"))

    status = inspect_ligand_chain(tmp_path, config)
    assert status.terminal_state == "active"
    assert [stage.stage for stage in status.stages] == ["af3", "covdock_lo"]
    assert status.stages[-1].run_id == "test-covdock-seed"


@pytest.mark.parametrize(
    "stages",
    [
        ["af3", "independent_evaluation", "pareto"],
        ["af3", "covdock_lo"],
        ["af3", "af3", "pareto"],
    ],
)
def test_continuation_scope_rejects_skips_missing_pareto_and_duplicates(stages):
    with pytest.raises(ValueError, match="stage_order"):
        LigandFirstChainConfig(
            campaign_id="bad", project_id="bad", campaign_path="campaign.json",
            trace_chain_config="chain.yaml", target_config="target.yaml",
            pdb="protein.pdb", reference_ligand="parent.sdf",
            af3_source_data_json="af3.json", covdock_receptor="receptor.mae",
            attachment_residue="A:797", covres="A:CYS:773", stage_order=stages,
        )


def test_provenance_records_the_approved_continuation_scope(tmp_path):
    config = tmp_path / "continuation.yaml"
    config.write_text(
        """schema_version: '1.0'
stage_order: [af3, covdock_lo, independent_evaluation, pareto]
campaign_id: continuation
project_id: continuation
campaign_path: campaign.json
trace_chain_config: continuation.yaml
target_config: target.yaml
pdb: protein.pdb
reference_ligand: parent.sdf
af3_source_data_json: af3.json
covdock_receptor: receptor.mae
attachment_residue: A:797
covres: A:CYS:773
"""
    )
    record = ligand_chain_provenance(tmp_path, config)
    assert record["stage_order"] == [
        "af3", "covdock_lo", "independent_evaluation", "pareto"
    ]
    assert record["config_sha256"] == sha256_file(config)
