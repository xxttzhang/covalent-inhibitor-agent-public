import io
import json
from pathlib import Path
import pytest
import covscreen.structure_acquisition as module

ROOT = Path(__file__).resolve().parents[1]


def responses(two_chains=False):
    chains = ["A", "B"] if two_chains else ["A"]
    values = {
        "entry/1M17": {"rcsb_entry_container_identifiers": {"entry_id": "1M17", "polymer_entity_ids": ["1"]}},
        "polymer_entity/1M17/1": {
            "rcsb_polymer_entity_container_identifiers": {"entry_id": "1M17", "entity_id": "1",
                                                        "uniprot_ids": ["P00533"], "asym_ids": chains},
            "entity_poly": {"pdbx_seq_one_letter_code_can": "C"},
            "rcsb_polymer_entity_align": [{"reference_database_name": "UniProt",
                "reference_database_accession": "P00533", "aligned_regions": [
                    {"ref_beg_seq_id": 797, "entity_beg_seq_id": 1, "length": 1}]}]},
    }
    for chain in chains:
        values[f"polymer_entity_instance/1M17/{chain}"] = {
            "rcsb_polymer_entity_instance_container_identifiers": {
                "entry_id": "1M17", "entity_id": "1", "asym_id": chain, "auth_asym_id": chain,
                "auth_to_entity_poly_seq_mapping": ["773"]}}
    return values


@pytest.mark.parametrize("ambiguous", [False, True])
def test_fetch_mapping_and_real_coordinate_audit(tmp_path, monkeypatch, ambiguous):
    payloads = responses(ambiguous)
    calls = []
    def get(url, timeout):
        calls.append(url)
        if url.endswith(".pdb"):
            raw = (ROOT / "projects/egfr-c797/ligand_first/source_structures/1M17.pdb").read_bytes()
        else:
            raw = json.dumps(payloads[url.split("/core/")[1]]).encode()
        return io.BytesIO(raw)
    monkeypatch.setattr(module.urllib.request, "urlopen", get)
    out = tmp_path / "acquired"
    result = module.acquire_structure("1M17", "P00533", 797, "AQ4", out)
    assert len(calls) == (5 if ambiguous else 4)
    assert result["campaign_ready"] is False
    assert (out / "structure_acquisition.json").is_file()
    if ambiguous:
        assert result["status"] == "cys_mapping_missing_or_ambiguous"
        assert "coordinate_audit" not in result
    else:
        assert result["status"] == "acquired_coordinate_audit_completed_review_pending", result
        assert result["coordinate_audit"]["cys_author_residue"] == "773"
        assert result["coordinate_audit"]["ligand_heavy_atom_count"] == 29
        assert len(result["sources"]) == 4
    with pytest.raises(FileExistsError):
        module.acquire_structure("1M17", "P00533", 797, "AQ4", out)


def test_untrusted_identifiers_rejected_before_network(tmp_path):
    with pytest.raises(ValueError):
        module.acquire_structure("../x", "P00533", 797, "AQ4", tmp_path / "no")
    assert not (tmp_path / "no").exists()
