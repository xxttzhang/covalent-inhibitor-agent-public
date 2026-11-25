from covscreen.af3_identity import (
    af3_job_name,
    merge_source_metadata,
    validate_component_id,
)


def test_af3_job_name_is_safe_bounded_and_identity_stable():
    name = af3_job_name("EGFR", "CovInDB/very long id with spaces:" + "x" * 100, "0/1", 42)
    assert len(name) <= 128
    assert name.endswith("__i42_covalent")
    assert "/" not in name
    assert " " not in name


def test_merge_source_metadata_preserves_pareto_evidence_and_af3_identity():
    manifest = {
        "job_name": "safe_job",
        "component_id": "LIG",
        "compound_id": "old",
        "input_sdf_index": -1,
    }
    row = {
        "compound_id": "CID-7",
        "protomer_id": "0",
        "input_sdf_index": 7,
        "property_fit_score": 0.91,
        "pains_alert": "",
        "high_reactivity_alert": False,
        "aggregation_risk": False,
        "scaffold_smiles": "c1ccccc1",
        "job_name": "must_not_overwrite",
    }
    merged = merge_source_metadata(manifest, row)
    assert merged["job_name"] == "safe_job"
    assert merged["component_id"] == "LIG"
    assert merged["compound_id"] == "CID-7"
    assert merged["input_sdf_index"] == 7
    assert merged["property_fit_score"] == 0.91
    assert merged["high_reactivity_alert"] is False


def test_component_id_is_short_ccd_code():
    assert validate_component_id("LIG") == "LIG"
    try:
        validate_component_id("LIG-CovInDB-123")
    except ValueError as exc:
        assert "1-5" in str(exc)
    else:
        raise AssertionError("long component identifier was accepted")


def test_source_table_cannot_overwrite_computed_target_binding():
    manifest = {'af3_target_seed_check': {'study_target_identity_verified': False},
                'protein_chain': 'B', 'cysteine_sequence_index': 2,
                'electrophilic_atom': 'C4'}
    row = {'compound_id': '007', 'protomer_id': '0', 'input_sdf_index': 0,
           'protein_chain': 'A', 'cysteine_sequence_index': 797,
           'electrophilic_atom': 'C99',
           'af3_target_seed_check': {'study_target_identity_verified': True}}
    merged = merge_source_metadata(manifest, row)
    assert all(merged[key] == value for key, value in manifest.items())
    assert merged['compound_id'] == '007'
