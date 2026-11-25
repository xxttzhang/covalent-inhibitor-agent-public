import pytest
from covscreen.ligand_identity import identity_plan, target_candidates, compound_candidates, ccd_query


def protein(sequence="AAC"):
    return {"results": [{"primaryAccession": "P00533", "genes": [{"geneName": {"value": "EGFR"}}],
        "organism": {"taxonId": 9606}, "entryType": "UniProtKB reviewed (Swiss-Prot)",
        "sequence": {"value": sequence}}]}


def test_explicit_taxon_cys_and_complete_identity_required():
    plan = identity_plan("EGFR", 9606, 3, "erlotinib")
    assert target_candidates(protein(), plan, 1)["unique_verified_target"] == "P00533"
    for count in (None, 2):
        assert target_candidates(protein(), plan, count)["unique_verified_target"] is None
    assert target_candidates(protein("AAA"), plan, 1)["unique_verified_target"] is None
    assert target_candidates(protein(), {**plan, "taxon": 10090}, 1)["unique_verified_target"] is None


@pytest.mark.parametrize("gene,taxon,cys,ligand", [
    ("EGFR OR *", 9606, 797, "erlotinib"), ("EGFR", True, 797, "erlotinib"),
    ("EGFR", 9606, 0, "erlotinib"), ("EGFR", 9606, 797, "x\n")])
def test_invalid_names_rejected(gene, taxon, cys, ligand):
    with pytest.raises(ValueError):
        identity_plan(gene, taxon, cys, ligand)


def test_full_inchikey_not_connectivity_only():
    key = "AAAAAAAAAAAAAA-BBBBBBBBBB-C"
    assert ccd_query(key)["query"]["parameters"]["value"] == key
    assert ccd_query(key)["return_type"] == "mol_definition"
    with pytest.raises(ValueError):
        ccd_query(key.split("-")[0])
    with pytest.raises(ValueError):
        compound_candidates({"PropertyTable": {"Properties": [{"CID": 1, "InChIKey": "bad"}]}})


@pytest.mark.parametrize("resolved", [True, False])
def test_mcp_handoff_only_when_resolver_provides_verified_ids(tmp_path, monkeypatch, resolved):
    import covscreen.tool_api as api
    result = {"status": "ambiguous_or_unmatched_identity"}
    if resolved:
        result = {"status": "identifiers_resolved_structure_validation_required",
                  "structure_discovery_inputs": {"uniprot_id": "P00533", "component_id": "AQ4"}}
    monkeypatch.setattr(api, "resolve_names", lambda *args: result)
    response = api.ligand_first_resolve_names_tool(
        tmp_path, "EGFR", 9606, 797, "erlotinib", action="search")
    assert response["calculation_submitted"] is False
    assert ("next_tool" in response) == resolved
    if resolved:
        assert response["next_tool"]["arguments"] == {
            "uniprot": "P00533", "component": "AQ4", "action": "plan"}
