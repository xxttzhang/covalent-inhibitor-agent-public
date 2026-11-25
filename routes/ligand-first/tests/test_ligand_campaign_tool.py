from pathlib import Path

import json
import pandas as pd

import pytest

from covscreen.tool_api import ligand_first_campaign_tool
from covscreen.jobs import ArtifactRegistry, RunResult, utc_now

ROOT = (Path(__file__).resolve().parents[3] / "science")
CONFIG = "configs/ligand_first/EGFR_C797_erlotinib_v1.yaml"


def test_campaign_plan_binds_runtime_and_configuration():
    first = ligand_first_campaign_tool(ROOT, CONFIG, max_runtime_minutes=60)
    second = ligand_first_campaign_tool(ROOT, CONFIG, max_runtime_minutes=120)
    assert first["status"] == "PLANNED"
    assert len(first["plan"]["stage_order"]) == 8
    assert first["plan_sha256"] != second["plan_sha256"]
    assert first["plan"]["requires_configured_execution_host"]
    assert first["plan"]["route"] == "ligand-first"
    assert first["plan"]["project_id"] == "egfr-c797"
    assert set(first["plan"]["stage_engines"]) == set(first["plan"]["stage_order"])
    assert set(first["plan"]["executable_stage_engines"]) == set(first["plan"]["stage_order"])
    assert first["plan"]["executable_stage_order"] == first["plan"]["stage_order"]
    assert set(first["plan"]["candidate_ceiling_by_stage"]) == set(first["plan"]["stage_order"])
    assert set(first["plan"]["resource_ceiling_by_stage"]) == set(first["plan"]["stage_order"])


def test_campaign_plan_uses_approved_continuation_stage_scope(tmp_path):
    root = tmp_path
    (root / "configs").mkdir()
    (root / "campaign.json").write_text("{}\n")
    for name in ("target.yaml", "protein.pdb", "parent.sdf", "af3.json", "receptor.mae"):
        (root / name).write_text("test\n")
    config = root / "configs/continuation.yaml"
    config.write_text(
        """schema_version: '1.0'
stage_order: [af3, covdock_lo, independent_evaluation, pareto]
campaign_id: continuation
project_id: continuation
campaign_path: campaign.json
trace_chain_config: configs/continuation.yaml
target_config: target.yaml
pdb: protein.pdb
reference_ligand: parent.sdf
af3_source_data_json: af3.json
covdock_receptor: receptor.mae
attachment_residue: A:797
covres: A:CYS:773
"""
    )
    result = ligand_first_campaign_tool(
        root, "configs/continuation.yaml", max_runtime_minutes=60
    )
    assert result["plan"]["stage_order"] == [
        "af3", "covdock_lo", "independent_evaluation", "pareto"
    ]


def test_real_three_candidate_continuation_approval_is_bounded_to_three():
    result = ligand_first_campaign_tool(
        ROOT, "configs/ligand_first/EGFR_C797_smoke_continuation_20261008.yaml",
        max_runtime_minutes=240,
    )
    assert result["plan"]["candidate_ceiling_by_stage"] == {
        "af3": 3, "covdock_lo": 3, "independent_evaluation": 3, "pareto": 3,
    }
    assert result["plan"]["seed_run_ids"] == {
        "af3": "20261008T045824-alphafold3-mpae-106090ee3f",
        "covdock_lo": "20261008T065829-schrodinger-covdock-lo-0a334174e1",
    }
    assert result["plan"]["executable_stage_order"] == [
        "independent_evaluation", "pareto"
    ]
    assert result["plan"]["executable_stage_engines"] == {
        "independent_evaluation": "ligand-first-evaluation",
        "pareto": "ligand-first-pareto",
    }
    assert result["plan"]["resource_ceiling_by_stage"] == {
        "independent_evaluation": {"gpu_count": 0, "cpu_count": 4, "memory_gb": 16},
        "pareto": {"gpu_count": 0, "cpu_count": 2, "memory_gb": 16},
    }
    assert result["plan"]["peak_gpu_count"] == 0
    assert result["plan"]["peak_cpu_count"] == 4
    assert result["plan"]["peak_memory_gb"] == 16
    assert result["plan"]["dispatch_policy"] == "ligand-smoke-v1"
    assert result["plan"]["dispatch_batch"] == "ligand-smoke-20260929-v3"


def test_post_covdock_continuation_adopts_serial_retry_without_reauthorizing_it():
    result = ligand_first_campaign_tool(
        ROOT,
        "configs/ligand_first/EGFR_C797_smoke_post_covdock_20261008.yaml",
        max_runtime_minutes=30,
    )
    assert result["plan"]["seed_run_ids"] == {
        "af3": "20261008T045824-alphafold3-mpae-106090ee3f",
        "covdock_lo": "20261008T092158-schrodinger-covdock-lo-731833321e",
    }
    assert result["plan"]["executable_stage_order"] == [
        "independent_evaluation", "pareto",
    ]
    assert "af3" not in result["plan"]["resource_ceiling_by_stage"]
    assert "covdock_lo" not in result["plan"]["resource_ceiling_by_stage"]
    assert result["plan"]["candidate_ceiling_by_stage"] == {
        "af3": 3, "covdock_lo": 3, "independent_evaluation": 3, "pareto": 3,
    }
    assert result["plan"]["peak_gpu_count"] == 0
    assert result["plan"]["peak_cpu_count"] == 4
    assert result["plan"]["peak_memory_gb"] == 16


def test_campaign_start_rejects_unapproved_budget_before_launch(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("No process should launch")
    monkeypatch.setattr("covscreen.tool_api.subprocess.Popen", forbidden)
    plan = ligand_first_campaign_tool(ROOT, CONFIG, max_runtime_minutes=60)
    with pytest.raises(ValueError, match="exact campaign plan"):
        ligand_first_campaign_tool(ROOT, CONFIG, action="start", max_runtime_minutes=120,
            human_approved=True, approval_note="approve 60 minutes only",
            approved_plan_sha256=plan["plan_sha256"])


def test_missing_controller_state_is_not_claimed_live():
    result = ligand_first_campaign_tool(ROOT, CONFIG, action="status", controller_id="0" * 32)
    assert result["status"] == "NO_CONTROLLER_STATE"


def test_campaign_status_refreshes_from_registry_and_pinpoints_failed_stage(tmp_path):
    for name in ("campaign.json", "protein.pdb", "parent.sdf", "af3.json", "receptor.mae", "target.yaml"):
        (tmp_path / name).write_text(f"test {name}\n")
    (tmp_path / "configs").mkdir()
    config = tmp_path / "configs/continuation.yaml"
    config.write_text(
        """schema_version: '1.0'
stage_order: [af3, covdock_lo, independent_evaluation, pareto]
campaign_id: continuation
project_id: continuation
campaign_path: campaign.json
trace_chain_config: configs/continuation.yaml
target_config: target.yaml
pdb: protein.pdb
reference_ligand: parent.sdf
af3_source_data_json: af3.json
covdock_receptor: receptor.mae
attachment_residue: A:797
covres: A:CYS:773
seed_run_ids:
  af3: af3-seed
  covdock_lo: covdock-seed
carsidock_top_n: 3
af3_top_n: 3
covdock_top_n: 3
final_n: 3
covdock_cpu_count: 4
"""
    )
    registry = ArtifactRegistry(tmp_path)
    af3 = registry.job_dir("af3-seed")
    (af3 / "artifacts").mkdir(parents=True)
    registry._write_json(af3 / "result.json", RunResult(
        run_id="af3-seed", state="COMPLETED", engine="alphafold3-mpae",
        route="ligand-first", project_id="continuation", submitted_at=utc_now(),
        finished_at=utc_now(), message="done", stdout_path="stdout.log", stderr_path="stderr.log",
    ).model_dump(mode="json"))
    (af3 / "artifacts/stage4_selection_manifest.json").write_text(
        json.dumps({"status": "completed", "quality_gates": {"coverage": True}})
    )
    pd.DataFrame({"input_sdf_index": [1, 2, 3]}).to_parquet(
        af3 / "artifacts/stage4_top_candidates.parquet", index=False
    )
    covdock = registry.job_dir("covdock-seed")
    covdock.mkdir(parents=True)
    queued = RunResult(
        run_id="covdock-seed", state="QUEUED", engine="schrodinger-covdock-lo",
        route="ligand-first", project_id="continuation", submitted_at=utc_now(),
        message="queued", stdout_path="stdout.log", stderr_path="stderr.log",
    )
    registry._write_json(covdock / "result.json", queued.model_dump(mode="json"))
    planned = ligand_first_campaign_tool(
        tmp_path, "configs/continuation.yaml", max_runtime_minutes=240
    )
    controller_id = "1" * 32
    controller = tmp_path / "state/ligand_controllers/runs" / controller_id
    controller.mkdir(parents=True)
    (controller / "approved_plan.json").write_text(json.dumps({
        "plan": planned["plan"], "plan_sha256": planned["plan_sha256"],
        "approval_note": "test", "native_approval": "test",
    }))
    (controller / "campaign_controller.json").write_text(json.dumps({
        "schema_version": "1.0", "campaign_id": "continuation",
        "run_ids": {"af3": "af3-seed", "covdock_lo": "covdock-seed"},
        "status": "active",
    }))

    live = ligand_first_campaign_tool(
        tmp_path, "unused", action="status", controller_id=controller_id
    )
    assert live["status"] == "ACTIVE"
    assert live["live_registry_observed"] is True
    assert live["observed_chain"]["stages"][-1]["stage"] == "covdock_lo"
    assert live["duplicate_submission_allowed"] is False

    failed = queued.model_copy(update={
        "state": "FAILED", "finished_at": utc_now(),
        "failure_reason": "test_failure", "message": "failed",
    })
    registry._write_json(covdock / "result.json", failed.model_dump(mode="json"))
    observed = ligand_first_campaign_tool(
        tmp_path, "unused", action="status", controller_id=controller_id
    )
    assert observed["status"] == "REQUIRES_REVIEW"
    assert observed["failure"]["stage"] == "covdock_lo"
    assert observed["failure"]["run_id"] == "covdock-seed"
