import hashlib
import json
import shutil
from pathlib import Path
import pytest
from covscreen.native_case import prepare_native_case
from covscreen.rcsb_mapping import map_cysteine
from covscreen.structure_site_audit import audit_structure_site
from covscreen.ligand_first_inputs import load_campaign, audit_ligand_first_inputs

ROOT = (Path(__file__).resolve().parents[3] / "science")


@pytest.fixture
def acquisition(tmp_path):
    folder = tmp_path / "acquired"
    folder.mkdir()
    shutil.copyfile(ROOT / "tests/fixtures/AQ4.cif", folder / "component.cif")
    shutil.copyfile(ROOT / "tests/fixtures/structures/1M17.pdb", folder / "structure.pdb")
    entry = {"rcsb_entry_container_identifiers": {"entry_id": "1M17"},
        "exptl": [{"method": "X-RAY DIFFRACTION"}], "rcsb_entry_info": {"resolution_combined": [2.6]}}
    entity = {"rcsb_polymer_entity_container_identifiers": {"entry_id": "1M17", "entity_id": "1", "uniprot_ids": ["P00533"]},
        "entity_poly": {"pdbx_seq_one_letter_code_can": "C", "rcsb_mutation_count": 0,
            "rcsb_conflict_count": 0, "rcsb_insertion_count": 0, "rcsb_deletion_count": 0},
        "rcsb_entity_source_organism": [{"ncbi_taxonomy_id": 9606, "ncbi_scientific_name": "Homo sapiens",
                                        "rcsb_gene_name": [{"value": "egfr"}]}],
        "rcsb_polymer_entity_align": [{"reference_database_name": "UniProt", "reference_database_accession": "P00533",
            "aligned_regions": [{"ref_beg_seq_id": 797, "entity_beg_seq_id": 1, "length": 1}]}]}
    chain = {"rcsb_polymer_entity_instance_container_identifiers": {"entry_id": "1M17", "entity_id": "1",
        "asym_id": "A", "auth_asym_id": "A", "auth_to_entity_poly_seq_mapping": ["773"]}}
    for name, value in (("entry.json", entry), ("entity_1.json", entity), ("chain_A.json", chain)):
        (folder / name).write_text(json.dumps(value))
    sources = [{"path": path.name, "url": "test:fixture", "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
               for path in folder.iterdir()]
    mapping = map_cysteine(entity, chain, "P00533", 797)
    result = {"status": "acquired_coordinate_audit_completed_review_pending", "sources": sources,
        "mappings": [mapping], "plan": {"component": "AQ4", "uniprot": "P00533", "cys": 797,
            "expected_inchikey": "AAKJLRGGTJKAMG-UHFFFAOYSA-N"},
        "coordinate_audit": audit_structure_site(folder / "structure.pdb", mapping, "AQ4")}
    (folder / "structure_acquisition.json").write_text(json.dumps(result))
    return folder


def prepare(source, out, **kwargs):
    return prepare_native_case(source, out, campaign_id="egfr-new-native", gene="EGFR", taxon=9606,
        ligand_name="erlotinib", noncovalent_parent_assertion="Caller identifies erlotinib as noncovalent parent", **kwargs)


def test_generated_native_case_passes_real_parent_audit(acquisition, tmp_path):
    out = tmp_path / "case"
    result = prepare(acquisition, out)
    campaign = load_campaign(out / "campaign.json")
    audit, mol = audit_ligand_first_inputs(campaign, out)
    assert audit.status == "PASS"
    assert mol.GetNumAtoms() == 29
    assert campaign.planning_mode == "upstream-native"
    assert result["runtime_registered"] is False
    assert result["noncovalent_parent_evidence"]["kind"] == "caller_assertion_not_independently_verified"


def test_modified_frozen_source_cannot_register(acquisition, tmp_path):
    (acquisition / "entry.json").write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        prepare(acquisition, tmp_path / "case")


def test_changed_derived_mapping_cannot_override_source(acquisition, tmp_path):
    path = acquisition / "structure_acquisition.json"
    payload = json.loads(path.read_text())
    payload["mappings"][0]["author_residue_id"] = "774"
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="differs from frozen"):
        prepare(acquisition, tmp_path / "case")


def test_accession_bound_primary_gene_prepares_real_parent(acquisition, tmp_path):
    from covscreen.ligand_parent_evidence import verified_target_gene
    evidence = {'target': 'P00533', 'taxon': 9606, 'identity': {
        'uniprot_id': 'P00533', 'gene': 'EGFR', 'requested_target': 'P00533',
        'taxon': 9606, 'canonical_cys_verified': True}}
    out = tmp_path / 'accession-case'
    result = prepare_native_case(acquisition, out, campaign_id='accession-case',
        gene=verified_target_gene(evidence), taxon=evidence['taxon'], ligand_name='erlotinib',
        noncovalent_parent_assertion='Synthetic software fixture, not scientific acceptance')
    campaign = load_campaign(out / 'campaign.json')
    audit, mol = audit_ligand_first_inputs(campaign, out)
    assert audit.status == 'PASS' and mol.GetNumAtoms() == 29
    assert result['runtime_registered'] is False
    assert evidence['target'] == 'P00533'
