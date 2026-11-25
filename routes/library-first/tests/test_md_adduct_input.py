import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_adduct_input import (admit_covalent_adduct_input,
                                                       load_covalent_adduct_input)


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def inputs(tmp_path):
    source = tmp_path / "candidate.mae"
    source.write_bytes(b"opaque-native-adduct-test-bytes\n")
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
    candidate = tmp_path / "candidate.json"
    candidate.write_text(json.dumps({"schema": "candidate-md-handoff-v1",
        "candidate_id": "candidate_001", "compound_id": "CMP-1",
        "status": "structural_quality_passed", "engine": "CovDock-LO",
        "target": {"entry_id": "4Z3V", "uniprot": "Q06187", "canonical_cys": 481,
            "author_chain": "A", "author_residue": "481", "atom_name": "SG"},
        "chemistry": {"warhead_class": "acrylamide", "reaction_type": "Michael Addition",
            "covalent_mode": "Cys-S-C", "electrophile_atom_name": "C1",
            "product_state": "covalent_adduct"},
        "pose": {"format": "mae", "source_sha256": source_sha, "pose_index": 0},
        "parents": {"prepared_receptor_sha256": prepared_sha,
                    "receptor_context_sha256": context_sha}, "warnings": []}))
    candidate_sha = sha(candidate)
    qc = tmp_path / "qc.json"
    qc.write_text(json.dumps({"schema": "covalent-adduct-graph-qc-v1",
        "system_id": "candidate_001", "status": "LOOP_GRAPH_QC_PASSED_PENDING_RELAXATION",
        "simulation_state": "covalent_adduct", "heavy_graph_preserved": True, "warnings": [],
        "loop_qc_input_sha256": source_sha, "covalent_atom_indices_1based": [1, 10],
        "ligand_heavy_atom_indices_1based": [1, 2, 3, 4, 5],
        "target_atom": {"chain": "A", "residue": "481", "atom_name": "SG",
                        "atom_index_1based": 10},
        "ligand_residue": {"chain": "L", "residue": "1", "resname": "LIG",
                           "electrophile_atom_name": "C1"},
        "candidate_evidence_sha256": candidate_sha, "prepared_receptor_sha256": prepared_sha,
        "receptor_context_sha256": context_sha, "independent_graph_audit": True}))
    return [source, source_sha, qc, sha(qc), candidate, candidate_sha,
            prepared, prepared_sha, context, context_sha]


def test_admits_one_fully_bound_candidate_without_claiming_md_readiness(tmp_path):
    values = inputs(tmp_path)
    output = tmp_path / "output"
    result = admit_covalent_adduct_input(*values, output)
    assert result["status"] == "adduct_input_admitted_native_parameterization_pending"
    assert result["native_graph_revalidation_pending"] is True
    assert result["parameterization_completed"] is False
    assert result["md_system_ready"] is False
    assert result["campaign_ready"] is False
    manifest = json.loads((output / "adduct-input.json").read_text())
    assert all(sha(output / name) == digest for name, digest in manifest["artifacts"].items())


@pytest.mark.parametrize("position,replacement,match", [
    (1, "0" * 64, "source"), (3, "0" * 64, "QC"),
    (5, "0" * 64, "Candidate"), (7, "0" * 64, "Prepared"),
    (9, "0" * 64, "context"),
])
def test_rejects_any_changed_parent_hash(tmp_path, position, replacement, match):
    values = inputs(tmp_path)
    values[position] = replacement
    with pytest.raises(ValueError, match=match):
        admit_covalent_adduct_input(*values, tmp_path / "output")


def test_rejects_unsupported_reaction_before_output(tmp_path):
    values = inputs(tmp_path)
    candidate = values[4]
    record = json.loads(candidate.read_text())
    record["chemistry"]["warhead_class"] = "unknown"
    candidate.write_text(json.dumps(record))
    values[5] = sha(candidate)
    with pytest.raises(ValueError, match="reaction chemistry"):
        admit_covalent_adduct_input(*values, tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_existing_output_is_never_reused(tmp_path):
    values = inputs(tmp_path)
    with pytest.raises(ValueError, match="new directory"):
        admit_covalent_adduct_input(*values, tmp_path)


def test_registered_artifact_reloads_and_recomputes_every_parent(tmp_path):
    values = inputs(tmp_path)
    identifier = "a" * 32
    output = tmp_path / "state/covalent_adduct_input" / identifier
    admit_covalent_adduct_input(*values, output)
    loaded = load_covalent_adduct_input(tmp_path, "covalent-adduct-input-" + identifier,
                                       sha(output / "adduct-input.json"))
    assert loaded["manifest"]["candidate_id"] == "candidate_001"
    assert loaded["graph_qc"]["covalent_atom_indices_1based"] == [1, 10]
    assert len(loaded["evidence_files"]) == 6
    (output / "candidate-evidence.json").write_text("{}")
    with pytest.raises(ValueError, match="checksum mismatch"):
        load_covalent_adduct_input(tmp_path, "covalent-adduct-input-" + identifier,
                                   sha(output / "adduct-input.json"))
