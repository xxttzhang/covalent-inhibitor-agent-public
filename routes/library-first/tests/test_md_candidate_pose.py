import csv
import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_candidate_pose import (freeze_candidate_pose,
                                                         freeze_delivered_candidate_pose,
                                                         load_candidate_pose_input)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inputs(tmp_path):
    source = tmp_path / "poses.maegz"
    source.write_bytes(b"opaque-covdock-structures\n")
    source_sha = sha(source)
    prepared = tmp_path / "prepared.json"
    prepared.write_text(json.dumps({"schema": "prepared-receptor-artifact-v1",
        "status": "prepared_isolated_protein_scope", "entry_id": "4Z3V",
        "uniprot": "Q06187", "canonical_cys": 481, "author_chain": "A",
        "author_residue": "481", "receptor_prepared": True, "campaign_ready": False}))
    prepared_sha = sha(prepared)
    context = tmp_path / "context.json"
    context.write_text(json.dumps({"schema": "receptor-site-context-artifact-v1",
        "status": "descriptive_site_context_completed_review_pending",
        "identity": {"entry_id": "4Z3V", "uniprot": "Q06187", "canonical_cys": 481,
            "author_chain": "A", "author_residue": "481"},
        "parents": {"prepared_receptor_sha256": prepared_sha}, "campaign_ready": False}))
    context_sha = sha(context)
    table = tmp_path / "candidate-results.csv"
    fields = ["route", "result_scope", "online_structural_status", "compound_id",
        "input_sdf_index", "standardized_smiles", "warhead_class",
        "covdock_structure_pose", "covdock_artifact_id", "covdock_artifact_version",
        "covdock_artifact_sha256", "covdock_target_context"]
    target = {"artifact_sha256": source_sha, "parameters": {"chain": "A",
        "residue": "CYS", "residue_number": 481, "reaction_type": "Michael Addition"}}
    with table.open("w", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerow({"route": "library-first", "result_scope": "candidate-results-v1",
            "online_structural_status": "BOTH_STRUCTURAL_QC_PASSED", "compound_id": "CMP-1",
            "input_sdf_index": "7", "standardized_smiles": "C=CC(=O)NC",
            "warhead_class": "acrylamide", "covdock_structure_pose": "12",
            "covdock_artifact_id": "art-1", "covdock_artifact_version": "ver-1",
            "covdock_artifact_sha256": source_sha,
            "covdock_target_context": json.dumps(target)})
    return [table, sha(table), source, source_sha, "CMP-1", 7, prepared, prepared_sha,
            context, context_sha]


def test_freezes_exact_pose_and_all_parents_without_claiming_qc(tmp_path):
    values = inputs(tmp_path)
    output = tmp_path / "output"
    result = freeze_candidate_pose(*values, output)
    assert result["pose_index"] == 12
    assert result["status"] == "pose_selected_native_graph_qc_pending"
    assert result["native_graph_qc_completed"] is False
    assert result["md_system_ready"] is False
    assert result["campaign_ready"] is False
    assert sha(output / "covdock-source.maegz") == values[3]


def test_rejects_candidate_that_did_not_pass_both_structural_gates(tmp_path):
    values = inputs(tmp_path)
    table = values[0]
    text = table.read_text().replace("BOTH_STRUCTURAL_QC_PASSED", "COVDOCK_ONLY")
    table.write_text(text)
    values[1] = sha(table)
    with pytest.raises(ValueError, match="structural-quality"):
        freeze_candidate_pose(*values, tmp_path / "output")


def test_rejects_source_hash_not_bound_by_candidate_row(tmp_path):
    values = inputs(tmp_path)
    table = values[0]
    table.write_text(table.read_text().replace(values[3], "0" * 64))
    values[1] = sha(table)
    with pytest.raises(ValueError, match="exact CovDock source"):
        freeze_candidate_pose(*values, tmp_path / "output")


def test_registered_artifact_replays_copied_inventory(tmp_path):
    values = inputs(tmp_path)
    identifier = "a" * 32
    output = tmp_path / "state/candidate_pose_input" / identifier
    freeze_candidate_pose(*values, output)
    loaded = load_candidate_pose_input(tmp_path, "candidate-pose-input-" + identifier,
                                       sha(output / "candidate-pose-input.json"))
    assert loaded["manifest"]["compound_id"] == "CMP-1"
    assert loaded["row"]["covdock_structure_pose"] == "12"
    (output / "covdock-source.maegz").write_bytes(b"changed")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_candidate_pose_input(tmp_path, "candidate-pose-input-" + identifier,
                                  sha(output / "candidate-pose-input.json"))


def delivered_inputs(tmp_path):
    old=inputs(tmp_path)
    source,prepared,context=old[2],old[6],old[8]
    delivery_dir=tmp_path/"delivery"; (delivery_dir/"assets").mkdir(parents=True)
    frozen=delivery_dir/"assets/covdock_7.maegz"
    frozen.write_bytes(source.read_bytes())
    delivery=delivery_dir/"candidate-results.json"
    delivery.write_text(json.dumps({"schema":"library-first-md-candidate-delivery-v1",
        "status":"candidate_delivery_frozen_md_selection_pending","route":"library-first",
        "task_id":"delivery-run-000001","candidate_selected_for_md":False,
        "native_graph_qc_completed":False,
        "target_context":{"attachment_residue":"A:481","nucleophilic_atom":"SG",
            "reaction_type":"Michael Addition"},
        "assets":{"covdock_7":{"filename":"assets/covdock_7.maegz",
            "sha256":sha(frozen),"size":frozen.stat().st_size}},
        "candidates":[{"compound_id":"CMP-1","input_sdf_index":"7",
            "standardized_smiles":"C=CC(=O)NC","warhead_class":"acrylamide",
            "nucleophilic_atom":"A:CYS:481:SG","af3_status":"SUCCESS",
            "covdock_status":"SUCCESS","physical_validity":"PASS",
            "covdock_asset":"covdock_7","covdock_structure_pose":"12"}]}))
    return delivery,sha(delivery),"CMP-1",7,prepared,sha(prepared),context,sha(context)


def test_selects_from_frozen_delivery_without_source_path_arguments(tmp_path):
    values=delivered_inputs(tmp_path); identifier="b"*32
    output=tmp_path/"state/candidate_pose_input"/identifier
    result=freeze_delivered_candidate_pose(*values,output)
    assert result["schema"]=="candidate-pose-input-artifact-v2"
    assert result["delivered_artifact"]["asset_key"]=="covdock_7"
    loaded=load_candidate_pose_input(tmp_path,"candidate-pose-input-"+identifier,
        sha(output/"candidate-pose-input.json"))
    assert loaded["manifest"]["candidate_id"]==result["candidate_id"]


def test_delivery_selection_rejects_receptor_target_mismatch(tmp_path):
    values=delivered_inputs(tmp_path)
    payload=json.loads(values[0].read_text())
    payload["target_context"]["attachment_residue"]="B:481"
    values[0].write_text(json.dumps(payload)); values=list(values); values[1]=sha(values[0])
    with pytest.raises(ValueError,match="differs from receptor"):
        freeze_delivered_candidate_pose(*values,tmp_path/"out")
