import hashlib
import json
from pathlib import Path

import pandas as pd
import pytest

from covscreen_library_first.md_candidate_delivery import freeze_md_candidate_delivery


def sha(path): return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inputs(tmp_path):
    pose=tmp_path/"poses.maegz"; pose.write_bytes(b"poses\n")
    af3=tmp_path/"model.cif"; af3.write_text("model\n")
    table=tmp_path/"candidates.parquet"
    pd.DataFrame([{"compound_id":"CMP-1","input_sdf_index":7,
        "standardized_smiles":"C=CC(=O)NC","target_id":"BTK","site_id":"A:CYS:481",
        "parent_id":"library","warhead_class":"acrylamide","electrophilic_atom":"C1",
        "nucleophilic_atom":"A:CYS:481:SG","af3_success":True,"af3_mpae":1.2,
        "model_cif":str(af3),"covdock_success":True,"covdock_score":-7.0,
        "covdock_pose_file":str(pose),"covdock_pose_index":3,"physical_validity":"PASS",
        "covdock_gate_detail":"A","cross_method_consensus":"HIGH"}]).to_parquet(table,index=False)
    manifest=tmp_path/"covdock.json"
    manifest.write_text(json.dumps({"status":"completed","gate_quality_gates":{"identity":True},
        "pose_files":[str(pose)],"attachment_residue":"A:481","nucleophilic_atom":"SG",
        "reaction_type":"Michael Addition","input_receptor_sha256":"a"*64}))
    return table,manifest


def test_freezes_candidate_assets_without_source_paths(tmp_path):
    table,manifest=inputs(tmp_path)
    result=freeze_md_candidate_delivery(table,sha(table),manifest,sha(manifest),
        "campaign-001",tmp_path/"out")
    assert result["candidate_count"]==1 and result["candidate_selected_for_md"] is False
    assert result["candidates"][0]["covdock_structure_pose"]=="3"
    assert all("path" not in item and "source" not in item for item in result["assets"].values())
    assert all((tmp_path/"out"/item["filename"]).is_file() for item in result["assets"].values())


def test_rejects_unregistered_covdock_asset(tmp_path):
    table,manifest=inputs(tmp_path); value=json.loads(manifest.read_text())
    value["pose_files"]=[str(tmp_path/"other.maegz")]; manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError,match="absent"):
        freeze_md_candidate_delivery(table,sha(table),manifest,sha(manifest),"campaign-001",tmp_path/"out")


def test_rejects_empty_covdock_quality_gates(tmp_path):
    table,manifest=inputs(tmp_path); value=json.loads(manifest.read_text())
    value["gate_quality_gates"]={}; manifest.write_text(json.dumps(value))
    with pytest.raises(ValueError,match="not eligible"):
        freeze_md_candidate_delivery(table,sha(table),manifest,sha(manifest),"campaign-001",tmp_path/"out")
