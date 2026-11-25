from __future__ import annotations

import json
from pathlib import Path

from covscreen.dashboard import refresh_dashboard
from covscreen.jobs import ArtifactRegistry, JobSpec, ResourceBudget, RunResult


def test_dashboard_tracks_latest_prescreen_job_and_outputs(tmp_path: Path):
    (tmp_path / "results").mkdir()
    (tmp_path / "results" / "dashboard.json").write_text(
        json.dumps({"schema_version": "1.0", "tasks": [], "candidates": [], "outputs": []})
    )
    registry = ArtifactRegistry(tmp_path)
    run_id = "20260901T000000-covindb-prescreen-test"
    artifact_dir = registry.job_dir(run_id) / "artifacts"
    spec = JobSpec(
        run_id=run_id,
        request_id="dashboard-prescreen-test",
        project_id="egfr-c797",
        route="library-first",
        engine="covindb-prescreen",
        command=["python", "run.py"],
        working_directory=str(tmp_path),
        output_directory=str(artifact_dir),
        parameters={"max_rows": 10000},
        provenance={"dataset": "CovalentInDB 2.0 Virtual Screening Library"},
        resource_budget=ResourceBudget(max_runtime_minutes=60, max_candidates=500),
    )
    result = RunResult(
        run_id=run_id,
        state="COMPLETED",
        engine="covindb-prescreen",
        route="library-first",
        project_id="egfr-c797",
        submitted_at="2026-09-01T00:00:00+00:00",
        finished_at="2026-09-01T00:01:00+00:00",
        message="Job completed.",
        stdout_path="stdout.log",
        stderr_path="stderr.log",
    )
    registry.create(spec, result)
    artifact_dir.mkdir()
    (artifact_dir / "progress.json").write_text(
        json.dumps(
            {
                "counts": {"input_rows": 10000, "unique": 9995, "selected": 500},
                "quality_gates": {"rdkit_parse_rate_ge_0_99": True},
            }
        )
    )
    (artifact_dir / "stage2_selection_manifest.json").write_text(
        json.dumps(
            {
                "target": {"site_id": "EGFR_C797"},
                "quality": {"selected_warhead_mapping_rate": 1.0},
            }
        )
    )
    (artifact_dir / "stage2_candidates.parquet").write_bytes(b"parquet")
    dashboard = refresh_dashboard(tmp_path, run_id)
    assert dashboard["library_first"]["run_id"] == run_id
    assert dashboard["library_first"]["selected"] == 500
    assert dashboard["library_first"]["stages"][2]["status"] == "completed"
    assert dashboard["library_first"]["stages"][3]["status"] == "pending"
    assert dashboard["tasks"][0]["status"] == "completed"
    assert dashboard["outputs"][0]["path"].endswith("stage2_selection_manifest.json")


def test_dashboard_tracks_every_registered_library_first_stage(tmp_path: Path):
    (tmp_path / "results").mkdir()
    registry = ArtifactRegistry(tmp_path)

    def add_job(engine: str, stamp: int, manifest_name: str, payload: dict):
        run_id = f"20260901T000{stamp:02d}-{engine}-test"
        artifact_dir = registry.job_dir(run_id) / "artifacts"
        spec = JobSpec(
            run_id=run_id,
            request_id=f"request-{engine}",
            project_id="egfr-c797",
            route="library-first",
            engine=engine,
            command=["python", "run.py"],
            working_directory=str(tmp_path),
            output_directory=str(artifact_dir),
            resource_budget=ResourceBudget(max_runtime_minutes=60, max_candidates=100),
        )
        result = RunResult(
            run_id=run_id,
            state="COMPLETED",
            engine=engine,
            route="library-first",
            project_id="egfr-c797",
            submitted_at=f"2026-09-01T00:00:{stamp:02d}+00:00",
            finished_at=f"2026-09-01T00:01:{stamp:02d}+00:00",
            message="Job completed.",
            stdout_path="stdout.log",
            stderr_path="stderr.log",
        )
        registry.create(spec, result)
        artifact_dir.mkdir()
        (artifact_dir / manifest_name).write_text(json.dumps(payload))
        return run_id, artifact_dir

    prescreen_id, prescreen_dir = add_job(
        "covindb-prescreen",
        1,
        "stage2_selection_manifest.json",
        {
            "counts": {"input_rows": 1000, "unique": 990, "selected": 100},
            "quality_gates": {"identity_loss_zero": True},
            "target": {"site_id": "EGFR_C797"},
        },
    )
    # The running prescreen writer is the alternate count source used by the dashboard.
    (prescreen_dir / "progress.json").write_text(
        json.dumps({"counts": {"input_rows": 1000, "unique": 990, "selected": 100}})
    )
    add_job(
        "carsidock-cov",
        2,
        "carsidock_scores_audit.json",
        {"input_rows": 100, "success_rows": 80},
    )
    add_job(
        "rtmscore",
        3,
        "rtmscore.run_manifest.json",
        {"output_row_count": 100, "scored_row_count": 78},
    )
    add_job(
        "carsidock-pool-selection",
        4,
        "stage3_selection_manifest.json",
        {
            "quality": {"input_identity_count": 100, "selected_identity_count": 50},
            "quality_gates": {"identity_loss_zero": True},
        },
    )
    add_job(
        "alphafold3-mpae",
        5,
        "stage4_selection_manifest.json",
        {
            "quality": {"input_identity_count": 50, "selected_count": 10},
            "quality_gates": {"coverage_ge_minimum": True},
        },
    )
    add_job(
        "schrodinger-covdock-lo",
        6,
        "stage5_gate_manifest.json",
        {
            "quality": {"input_identity_count": 10, "final_candidate_count": 5},
            "quality_gates": {"covdock_success_rate_ge_minimum": True},
        },
    )
    pareto_id, pareto_dir = add_job(
        "library-first-pareto",
        7,
        "stage6_pareto_manifest.json",
        {
            "quality": {"input_count": 5, "selected_count": 3},
            "quality_gates": {"baseline_objectives_available": True},
        },
    )
    (pareto_dir / "stage6_final_candidates.parquet").write_bytes(b"parquet")
    dashboard = refresh_dashboard(tmp_path, pareto_id)
    assert dashboard["library_first"]["state"] == "completed"
    assert dashboard["library_first"]["progress"] == 100.0
    assert dashboard["library_first"]["selected"] == 3
    assert dashboard["library_first"]["run_id"] == pareto_id
    stages = {stage["id"]: stage for stage in dashboard["library_first"]["stages"]}
    assert stages["carsidock"]["count"] == 80
    assert stages["rtmscore"]["count"] == 78
    assert stages["recall_pool"]["count"] == 50
    assert stages["af3"]["count"] == 10
    assert stages["covdock"]["count"] == 5
    assert stages["pareto"]["count"] == 3
    assert dashboard["library_first"]["quality_gates"][
        "pareto:baseline_objectives_available"
    ] is True
    assert any(
        item["path"].endswith("stage6_final_candidates.parquet")
        for item in dashboard["outputs"]
    )


def test_dashboard_prefers_declared_parent_linked_chain_over_orphan_plan(tmp_path: Path):
    (tmp_path / "results").mkdir()
    registry = ArtifactRegistry(tmp_path)

    prescreen_id = "20260901T000000-covindb-prescreen-chain0"
    artifact_dir = registry.job_dir(prescreen_id) / "artifacts"
    spec = JobSpec(
        run_id=prescreen_id,
        request_id="chain-prescreen",
        project_id="egfr-c797",
        route="library-first",
        engine="covindb-prescreen",
        command=["true"],
        working_directory=str(tmp_path),
        output_directory=str(artifact_dir),
        parameters={"max_rows": 10},
        provenance={"dataset": "CovalentInDB 2.0"},
        resource_budget=ResourceBudget(max_runtime_minutes=10, max_candidates=2),
    )
    result = RunResult(
        run_id=prescreen_id,
        state="COMPLETED",
        engine="covindb-prescreen",
        route="library-first",
        project_id="egfr-c797",
        submitted_at="2026-09-01T00:00:00+00:00",
        message="done",
        stdout_path="stdout.log",
        stderr_path="stderr.log",
    )
    registry.create(spec, result)
    artifact_dir.mkdir()
    (artifact_dir / "stage2_selection_manifest.json").write_text(
        json.dumps(
            {
                "status": "completed",
                "counts": {"input_rows": 10, "unique": 10, "selected": 2},
                "quality_gates": {"identity_loss_zero": True},
            }
        )
    )
    (artifact_dir / "progress.json").write_text(
        json.dumps({"counts": {"input_rows": 10, "unique": 10, "selected": 2}})
    )
    import pandas as pd

    pd.DataFrame(
        {
            "input_sdf_index": [0, 1],
            "compound_id": ["A", "B"],
            "standardized_smiles": ["CC", "CCC"],
        }
    ).to_parquet(artifact_dir / "stage2_candidates.parquet", index=False)
    (artifact_dir / "stage2_candidates.sdf").write_text("$$$$\n$$$$\n")

    orphan_id = "20260901T010000-carsidock-cov-orphan0"
    orphan_spec = JobSpec(
        run_id=orphan_id,
        request_id="orphan-plan",
        project_id="egfr-c797",
        route="library-first",
        engine="carsidock-cov",
        command=["true"],
        working_directory=str(tmp_path),
        output_directory=str(registry.job_dir(orphan_id) / "artifacts"),
        resource_budget=ResourceBudget(max_runtime_minutes=10, gpu_count=1, max_candidates=2),
    )
    orphan_result = RunResult(
        run_id=orphan_id,
        state="PLANNED",
        engine="carsidock-cov",
        route="library-first",
        project_id="egfr-c797",
        submitted_at="2026-09-01T01:00:00+00:00",
        message="plan",
        stdout_path="stdout.log",
        stderr_path="stderr.log",
    )
    registry.create(orphan_spec, orphan_result)

    config_dir = tmp_path / "configs" / "library_first"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "pilot.yaml"
    config_path.write_text(
        "\n".join(
            [
                'schema_version: "1.0"',
                "campaign_id: pilot",
                "project_id: egfr-c797",
                "target_config: x",
                "pdb: x",
                "reference_ligand: x",
                "af3_source_data_json: x",
                "covdock_receptor: x",
                'attachment_residue: "A:797"',
                'covres: "A:CYS:773"',
                "seed_run_ids:",
                f"  prescreen: {prescreen_id}",
            ]
        )
    )
    chain_dir = tmp_path / "state" / "chains" / "pilot"
    chain_dir.mkdir(parents=True)
    (chain_dir / "chain_status.json").write_text(
        json.dumps(
            {
                "provenance": {
                    "config_path": "configs/library_first/pilot.yaml"
                }
            }
        )
    )

    dashboard = refresh_dashboard(tmp_path)
    stages = {stage["id"]: stage for stage in dashboard["library_first"]["stages"]}
    assert stages["carsidock"]["status"] == "pending"
    assert dashboard["library_first"]["campaign_id"] == "pilot"
    assert dashboard["library_first"]["next_stage"] == "carsidock"
