import hashlib
import json
import pytest
import covscreen.tool_api as api


@pytest.fixture
def evidence(tmp_path):
    i, d = tmp_path / "identity", tmp_path / "discovery"
    i.mkdir(); d.mkdir()
    sources = []
    for name in ("uniprot", "pubchem", "rcsb_ccd"):
        raw = b'{}'
        (i / (name + ".json")).write_bytes(raw)
        sources.append({"source": name, "sha256": hashlib.sha256(raw).hexdigest()})
    (i / "identity_resolution.json").write_text(json.dumps({
        "status": "identifiers_resolved_structure_validation_required", "evidence": sources,
        "structure_discovery_inputs": {"uniprot_id": "P00533", "component_id": "AQ4"},
        "plan": {"gene": "EGFR", "taxon": 9606, "cys": 797, "ligand_name": "erlotinib"},
        "compounds": [{"inchikey": "AAKJLRGGTJKAMG-UHFFFAOYSA-N"}]}))
    raw = json.dumps({"result_set": [{"identifier": "1M17"}]}).encode()
    (d / "rcsb_response.json").write_bytes(raw)
    (d / "structure_discovery.json").write_text(json.dumps({
        "query": api.build_query("P00533", "AQ4", 20),
        "raw_response_sha256": hashlib.sha256(raw).hexdigest()}))
    return tmp_path


def call(root, **kwargs):
    return api.ligand_first_prepare_inputs_tool(root, "identity", "discovery", "1M17",
                                               "Caller identifies a noncovalent parent", **kwargs)


def test_plan_has_no_network_or_state_writes(evidence, monkeypatch):
    monkeypatch.setattr(api, "acquire_structure", lambda *a, **k: pytest.fail("unexpected execution"))
    result = call(evidence)
    assert result["network_request_sent"] is False
    assert not (evidence / "state").exists()


def test_raw_discovery_tampering_is_rejected(evidence):
    (evidence / "discovery/rcsb_response.json").write_text("{}")
    with pytest.raises(ValueError, match="hash mismatch"):
        call(evidence)


def test_case_preparation_receives_saved_identities(evidence, monkeypatch):
    def acquire(entry, uniprot, cys, component, out, **kwargs):
        assert (entry, uniprot, cys, component) == ("1M17", "P00533", 797, "AQ4")
        assert kwargs["expected_inchikey"] == "AAKJLRGGTJKAMG-UHFFFAOYSA-N"
        return {"status": "acquired_coordinate_audit_completed_review_pending"}
    def prepare(source, out, **kwargs):
        assert kwargs["gene"] == "EGFR" and kwargs["taxon"] == 9606
        assert kwargs["ligand_name"] == "erlotinib"
        return {"status": "native_inputs_prepared_not_runtime_registered"}
    monkeypatch.setattr(api, "acquire_structure", acquire)
    monkeypatch.setattr(api, "prepare_native_case", prepare)
    result = call(evidence, action="prepare")
    assert result["runtime_registered"] is False
    assert result["calculation_submitted"] is False
