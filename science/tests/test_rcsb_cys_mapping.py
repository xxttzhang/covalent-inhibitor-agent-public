import importlib.util
from pathlib import Path

import pytest

path = Path(__file__).resolve().parents[1] / "scripts/map_rcsb_cysteine.py"
spec = importlib.util.spec_from_file_location("rcsb_cys_mapping", path)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixtures():
    entity = {"rcsb_polymer_entity_container_identifiers": {
        "entry_id": "1M17", "entity_id": "1", "uniprot_ids": ["P00533"]},
        "entity_poly": {"pdbx_seq_one_letter_code_can": "ACG"},
        "rcsb_polymer_entity_align": [{"reference_database_name": "UniProt",
            "reference_database_accession": "P00533", "aligned_regions": [
                {"ref_beg_seq_id": 796, "entity_beg_seq_id": 1, "length": 3}]}]}
    instance = {"rcsb_polymer_entity_instance_container_identifiers": {
        "entry_id": "1M17", "entity_id": "1", "asym_id": "A", "auth_asym_id": "X",
        "auth_to_entity_poly_seq_mapping": ["772", "773A", "774"]}}
    return entity, instance


def test_mapping_preserves_author_chain_and_insertion_code():
    result = module.map_cysteine(*fixtures(), "P00533", 797)
    assert result["entity_seq_id"] == 2
    assert result["auth_asym_id"] == "X"
    assert result["author_residue_id"] == "773A"
    assert result["status"].endswith("coordinates_pending")


@pytest.mark.parametrize("failure", ["wrong_entity", "not_cys", "gap", "ambiguous"])
def test_rejects_invalid_or_uncertain_mapping(failure):
    entity, instance = fixtures()
    if failure == "wrong_entity":
        instance["rcsb_polymer_entity_instance_container_identifiers"]["entity_id"] = "2"
    elif failure == "not_cys":
        entity["entity_poly"]["pdbx_seq_one_letter_code_can"] = "ASG"
    elif failure == "gap":
        entity["rcsb_polymer_entity_align"][0]["aligned_regions"] = []
    else:
        entity["rcsb_polymer_entity_align"][0]["aligned_regions"].append(
            {"ref_beg_seq_id": 797, "entity_beg_seq_id": 1, "length": 1})
    with pytest.raises(ValueError):
        module.map_cysteine(entity, instance, "P00533", 797)
