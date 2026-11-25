import json
from pathlib import Path

import pandas as pd

from covscreen.jobs import (
    ArtifactRegistry, JobSpec, ResourceBudget, RunResult, sha256_file, utc_now,
)
from covscreen.ligand_acceptance import audit_ligand_p0_closure


RUNS = {
    "af3": "accept-af3-0001",
    "covdock": "accept-covdock-0001",
    "evaluation": "accept-evaluation-0001",
    "pareto": "accept-pareto-0001",
}
ENGINES = {
    "af3": "alphafold3-mpae",
    "covdock": "schrodinger-covdock-lo",
    "evaluation": "ligand-first-evaluation",
    "pareto": "ligand-first-pareto",
}


def _register(root: Path, stage: str, parent: str | None, parameters: dict) -> Path:
    registry = ArtifactRegistry(root)
    run_id, engine = RUNS[stage], ENGINES[stage]
    spec = JobSpec(
        run_id=run_id, request_id=f"request-{stage}", project_id="project",
        route="ligand-first", engine=engine, command=["true"],
        working_directory=str(root), output_directory=str(registry.job_dir(run_id) / "artifacts"),
        parameters=parameters, resource_budget=ResourceBudget(
            max_runtime_minutes=15, cpu_count=2, memory_gb=8, max_candidates=3
        ), parent_run_id=parent,
    )
    result = RunResult(
        run_id=run_id, state="COMPLETED", engine=engine, route="ligand-first",
        project_id="project", parent_run_id=parent, submitted_at=utc_now(),
        finished_at=utc_now(), return_code=0, runtime_seconds=1,
        message="completed", stdout_path="stdout.log", stderr_path="stderr.log",
    )
    directory = registry.create(spec, result) / "artifacts"
    directory.mkdir()
    return directory


def _manifest(directory: Path, name: str, files: list[Path], *, grouped=False) -> None:
    quality_key = "gate_quality_gates" if grouped else "quality_gates"
    payload = {
        "status": "completed",
        quality_key: {"identity": True},
        "outputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)} for path in files
        },
    }
    (directory / name).write_text(json.dumps(payload))


def _fixture(root: Path):
    rows = pd.DataFrame({
        "input_sdf_index": [0, 1, 2],
        "compound_id": ["c0", "c1", "c2"],
        "target_id": ["EGFR"] * 3,
        "site_id": ["C797"] * 3,
        "parent_id": ["erlotinib"] * 3,
        "standardized_smiles": ["C#CC(=O)NC", "C#CC(=O)NCC", "C#CC(=O)NCCC"],
        "warhead_class": ["propiolamide"] * 3,
        "electrophilic_atom": ["C1"] * 3,
        "nucleophilic_atom": ["SG"] * 3,
        "af3_success": [True] * 3,
        "af3_mpae": [0.9, 1.0, 1.1],
    })
    af3 = _register(root, "af3", None, {})
    af3_table = af3 / "stage4_top_candidates.parquet"
    rows.to_parquet(af3_table, index=False)
    _manifest(af3, "stage4_selection_manifest.json", [af3_table])

    cov = _register(root, "covdock", None, {"upstream_run_id": RUNS["af3"]})
    cov_rows = rows.assign(
        covdock_success=[True] * 3, covdock_score=[-9.0, -8.0, -7.0],
        covdock_gate_detail=["A", "B1", "C"], physical_validity=["PASS", "WARN", "FAIL"],
    )
    cov_table = cov / "stage5_gated_candidates.parquet"
    cov_rows.to_parquet(cov_table, index=False)
    _manifest(cov, "covdock_lo_registered_manifest.json", [cov_table], grouped=True)

    evaluation = _register(root, "evaluation", RUNS["covdock"], {})
    evaluated = cov_rows.assign(independent_gate=["PASS", "WARN", "FAIL"])
    evaluation_table = evaluation / "ligand_first_evaluated_candidates.parquet"
    evaluated.to_parquet(evaluation_table, index=False)
    _manifest(evaluation, "ligand_first_evaluation_manifest.json", [evaluation_table])

    pareto = _register(root, "pareto", RUNS["evaluation"], {})
    ranked = evaluated.assign(
        pareto_eligible=[True, True, False],
        pareto_exclusion_reason=["", "", "physical_validity_FAIL"],
        pareto_front=[1.0, 2.0, float("nan")],
    )
    all_table = pareto / "stage6_pareto_all_candidates.parquet"
    final_table = pareto / "stage6_final_candidates.parquet"
    ranked.to_parquet(all_table, index=False)
    ranked.iloc[:2].assign(final_rank=[1, 2]).to_parquet(final_table, index=False)
    report = pareto / "stage6_selection_report.md"
    trace_json = pareto / "ligand_first_traceability_report.json"
    trace_md = pareto / "ligand_first_traceability_report.md"
    report.write_text("# report\n")
    trace_json.write_text("{}\n")
    trace_md.write_text("# trace\n")
    _manifest(
        pareto, "stage6_pareto_manifest.json",
        [all_table, final_table, report, trace_json, trace_md],
    )

    delivery = root / "delivery.csv"
    delivery_rows = ranked.copy()
    delivery_rows["af3_artifact_id"] = [f"af3-{i}" for i in range(3)]
    delivery_rows["af3_artifact_version"] = [f"af3-v{i}" for i in range(3)]
    # Start hashes with a hexadecimal letter so CSV type inference cannot
    # coerce an all-digit digest and discard leading zeroes.
    delivery_rows["af3_artifact_sha256"] = ["a" * 63 + str(i + 1) for i in range(3)]
    delivery_rows["covdock_artifact_id"] = ["covdock"] * 3
    delivery_rows["covdock_artifact_version"] = ["covdock-v1"] * 3
    delivery_rows["covdock_artifact_sha256"] = ["f" * 64] * 3
    delivery_rows["selection_report_artifact_id"] = ["selection-report"] * 3
    delivery_rows["selection_report_artifact_version"] = ["selection-report-v1"] * 3
    delivery_rows["selection_report_artifact_sha256"] = ["d" * 64] * 3
    delivery_rows["traceability_report_artifact_id"] = ["traceability-report"] * 3
    delivery_rows["traceability_report_artifact_version"] = ["traceability-report-v1"] * 3
    delivery_rows["traceability_report_artifact_sha256"] = ["e" * 64] * 3
    delivery_rows.to_csv(delivery, index=False)
    artifacts = {}
    for row in delivery_rows.itertuples():
        for prefix in ("af3", "covdock"):
            key = (getattr(row, f"{prefix}_artifact_id"), getattr(row, f"{prefix}_artifact_version"), getattr(row, f"{prefix}_artifact_sha256"))
            if not any(item["id"] == key[0] for item in artifacts.values()):
                artifacts[f"{prefix}-{len(artifacts)}"] = {"id": key[0], "current": {"id": key[1], "sha256": key[2]}}
    artifacts["selection-report"] = {"id": "selection-report", "current": {
        "id": "selection-report-v1", "sha256": "d" * 64,
    }}
    artifacts["traceability-report"] = {"id": "traceability-report", "current": {
        "id": "traceability-report-v1", "sha256": "e" * 64,
    }}
    receipt = root / "receipt.json"
    receipt.write_text(json.dumps({"artifacts": artifacts}))
    browser = root / "browser.json"
    browser.write_text(json.dumps({
        "production": True, "single_task_page": True, "candidate_count": 3,
        "stage_progress_visible": True, "structures_visible": True,
        "scores_visible": True, "gate_reasons_visible": True,
        "ranking_rationale_visible": True, "report_visible": True,
    }))
    return delivery, receipt, browser


def test_exact_three_candidate_closure_passes_all_evidence_gates(tmp_path):
    delivery, receipt, browser = _fixture(tmp_path)
    result = audit_ligand_p0_closure(
        tmp_path, RUNS, delivery_table=delivery, platform_receipt=receipt,
        browser_verification=browser,
    )
    assert result["status"] == "PASS", result["failed_gates"]
    assert not result["failed_gates"]


def test_failed_covdock_score_cannot_be_filled_with_zero(tmp_path):
    delivery, receipt, browser = _fixture(tmp_path)
    table = tmp_path / "state/jobs" / RUNS["covdock"] / "artifacts/stage5_gated_candidates.parquet"
    frame = pd.read_parquet(table)
    frame.loc[0, ["covdock_success", "covdock_score"]] = [False, 0.0]
    frame.to_parquet(table, index=False)
    result = audit_ligand_p0_closure(
        tmp_path, RUNS, delivery_table=delivery, platform_receipt=receipt,
        browser_verification=browser,
    )
    assert result["status"] == "INCOMPLETE_OR_FAILED"
    assert "scores.covdock_missing_not_zero" in result["failed_gates"]


def test_manifest_cannot_claim_output_outside_registry(tmp_path):
    delivery, receipt, browser = _fixture(tmp_path)
    manifest_path = (
        tmp_path / "state/jobs" / RUNS["evaluation"]
        / "artifacts/ligand_first_evaluation_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    output = next(iter(manifest["outputs"].values()))
    output["path"] = str(tmp_path.parent / "untrusted.parquet")
    manifest_path.write_text(json.dumps(manifest))
    result = audit_ligand_p0_closure(
        tmp_path, RUNS, delivery_table=delivery, platform_receipt=receipt,
        browser_verification=browser,
    )
    assert result["status"] == "INCOMPLETE_OR_FAILED"
    assert "evaluation.manifest_outputs" in result["failed_gates"]


def test_manifest_must_explicitly_be_completed(tmp_path):
    delivery, receipt, browser = _fixture(tmp_path)
    manifest_path = (
        tmp_path / "state/jobs" / RUNS["pareto"]
        / "artifacts/stage6_pareto_manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["status"] = "started"
    manifest_path.write_text(json.dumps(manifest))
    result = audit_ligand_p0_closure(
        tmp_path, RUNS, delivery_table=delivery, platform_receipt=receipt,
        browser_verification=browser,
    )
    assert result["status"] == "INCOMPLETE_OR_FAILED"
    assert "pareto.manifest_completed" in result["failed_gates"]


def test_workbench_must_show_progress_gate_reason_and_ranking_on_one_page(tmp_path):
    delivery, receipt, browser = _fixture(tmp_path)
    evidence = json.loads(browser.read_text())
    evidence["single_task_page"] = False
    evidence["stage_progress_visible"] = False
    evidence["gate_reasons_visible"] = False
    evidence["ranking_rationale_visible"] = False
    browser.write_text(json.dumps(evidence))
    result = audit_ligand_p0_closure(
        tmp_path, RUNS, delivery_table=delivery, platform_receipt=receipt,
        browser_verification=browser,
    )
    assert result["status"] == "INCOMPLETE_OR_FAILED"
    assert "workbench.browser_verified" in result["failed_gates"]
    detail = result["gates"]["workbench.browser_verified"]["detail"]
    assert detail["missing_required_evidence"] == [
        "single_task_page",
        "stage_progress_visible",
        "gate_reasons_visible",
        "ranking_rationale_visible",
    ]


def test_workbench_cannot_claim_visible_report_without_registered_report_artifacts(tmp_path):
    delivery, receipt, browser = _fixture(tmp_path)
    table = pd.read_csv(delivery)
    table = table.drop(columns=[
        "selection_report_artifact_id", "selection_report_artifact_version",
        "selection_report_artifact_sha256", "traceability_report_artifact_id",
        "traceability_report_artifact_version", "traceability_report_artifact_sha256",
    ])
    table.to_csv(delivery, index=False)
    result = audit_ligand_p0_closure(
        tmp_path, RUNS, delivery_table=delivery, platform_receipt=receipt,
        browser_verification=browser,
    )
    assert result["status"] == "INCOMPLETE_OR_FAILED"
    assert "workbench.report_references" in result["failed_gates"]
