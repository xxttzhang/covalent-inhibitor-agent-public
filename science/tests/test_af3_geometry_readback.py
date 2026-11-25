"""Real mmCIF/Parquet parser tests; tiny fixtures are NOT AF3 predictions."""
import hashlib
from pathlib import Path

import pandas as pd
import pytest
import yaml

from covscreen.af3_results import reactive_geometry
from covscreen.refinement_output import collect_refinement_outputs, verify_af3_geometry
from test_af3_result_validation import structure_file
import test_refinement_output as output_fixture


def connectivity(partner="C3", duplicate=False):
    headers = ["_struct_conn.conn_type_id"] + [f"_struct_conn.ptnr{number}_{field}"
        for number in (1, 2) for field in ("auth_asym_id", "auth_seq_id", "label_atom_id")]
    row = f"covale A 105 SG L 1 {partner}\n"
    return "\nloop_\n" + "\n".join(headers) + "\n" + row * (2 if duplicate else 1) + "#\n"


@pytest.mark.parametrize("link,expected,count", [("", False, 0), (connectivity(), True, 1),
    (connectivity("C2"), False, 1), (connectivity(duplicate=True), False, 2)])
def test_distance_does_not_invent_covalent_connectivity(tmp_path, link, expected, count):
    model, target = structure_file(tmp_path)
    value = reactive_geometry(model.read_text() + link, "NC(=O)C=C", yaml.safe_load(target.read_text()), warhead_class="acrylamide")
    assert value["reactive_distance_angstrom"] == pytest.approx(1.8)
    assert value["model_has_exact_covalent_link"] is expected
    assert value["model_target_covalent_link_count"] == count
    assert value["nonbonded_pair_count"] == 4
    assert value["minimum_nonbonded_heavy_atom_distance_angstrom"] > 4
    assert value["full_topology_and_stereochemistry_verified"] is False
    assert value["scientific_quality_verified"] is False


def test_malformed_connectivity_cannot_be_measured(tmp_path):
    model, target = structure_file(tmp_path)
    text = model.read_text() + "\n_struct_conn.conn_type_id covale\n"
    with pytest.raises(ValueError, match="malformed"):
        reactive_geometry(text, "NC(=O)C=C", yaml.safe_load(target.read_text()), warhead_class="acrylamide")


@pytest.mark.parametrize("field,value", [("pdbx_ptnr1_PDB_ins_code", "A"), ("ptnr2_label_alt_id", "B"), ("ptnr1_symmetry", "2_555")])
def test_connectivity_cannot_point_to_another_conformer_or_symmetry_mate(tmp_path, field, value):
    model, target = structure_file(tmp_path)
    text = model.read_text() + connectivity() + f"\n_struct_conn.{field} {value}\n"
    with pytest.raises(ValueError, match="unsupported"):
        reactive_geometry(text, "NC(=O)C=C", yaml.safe_load(target.read_text()), warhead_class="acrylamide")


@pytest.fixture
def dataset():
    fixture = output_fixture.RefinementOutputTest()
    fixture.setUp()
    try:
        fixture.fixture("af3")
        source = fixture.root / "frozen-candidates.parquet"
        pd.DataFrame([{"input_sdf_index": row["input_sdf_index"], "compound_id": row["compound_id"],
            "standardized_smiles": "NC(=O)C=C", "warhead_class": "acrylamide"} for row in fixture.rows]).to_parquet(source, index=False)
        fixture.contract["input_sha256"] = hashlib.sha256(source.read_bytes()).hexdigest()
        fixture.manifest["inputs"]["candidate_table"]["sha256"] = fixture.contract["input_sha256"]
        for row in fixture.rows:
            output = Path(row["model_cif"])
            model, target = structure_file(output.parent)
            contents = model.read_text() + connectivity()
            output.write_text(contents)
            geometry = reactive_geometry(contents, "NC(=O)C=C", yaml.safe_load(target.read_text()), warhead_class="acrylamide")
            row.update({key: geometry[key] for key in ("source_residue", "af3_sequence_residue", "nucleophilic_atom",
                "electrophilic_atom", "reactive_distance_angstrom")})
        fixture.persist()
        runtime = {"protein_chain": "A", "ligand_chain": "L", "cysteine_sequence_index": 105, "source_residue_number": 797}
        options = {"candidate_table": source, "target_config": target, "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(), "runtime": runtime}
        yield fixture, options
    finally:
        fixture.tearDown()


def check(fixture, options):
    evidence = collect_refinement_outputs(fixture.root, "af3", fixture.contract)
    return verify_af3_geometry(fixture.root, evidence, **options)


def test_geometry_readback_uses_frozen_input_and_retains_measurements_without_acceptance(dataset):
    fixture, options = dataset
    before = {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in fixture.root.rglob("*") if path.is_file()}
    value = check(fixture, options)
    assert value["scientific_quality_verified"] is False
    assert [row["input_sdf_index"] for row in value["candidates"]] == [1, 3]
    assert all(row["measurements"]["model_has_exact_covalent_link"] for row in value["candidates"])
    assert before == {path: (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest()) for path in fixture.root.rglob("*") if path.is_file()}


def test_csv_candidate_handoff_uses_the_same_hash_and_identity_guard(dataset):
    fixture, options = dataset
    source = options['candidate_table']
    table = pd.read_parquet(source)
    csv = source.with_suffix('.csv')
    table.to_csv(csv, index=False)
    options['candidate_table'] = csv
    checksum = hashlib.sha256(csv.read_bytes()).hexdigest()
    fixture.contract['input_sha256'] = checksum
    fixture.manifest['inputs']['candidate_table']['sha256'] = checksum
    fixture.persist()
    result = check(fixture, options)
    assert len(result['candidates']) == 2
    assert all(row['state'] == 'MEASURED' for row in result['candidates'])
    csv.write_bytes(csv.read_bytes() + b'\n')
    with pytest.raises(ValueError, match='checksum'):
        check(fixture, options)


@pytest.mark.parametrize("field,value", [("reactive_distance_angstrom", .1), ("reactive_distance_angstrom", float("nan")),
    ("source_residue", 773), ("electrophilic_atom", "C2"), ("af3_sequence_residue", 104)])
def test_rehashing_score_table_cannot_invent_geometry(dataset, field, value):
    fixture, options = dataset
    fixture.rows[0][field] = value
    fixture.persist()
    with pytest.raises(ValueError, match="score-table"):
        check(fixture, options)


def test_failed_output_stays_unmeasured_and_changed_input_or_target_cannot_pass(dataset):
    fixture, options = dataset
    fixture.rows[0].update(engine_status="FAILED", raw_score=None, failure_reason="test engine failure")
    fixture.persist()
    value = check(fixture, options)
    assert value["candidates"][0]["state"] == "NOT_EVALUATED"
    assert value["candidates"][0]["measurements"] is None
    with pytest.raises(ValueError, match="target differs"):
        check(fixture, {**options, "runtime": {**options["runtime"], "cysteine_sequence_index": 106}})
    options["candidate_table"].write_bytes(b"changed frozen input")
    with pytest.raises(ValueError, match="checksum"):
        check(fixture, options)


def test_missing_model_connection_is_evidence_not_engine_failure(dataset):
    fixture, options = dataset
    model = Path(fixture.rows[0]["model_cif"])
    model.write_text(model.read_text().replace(connectivity(), ""))
    value = check(fixture, options)
    assert value["candidates"][0]["state"] == "MEASURED"
    assert value["candidates"][0]["measurements"]["model_has_exact_covalent_link"] is False
