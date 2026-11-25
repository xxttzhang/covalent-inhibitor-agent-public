"""Report generation tests with explicit synthetic registry records, not real runs."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize("selection_ok,upstream_state,expected", [
    (True, "COMPLETED", "completed"),
    (False, "COMPLETED", "incomplete"),
    (True, "FAILED", "incomplete"),
])
def test_report_does_not_hide_failed_selection_or_parent(
    tmp_path, monkeypatch, selection_ok, upstream_state, expected
):
    source = (Path(__file__).resolve().parents[3] / "science") / "scripts/select_library_first_pareto.py"
    spec = importlib.util.spec_from_file_location("trace_report_probe", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    record = SimpleNamespace(route="ligand-first", engine="ligand-first-evaluation",
        state=upstream_state, return_code=0 if upstream_state == "COMPLETED" else 1,
        project_id="project", parent_run_id=None, submitted_at=None, finished_at=None,
        candidate_table_path=None)
    registry = SimpleNamespace(load_result=lambda _: record, job_dir=lambda _: tmp_path / "job")
    monkeypatch.setattr(module, "ArtifactRegistry", lambda *a, **k: registry)
    config = tmp_path / "config.yaml"
    config.write_text("synthetic: true\n")
    manifest = {"status": "completed" if selection_ok else "quality_gate_failed",
                "quality_gates": {"selection": selection_ok}, "quality": {"selected_count": 0}}
    json_path, _ = module._write_traceability_report(
        tmp_path, "parent", "current", config, tmp_path, manifest)
    payload = json.loads(json_path.read_text())
    assert payload["status"] == expected
    assert payload["stages"][0]["state"] == upstream_state
    assert payload["stages"][-1]["state"] == (
        "COMPLETED" if selection_ok else "QUALITY_GATE_FAILED")


def test_report_prefers_scientific_upstream_over_failed_retry_parent(tmp_path, monkeypatch):
    source = (Path(__file__).resolve().parents[3] / "science") / "scripts/select_library_first_pareto.py"
    spec = importlib.util.spec_from_file_location("trace_report_lineage", source)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def result(engine, parent=None, state="COMPLETED"):
        return SimpleNamespace(
            route="ligand-first", project_id="project", engine=engine, state=state,
            return_code=0 if state == "COMPLETED" else 1, parent_run_id=parent,
            submitted_at=None, finished_at=None, candidate_table_path=None,
            runtime_seconds=10.0,
        )

    results = {
        "evaluation": result("ligand-first-evaluation", "covdock"),
        "covdock": result("schrodinger-covdock-lo"),
        "af3-retry": result("alphafold3-mpae", "af3-failed"),
        "af3-failed": result("alphafold3-mpae", state="FAILED"),
        "carsidock": result("carsidock-cov"),
    }
    budget = {"cpu_count": 2, "gpu_count": 0, "memory_gb": 8}
    specs = {
        "evaluation": SimpleNamespace(parameters={}, resource_budget=budget),
        "covdock": SimpleNamespace(
            parameters={"upstream_run_id": "af3-retry"}, resource_budget=budget
        ),
        "af3-retry": SimpleNamespace(
            parameters={"upstream_run_id": "carsidock"}, resource_budget=budget
        ),
        "af3-failed": SimpleNamespace(
            parameters={"upstream_run_id": "carsidock"}, resource_budget=budget
        ),
        "carsidock": SimpleNamespace(parameters={}, resource_budget=budget),
    }
    registry = SimpleNamespace(
        load_result=lambda run_id: results[run_id],
        load_spec=lambda run_id: specs[run_id],
        job_dir=lambda run_id: tmp_path / run_id,
    )
    monkeypatch.setattr(module, "ArtifactRegistry", lambda *a, **k: registry)
    config = tmp_path / "config.yaml"
    config.write_text("synthetic: true\n")
    manifest = {
        "status": "completed",
        "quality_gates": {"selection": True},
        "quality": {"selected_count": 3},
    }
    json_path, _ = module._write_traceability_report(
        tmp_path, "evaluation", "pareto", config, tmp_path, manifest
    )
    payload = json.loads(json_path.read_text())
    assert [stage["run_id"] for stage in payload["stages"]] == [
        "carsidock", "af3-retry", "covdock", "evaluation", "pareto"
    ]
    assert "af3-failed" not in [stage["run_id"] for stage in payload["stages"]]
    af3 = payload["stages"][1]
    assert af3["parent_run_id"] == "af3-failed"
    assert af3["scientific_upstream_run_id"] == "carsidock"
    assert af3["lineage_edge"] == "spec.parameters.upstream_run_id"
    assert [item["run_id"] for item in af3["execution_attempts"]] == [
        "af3-failed", "af3-retry"
    ]
    evidence = payload["execution_evidence"]
    assert evidence["observed_attempt_count"] == 6
    assert evidence["runtime_observed_attempt_count"] == 5
    assert evidence["resource_observed_attempt_count"] == 5
    assert evidence["completed_attempt_count"] == 5
    assert evidence["failed_attempt_count"] == 1
    assert evidence["attempt_failure_rate"] == pytest.approx(1 / 6)
    assert evidence["total_observed_runtime_seconds"] == pytest.approx(50.0)
    assert evidence["requested_resource_consumption"]["cpu_hours"] == pytest.approx(
        100.0 / 3600.0
    )
    assert evidence["repeatability"] == "not_assessed_single_chain"
    assert evidence["monetary_cost"] is None
    assert evidence["monetary_cost_reason"] == (
        "not_available_without_an_approved_rate_card"
    )
