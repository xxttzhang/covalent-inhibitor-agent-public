"""Real workflow/approval/registry contracts with explicitly synthetic science outputs."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from covscreen.execution import JobManager, execute_persisted_job
from covscreen.jobs import sha256_file
from covscreen.mcp.server import dispatch
from covscreen.library_workflow import RESEARCH
from covscreen.workflow_store import load
from test_workflow_md_execution import research, call, approval, register
from test_workflow_md_parameterization import admitted, parameterized


def ready(fixture, monkeypatch):
    admitted(fixture, monkeypatch)
    call(fixture, "plan_md_parameterization")
    started = dispatch(fixture[0], RESEARCH, approval(fixture, "start_md_parameterization"))
    output = parameterized(fixture, started["md_execution"]["run_id"])
    assert call(fixture, "md_parameterization_status")["md_execution"]["verification"] == "verified"
    workspace = Path(__file__).resolve().parents[2]
    with (fixture[0] / "configs/local.inspire.yaml").open("a") as config:
        config.write("\nopenmm_system_preparation:\n  enabled: true\n"
            f"  python: {workspace / '.tooling/md-interoperability-20261002/bin/python'}\n"
            f"  pythonpath: {workspace / '.tooling/md-interoperability-20261002'}\n"
            "  intermol_source: bundled\n")
    return output


def prepared(fixture, run, *, parent=None):
    manager = JobManager(fixture[0])
    spec = manager.registry.load_spec(run)
    parent = parent or load(fixture[1])["md_execution"]["plan"]["input"]
    folder = manager.registry.job_dir(run) / "artifacts/openmm-system-preparation"
    for directory in ("conversion", "forcefield-audit", "minimization"):
        (folder / directory).mkdir(parents=True)
    base = {"system_id": parent["candidate_id"], "atoms": 10, "simulation_started": False,
        "md_system_ready": False, "campaign_ready": False}
    parents = {"source_cms_sha256": parent["parameterized_cms_sha256"], "parameter_audit_sha256": parent["manifest_sha256"]}

    def files(directory, names):
        for name in names:
            (folder / directory / name).write_text("SOFTWARE FIXTURE, NOT SCIENTIFIC EVIDENCE: " + name)
        return {name: sha256_file(folder / directory / name) for name in names}

    def write(name, value):
        (folder / name).write_text(json.dumps(value))
        return sha256_file(folder / name)

    conversion = write("conversion/conversion.json", {**base, **parents,
        "schema": "openmm-conversion-artifact-v1", "status": "conversion_written_forcefield_audit_pending",
        "forcefield": "OPLS_2005", "water": "TIP3P", "simulation_state": "covalent_adduct",
        "artifacts": files("conversion", ["unique.cms", "title-normalization.json", "atomtype-elements.json", "system.top", "system.gro", "source-atom-arrays.npz"])})
    forcefield = write("forcefield-audit/forcefield-audit.json", {**base, **parents,
        "schema": "openmm-forcefield-audit-artifact-v1", "status": "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING",
        "conversion_manifest_sha256": conversion, "independent_forcefield_audit_completed": True,
        "system_xml_sha256": files("forcefield-audit", ["system.xml"])["system.xml"]})
    minimization = write("minimization/minimization.json", {**base,
        "schema": "openmm-minimization-artifact-v1", "status": "BOUNDED_MINIMIZATION_PASSED_SHORT_QC_PENDING",
        "parents": {"parameter_audit_sha256": parent["manifest_sha256"], "conversion_manifest_sha256": conversion,
            "forcefield_audit_manifest_sha256": forcefield}, "platform": "CPU", "convergence_claimed": False,
        "max_iterations": spec.parameters["max_minimization_iterations"],
        "initial_energy_kj_mol": 10, "final_energy_kj_mol": 2, "initial_force_rms_kj_mol_nm": 1,
        "final_force_rms_kj_mol_nm": 0.5, "maximum_atom_displacement_angstrom": 0.1, "covalent_distance_angstrom": 1.8,
        "short_qc_completed": False, "equilibration_completed": False,
        "artifacts": files("minimization", ["system.xml", "minimized-positions-nm.npy", "cpu-reference-forces.npy"])})
    write("openmm-preparation.json", {**base, "schema": "openmm-system-preparation-artifact-v1",
        "status": "OPENMM_SYSTEM_MINIMIZED_SHORT_QC_PENDING", "parents": parents,
        "stages": {"conversion_manifest_sha256": conversion, "forcefield_audit_manifest_sha256": forcefield,
            "minimization_manifest_sha256": minimization}, "independent_forcefield_audit_completed": True,
        "bounded_minimization_completed": True, "short_qc_completed": False, "equilibration_completed": False})
    result = manager.registry.load_result(run)
    result.state = "COMPLETED"
    result.return_code = 0
    result.artifacts = [manager.registry.register_file(run, file, "other") for file in folder.rglob("*") if file.is_file()]
    manager.registry.write_result(result)
    return folder


def start(fixture):
    call(fixture, "plan_md_system_preparation", max_minimization_iterations=25)
    return dispatch(fixture[0], RESEARCH, approval(fixture, "start_md_system_preparation"))["md_execution"]["run_id"]


def test_preparation_uses_existing_engine_parent_and_independent_approval(research, monkeypatch):
    ready(research, monkeypatch)
    parent = deepcopy(load(research[1])["md_execution"])
    plan = call(research, "plan_md_system_preparation", max_minimization_iterations=25)
    assert plan["workflow"]["stage"] == "MD_SYSTEM_PREPARATION_PLAN_READY"
    fields = plan["workflow"]["tools"][RESEARCH]["fields"]
    assert "max_minimization_iterations" in fields
    assert "native_timeout_seconds" not in fields
    assert "parameterization_run_id" not in fields
    record = load(research[1])["md_execution"]
    assert record["parent"] == parent
    assert record["plan"]["spec"]["parent_run_id"] == parent["run_id"]
    assert record["plan"]["spec"]["engine"] == "library-first-openmm-system-preparation"
    with pytest.raises(ValueError, match="receipt required"):
        call(research, "start_md_system_preparation", human_approved=True)
    run = dispatch(research[0], RESEARCH, approval(research, "start_md_system_preparation"))["md_execution"]["run_id"]
    prepared(research, run)
    result = call(research, "md_system_preparation_status")
    assert result["md_execution"]["verification"] == "verified"
    assert result["md_execution"]["system_preparation_completed"] is True
    assert result["md_execution"]["system_preparation"]["short_qc_completed"] is False
    assert result["md_execution"]["md_system_ready"] is False
    assert result["assessment"]["state"] == "not_evaluated"
    assert load(research[1])["md_execution"]["parent"] == parent


@pytest.mark.parametrize("changed", ["openmm-preparation.json", "conversion/system.top", "conversion/source-atom-arrays.npz",
    "forcefield-audit/system.xml", "minimization/system.xml", "minimization/minimized-positions-nm.npy",
    "minimization/cpu-reference-forces.npy", "parent"])
def test_preparation_rejects_changed_current_and_parent_outputs(research, monkeypatch, changed):
    upstream = ready(research, monkeypatch)
    run = start(research)
    output = prepared(research, run)
    assert call(research, "md_system_preparation_status")["md_execution"]["verification"] == "verified"
    witness = deepcopy(load(research[1])["md_execution"]["system_preparation"])
    target = upstream / "parameterized.cms" if changed == "parent" else output / changed
    target.write_text("changed software artifact")
    if changed == "parent":
        with pytest.raises(ValueError):
            call(research, "md_system_preparation_status")
    else:
        assert call(research, "md_system_preparation_status")["md_execution"]["system_preparation"] is None
    assert load(research[1])["md_execution"]["verification"] == "evidence_rejected"
    assert load(research[1])["md_execution"]["system_preparation"] == witness


@pytest.mark.parametrize("changed", ["status", "parent", "stages", "readiness", "inventory", "minimization"])
def test_registered_but_inconsistent_receipt_is_not_accepted(research, monkeypatch, changed):
    ready(research, monkeypatch)
    run = start(research)
    output = prepared(research, run)
    receipt = json.loads((output / "openmm-preparation.json").read_text())
    if changed == "status":
        receipt["status"] = "PASS"
    elif changed == "parent":
        receipt["parents"]["parameter_audit_sha256"] = "0" * 64
    elif changed == "stages":
        receipt["stages"].pop("conversion_manifest_sha256")
    elif changed == "readiness":
        receipt["md_system_ready"] = True
    else:
        name = "conversion/conversion.json" if changed == "inventory" else "minimization/minimization.json"
        document = json.loads((output / name).read_text())
        if changed == "inventory":
            document["artifacts"] = {"../escape": "0" * 64}
        else:
            document["final_energy_kj_mol"] = float("nan")
        (output / name).write_text(json.dumps(document))
        key = "conversion_manifest_sha256" if changed == "inventory" else "minimization_manifest_sha256"
        receipt["stages"][key] = sha256_file(output / name)
    (output / "openmm-preparation.json").write_text(json.dumps(receipt))
    manager = JobManager(research[0])
    result = manager.registry.load_result(run)
    result.artifacts = [manager.registry.register_file(run, file, "other") for file in output.rglob("*") if file.is_file()]
    manager.registry.write_result(result)
    value = call(research, "md_system_preparation_status")["md_execution"]
    assert value["verification"] == "evidence_rejected"
    assert value["system_preparation"] is None


def test_preparation_release_restores_parameterization(research, monkeypatch):
    ready(research, monkeypatch)
    parent = deepcopy(load(research[1])["md_execution"])
    call(research, "plan_md_system_preparation")
    result = call(research, "release_md_stage", reason="Reconsider the bounded minimization settings before any submission.")
    assert result["workflow"]["stage"] == "MD_PARAMETERIZATION_REVIEW"
    assert load(research[1])["md_execution"] == parent
    assert "plan_md_system_preparation" in result["workflow"]["tools"][RESEARCH]["actions"]


def test_preparation_recovers_lost_submission_then_cancels_original_job(research, monkeypatch):
    ready(research, monkeypatch)
    call(research, "plan_md_system_preparation")
    def interrupted(manager, request):
        register(manager, request)
        raise ConnectionError("Lost preparation receipt")
    monkeypatch.setattr(JobManager, "submit", interrupted)
    with pytest.raises(ConnectionError):
        dispatch(research[0], RESEARCH, approval(research, "start_md_system_preparation"))
    restored = call(research, "reconcile")
    assert restored["pending"] is None
    run = restored["md_execution"]["run_id"]
    result = dispatch(research[0], RESEARCH, approval(research, "cancel_md_stage"))
    assert result["md_execution"]["cancellation_requested"] is True
    assert result["md_execution"]["cancellation_confirmed"] is False
    execute_persisted_job(research[0], run)
    result = call(research, "md_system_preparation_status")
    assert result["md_execution"]["run_id"] == run
    assert result["md_execution"]["cancellation_confirmed"] is True


def test_preparation_rechecks_parent_before_planning(research, monkeypatch):
    output = ready(research, monkeypatch)
    (output / "input.mae").write_text("different native input")
    with pytest.raises(ValueError):
        call(research, "plan_md_system_preparation")
    assert load(research[1])["md_execution"]["verification"] == "evidence_rejected"
