from pathlib import Path
import sys

import pytest

from covscreen.native_approval import needs_approval
from covscreen.tool_api import (library_first_admit_md_candidate_tool,
                                library_first_select_md_candidate_tool)
from test_md_candidate_selection_tool import configured


def setup(tmp_path):
    arguments=configured(tmp_path); arguments["action"]="select"
    selected=library_first_select_md_candidate_tool(tmp_path,arguments)
    runtime=tmp_path/"runtime"; vendor=runtime/"vendor"; libraries=runtime/"lib"
    vendor.mkdir(parents=True); libraries.mkdir(); license_file=runtime/"license"
    license_file.write_text("test-only-license-placeholder")
    launcher=runtime/"run"
    launcher.write_text("#!/bin/sh\nprintf '%s\\n' '{\"runtime\":\"schrodinger-python\",\"module\":\"schrodinger\"}'\n")
    launcher.chmod(0o700)
    config=tmp_path/"configs/local.inspire.yaml"; config.parent.mkdir(exist_ok=True)
    config.write_text("adduct_parameterization:\n  enabled: true\n"
        f"  audit_python: {sys.executable}\n"
        f"  launcher: {launcher}\n  vendor_root: {vendor}\n  license_file: {license_file}\n"
        f"  runtime_lib_dir: {libraries}\nruntime:\n  worker_backend: local\n")
    return {"action":"plan","candidate_pose_artifact_id":selected["artifact_id"],
        "manifest_sha256":selected["manifest_sha256"],"native_timeout_seconds":1200,
        "resource_budget":{"max_runtime_minutes":30,"gpu_count":0,"cpu_count":2,
            "memory_gb":8,"max_candidates":1}}


def test_plan_freezes_candidate_pose_and_registered_native_runtime(tmp_path):
    result=library_first_admit_md_candidate_tool(tmp_path,setup(tmp_path))
    spec=result["spec"]
    assert result["status"]=="PLANNED" and result["native_graph_qc_completed"] is False
    assert spec["engine"]=="library-first-md-adduct-admission"
    assert spec["parameters"]["candidate_id"].startswith("library-")
    assert spec["parameters"]["runtime_registration"]["probe"]["module"]=="schrodinger"
    assert len(spec["input_sha256"])==5


def test_plan_rejects_tampered_candidate_pose_parent(tmp_path):
    arguments=setup(tmp_path)
    artifact=tmp_path/"state/candidate_pose_input"/arguments["candidate_pose_artifact_id"][21:]
    (artifact/"covdock-source.maegz").write_bytes(b"changed")
    with pytest.raises(ValueError,match="checksum mismatch"):
        library_first_admit_md_candidate_tool(tmp_path,arguments)


def test_start_requires_native_approval():
    name="covalent_library_first_admit_md_candidate"
    assert needs_approval(name,{"action":"start"}) is True
    assert needs_approval(name,{"action":"plan"}) is False


@pytest.mark.parametrize('route', ['library-first', 'ligand-first', 'de-novo'])
def test_shared_source_reaches_existing_admission_plan_with_explicit_origin(tmp_path, route):
    from covscreen.md_candidate_pose import freeze_delivered_candidate_pose
    from test_shared_md_pose import fixture
    from test_md_candidate_pose import sha
    arguments = setup(tmp_path)  # Synthetic runtime probe; no native graph computation.
    source = tmp_path/'shared'; source.mkdir()
    values = fixture(source, route)
    folder = tmp_path/'state/candidate_pose_input'/('e'*32)
    frozen = freeze_delivered_candidate_pose(*values, folder,
        expected_route=route, expected_task_id='campaign-001')
    arguments.update(candidate_pose_artifact_id='candidate-pose-input-'+'e'*32,
                     manifest_sha256=sha(folder/'candidate-pose-input.json'))
    result = library_first_admit_md_candidate_tool(tmp_path, arguments)
    assert result['status'] == 'PLANNED'
    assert result['calculation_submitted'] is False
    assert result['native_graph_qc_completed'] is False
    assert result['spec']['parameters']['discovery_origin'] == frozen['origin']
    assert result['spec']['provenance']['discovery_origin'] == frozen['origin']
    assert result['spec']['parameters']['candidate_id'].startswith(route+'-')
    # Evidence covers the copied source bundle, not just a display route label.
    paths = result['spec']['input_sha256']
    assert any('source-delivery/candidate_table.parquet' in path for path in paths)
