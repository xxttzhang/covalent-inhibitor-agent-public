"""Real Parquet/JSON measurement provenance; not an AF3 or licensed SDK run."""
import copy
import hashlib
from pathlib import Path

import pytest
import test_refinement_output as fixtures
from covscreen.refinement_output import collect_refinement_outputs, verify_af3_confidence
from covscreen.refinement_quality import POLICY, read_measurements, validate_policy


@pytest.fixture
def source():
    fixture = fixtures.RefinementOutputTest()
    fixture.setUp()
    yield fixture
    fixture.tearDown()


def test_policy_is_exact_and_cannot_weaken_thresholds_or_invent_affinity_claims():
    assert len(validate_policy(POLICY)) == 64
    for changed in (None, {**POLICY, "covdock_max_strain_kcal_mol": 99},
                    {**POLICY, "af3_confidence": "validated_affinity"}, {**POLICY, "extra": True},
                    {**POLICY, "min_nonbonded_distance_angstrom": float("nan")}):
        with pytest.raises(ValueError):
            validate_policy(changed)


def test_af3_measurements_preserve_real_confidence_values_and_witness_paths_without_writes(source):
    source.confidence_fixture()
    evidence = collect_refinement_outputs(source.root, "af3", source.contract)
    verify_af3_confidence(source.root, evidence, protein_chain="B", ligand_chain="L")
    before = {p: (p.stat().st_mtime_ns, fixtures.sha(p)) for p in source.root.rglob("*") if p.is_file()}
    value = read_measurements(source.root, evidence, POLICY)
    assert value["policy_digest"] == validate_policy(POLICY)
    for actual, expected in zip(value["candidates"], source.rows):
        assert actual["input_sdf_index"] == expected["input_sdf_index"]
        assert actual["metrics"]["mpae"] == expected["mpae"]
        assert actual["metrics"]["iptm"] == expected["iptm"]
        assert type(actual["metrics"]["has_clash"]) is bool
        assert len(actual["evidence"]) == 5
        assert all(name in evidence["artifacts"] for name in actual["evidence"])
    assert before == {p: (p.stat().st_mtime_ns, fixtures.sha(p)) for p in source.root.rglob("*") if p.is_file()}


def test_covdock_missing_measurements_remain_null_not_passes(source):
    source.fixture("covdock")
    evidence = collect_refinement_outputs(source.root, "covdock", source.contract)
    value = read_measurements(source.root, evidence, POLICY)
    for row in value["candidates"]:
        assert row["metrics"]["strain_kcal_mol"] is None
        assert row["metrics"]["covalent_link_present"] is None
        assert row["metrics"]["severe_clash"] is None
        assert row["metrics"]["engine"] == "covdock"
        assert "audit.json" in row["evidence"]
        assert "status" not in row


@pytest.mark.parametrize("engine", ["af3", "covdock"])
def test_failed_engine_row_retained_and_changed_identity_or_files_rejected(source, engine):
    source.confidence_fixture() if engine == "af3" else source.fixture(engine)
    source.rows[1].update(engine_status="FAILED", raw_score=None, failure_reason="test fixture engine error")
    source.persist()
    evidence = collect_refinement_outputs(source.root, engine, source.contract)
    result = read_measurements(source.root, evidence, POLICY)
    assert result["candidates"][1] == {"input_sdf_index": 3, "compound_id": "fixture-three", "metrics": None, "evidence": []}
    changed = copy.deepcopy(evidence)
    changed["candidates"][0]["engine_status"] = "FAILED"
    with pytest.raises(ValueError, match="identity or status"):
        read_measurements(source.root, changed, POLICY)
    source.rows[0]["compound_id"] = "other"
    source.persist()
    with pytest.raises(ValueError, match="checksum differs"):
        read_measurements(source.root, evidence, POLICY)


@pytest.mark.parametrize("local,status,value", [(None, "MISSING", None), (float("inf"), "INVALID_OR_NONFINITE", None),
    ("invalid", "INVALID_OR_NONFINITE", None), (0.0, "VALID", 0.0), (12.0, "VALID", 12.0)])
def test_provisional_policy_uses_only_exact_local_strain_with_provenance(source, local, status, value):
    import json
    from covscreen.refinement_quality import STRICT_POLICY
    source.fixture("covdock")
    audit = Path(source.rows[0]["covdock_inspection_file"])
    data = json.loads(audit.read_text())
    for row, inspected in zip(source.rows, data["rows"]):
        row["r_strain_Local_Strain_Energy"] = local
        row["r_strain_Strain_Energy"] = 3.0
        inspected["r_strain_Local_Strain_Energy"] = local
        inspected["r_strain_Strain_Energy"] = 3.0
    audit.write_text(json.dumps(data))
    for row in source.rows:
        row["covdock_inspection_sha256"] = fixtures.sha(audit)
    source.persist()
    evidence = collect_refinement_outputs(source.root, "covdock", source.contract)
    result = read_measurements(source.root, evidence, STRICT_POLICY)
    for row in result["candidates"]:
        assert row["metrics"]["strain_kcal_mol"] == value
        assert row["metrics"]["strain_provenance"] == {
            "source_field": "r_strain_Local_Strain_Energy", "unit": "kcal/mol",
            "definition": "vendor_local_strain_unmodified", "status": status}
    if local is None:
        assert read_measurements(source.root, evidence, POLICY)["candidates"][0]["metrics"]["strain_kcal_mol"] == 3.0
    for changed in ({**STRICT_POLICY, "strain_source_field": "r_strain_Strain_Energy"},
                    {**STRICT_POLICY, "calibration_status": "calibrated"}):
        with pytest.raises(ValueError):
            read_measurements(source.root, evidence, changed)

def test_unwitnessed_source_cannot_be_added_to_quality_evidence(source):
    source.confidence_fixture()
    evidence = collect_refinement_outputs(source.root, "af3", source.contract)
    missing = next(name for name in evidence["artifacts"] if name.endswith("_confidences.json"))
    evidence["artifacts"].pop(missing)
    with pytest.raises(ValueError, match="absent from retained"):
        read_measurements(source.root, evidence, POLICY)
