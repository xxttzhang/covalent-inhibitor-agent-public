import hashlib
import json
from pathlib import Path
import shutil

import pytest

from covscreen.native_approval import needs_approval
from covscreen.tool_api import library_first_parameterize_adduct_tool
from covscreen_library_first.md_adduct_input import admit_covalent_adduct_input
from test_md_candidate_delivery_job import completed_job


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def project(tmp_path):
    runtime = tmp_path / "runtime"
    vendor, libraries = runtime / "vendor", runtime / "lib"
    vendor.mkdir(parents=True)
    libraries.mkdir()
    license_file = runtime / "license"
    license_file.write_text("test-only-license-placeholder")
    launcher = runtime / "run"
    launcher.write_text("#!/bin/sh\nprintf '%s\\n' '{\"runtime\":\"schrodinger-python\",\"module\":\"schrodinger\"}'\n")
    launcher.chmod(0o700)
    config = tmp_path / "configs/local.inspire.yaml"
    config.parent.mkdir()
    config.write_text("adduct_parameterization:\n  enabled: true\n"
        f"  audit_python: {Path(__file__).parents[2] / '.tooling/design-route-validation/bin/python'}\n"
        f"  launcher: {launcher}\n  vendor_root: {vendor}\n  license_file: {license_file}\n"
        f"  runtime_lib_dir: {libraries}\nruntime:\n  worker_backend: local\n")
    source = tmp_path / "source.mae"
    source.write_bytes(b"software-contract-adduct\n")
    source_sha = sha(source)
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps({"schema": "prepared-receptor-artifact-v1",
        "status": "prepared_isolated_protein_scope", "entry_id": "4Z3V", "uniprot": "Q06187",
        "canonical_cys": 481, "author_chain": "A", "author_residue": "481",
        "receptor_prepared": True, "campaign_ready": False}))
    prepared_sha = sha(prepared)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"schema": "receptor-site-context-artifact-v1",
        "status": "descriptive_site_context_completed_review_pending",
        "identity": {"entry_id": "4Z3V", "uniprot": "Q06187", "canonical_cys": 481,
            "author_chain": "A", "author_residue": "481"},
        "parents": {"prepared_receptor_sha256": prepared_sha}, "campaign_ready": False}))
    context_sha = sha(context)
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"schema": "candidate-md-handoff-v1", "candidate_id": "candidate_001",
        "compound_id": "CMP-1", "status": "structural_quality_passed", "engine": "CovDock-LO",
        "target": {"entry_id": "4Z3V", "uniprot": "Q06187", "canonical_cys": 481,
            "author_chain": "A", "author_residue": "481", "atom_name": "SG"},
        "chemistry": {"warhead_class": "acrylamide", "reaction_type": "Michael Addition",
            "covalent_mode": "Cys-S-C", "electrophile_atom_name": "C1", "product_state": "covalent_adduct"},
        "pose": {"format": "mae", "source_sha256": source_sha, "pose_index": 0},
        "parents": {"prepared_receptor_sha256": prepared_sha,
                    "receptor_context_sha256": context_sha}, "warnings": []}))
    candidate_sha = sha(candidate)
    qc = tmp_path / "qc.json"
    qc.write_text(json.dumps({"schema": "covalent-adduct-graph-qc-v1", "system_id": "candidate_001",
        "status": "LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION", "simulation_state": "covalent_adduct",
        "heavy_graph_preserved": True, "warnings": [], "loop_qc_input_sha256": source_sha,
        "covalent_atom_indices_1based": [1, 10], "ligand_heavy_atom_indices_1based": [1, 2, 3, 4, 5],
        "target_atom": {"chain": "A", "residue": "481", "atom_name": "SG", "atom_index_1based": 10},
        "ligand_residue": {"chain": "L", "residue": "1", "resname": "LIG", "electrophile_atom_name": "C1"},
        "candidate_evidence_sha256": candidate_sha, "prepared_receptor_sha256": prepared_sha,
        "receptor_context_sha256": context_sha, "independent_graph_audit": True}))
    identifier = "b" * 32
    output = tmp_path / "state/covalent_adduct_input" / identifier
    admit_covalent_adduct_input(source, source_sha, qc, sha(qc), candidate, candidate_sha,
        prepared, prepared_sha, context, context_sha, output)
    return identifier, output


def arguments(identifier, output):
    return {"action": "plan", "adduct_input_artifact_id": "covalent-adduct-input-" + identifier,
        "manifest_sha256": sha(output / "adduct-input.json"), "native_timeout_seconds": 1200}


def test_plan_freezes_admitted_artifact_and_registered_native_runtime(tmp_path):
    identifier, artifact = project(tmp_path)
    result = library_first_parameterize_adduct_tool(tmp_path, arguments(identifier, artifact))
    spec = result["spec"]
    assert result["status"] == "PLANNED"
    assert spec["engine"] == "library-first-adduct-parameterization"
    assert spec["route"] == "library-first"
    assert len(spec["input_sha256"]) == 6
    assert spec["parameters"]["system_id"] == "candidate_001"
    assert spec["parameters"]["reaction_chemistry"]["warhead_class"] == "acrylamide"
    assert spec["parameters"]["runtime_registration"]["probe"]["module"] == "schrodinger"
    assert result["parameterization_completed"] is False
    assert result["md_system_ready"] is False
    assert not Path(spec["expected_output_paths"][0]).exists()


def test_plan_rejects_gpu_or_multiple_candidates(tmp_path):
    identifier, artifact = project(tmp_path)
    values = arguments(identifier, artifact)
    values["resource_budget"] = {"max_runtime_minutes": 30, "gpu_count": 1,
        "cpu_count": 2, "memory_gb": 8, "max_candidates": 2}
    with pytest.raises(ValueError, match="CPU-only"):
        library_first_parameterize_adduct_tool(tmp_path, values)


def test_plan_replays_adduct_parent_chain(tmp_path):
    identifier, artifact = project(tmp_path)
    (artifact / "candidate-evidence.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum mismatch"):
        library_first_parameterize_adduct_tool(tmp_path, arguments(identifier, artifact))


def test_only_start_requires_native_approval():
    name = "covalent_library_first_parameterize_adduct"
    assert needs_approval(name, {"action": "start"}) is True
    assert needs_approval(name, {"action": "plan"}) is False
    assert needs_approval(name, {"action": "status"}) is False


def test_plan_prefers_completed_admission_run_without_state_artifact_id(tmp_path):
    _,artifact=project(tmp_path)
    run="admission-parent-000001"; folder=tmp_path/"state/jobs"/run/"artifacts/md-adduct-admission"
    completed_job(tmp_path,run,"library-first-md-adduct-admission",folder)
    admitted=folder/"adduct-input"; shutil.copytree(artifact,admitted)
    manifest=admitted/"adduct-input.json"
    (folder/"md-adduct-admission.json").write_text(json.dumps({
        "status":"ADDUCT_ADMITTED_PARAMETERIZATION_PENDING",
        "native_graph_qc_completed":True,"system_id":"candidate_001",
        "artifacts":{"adduct-input/adduct-input.json":sha(manifest)}})+"\n")
    values={"action":"plan","adduct_admission_run_id":run,
        "adduct_input_manifest_sha256":sha(manifest),"native_timeout_seconds":1200}
    result=library_first_parameterize_adduct_tool(tmp_path,values)
    assert result["spec"]["parent_run_id"]==run
    assert result["spec"]["parameters"]["system_id"]=="candidate_001"
