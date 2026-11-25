"""Real Parquet and filesystem fixtures, not AF3/CovDock scientific evidence."""
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

import pandas as pd

from covscreen.refinement_output import (MANIFEST, SPECS, canonical, collect_refinement_outputs,
    freeze_refinement_outputs, read_refinement_outputs, verify_af3_confidence)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RefinementOutputTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="refinement-output-test-")
        self.root = Path(self.temp.name).resolve()
        identities = [[1, "fixture-one"], [3, "fixture-three"]]
        self.contract = {"input_count": 2, "input_sha256": "a" * 64,
            "identity_sha256": hashlib.sha256(canonical(identities)).hexdigest(),
            "verified": True, "grants_resource_approval": False}

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self, engine):
        rows = [{"input_sdf_index": index, "compound_id": compound, "engine_status": "SUCCESS",
                 "raw_score": float(index), "failure_reason": None, "score_direction": "lower_is_better"}
                for index, compound in ((1, "fixture-one"), (3, "fixture-three"))]
        if engine == "af3":
            for row in rows:
                name = "fixture_" + str(row["input_sdf_index"])
                directory = self.root / "predictions" / name
                directory.mkdir(parents=True)
                model = directory / (name + "_model.cif")
                model.write_text("NON_SCIENTIFIC_STRUCTURE_FIXTURE\n")
                for suffix in ("_summary_confidences.json", "_confidences.json"):
                    (directory / (name + suffix)).write_text("{}")
                row.update({"model_cif": str(model), "job_name": name})
            self.manifest = {"inputs": {"refinement_input_contract": self.contract,
                "candidate_table": {"sha256": self.contract["input_sha256"]}},
                "execution": {"resume_enabled": False, "reparse_only": False}}
        else:
            pose = self.root / "combined.maegz"
            pose.write_bytes(b"NON_SCIENTIFIC_MULTIPOSE_FIXTURE")
            audit = self.root / "audit.json"
            poses = [{"pose_index": position, "title": row["compound_id"] + "__i" + str(row["input_sdf_index"]),
                      "r_test_score": row["raw_score"]} for position, row in enumerate(rows)]
            audit.write_text(json.dumps({"input": str(pose), "rows": poses}))
            for position, row in enumerate(rows):
                row.update({"covdock_pose_file": str(pose), "covdock_pose_index": position,
                    "covdock_title": poses[position]["title"], "covdock_inspection_file": str(audit),
                    "covdock_inspection_sha256": sha(audit)})
            self.manifest = {"refinement_input_contract": self.contract, "input_score_source": "core",
                             "input_candidate_table_sha256": self.contract["input_sha256"],
                             "resume": {"requested": False, "ligprep_reused": False,
                                        "covdock_reused": False, "source_artifact_dir": None},
                             "score_property": "r_test_score"}
        self.rows, self.engine = rows, engine
        self.persist()

    def persist(self):
        manifest_name, table_name = SPECS[self.engine]
        table = self.root / table_name
        pd.DataFrame(self.rows).to_parquet(table, index=False)
        self.manifest["outputs"] = {table_name: {"path": str(table), "sha256": sha(table)}}
        (self.root / manifest_name).write_text(json.dumps(self.manifest))

    def read(self, evidence):
        return read_refinement_outputs(self.root, self.engine, self.contract, evidence["sha256"])

    def test_af3_snapshot_roundtrip_does_not_claim_scientific_acceptance(self):
        self.fixture("af3")
        evidence = freeze_refinement_outputs(self.root, self.engine, self.contract)
        result = self.read(evidence)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertFalse(result["scientific_quality_verified"])
        self.assertFalse(result["resource_approval_granted"])
        self.assertEqual(len(result["artifacts"]), 8)
        self.assertEqual(result["candidates"][0]["structure"]["pose_index"], None)
        with self.assertRaises(FileExistsError):
            freeze_refinement_outputs(self.root, self.engine, self.contract)

    def test_covdock_shared_file_has_distinct_zero_based_entry_references(self):
        self.fixture("covdock")
        result = self.read(freeze_refinement_outputs(self.root, self.engine, self.contract))
        refs = [row["structure"] for row in result["candidates"]]
        self.assertEqual(refs, [{"path": "combined.maegz", "pose_index": 0}, {"path": "combined.maegz", "pose_index": 1}])

    def test_refresh_rehashes_structure_and_requires_independent_manifest_digest(self):
        self.fixture("af3")
        evidence = freeze_refinement_outputs(self.root, self.engine, self.contract)
        with self.assertRaisesRegex(ValueError, "checksum"):
            read_refinement_outputs(self.root, self.engine, self.contract, "b" * 64)
        Path(self.rows[0]["model_cif"]).write_text("ALTERED_FIXTURE")
        with self.assertRaisesRegex(ValueError, "changed after"):
            self.read(evidence)

    def test_table_or_snapshot_rewrite_is_not_trusted(self):
        self.fixture("af3")
        evidence = freeze_refinement_outputs(self.root, self.engine, self.contract)
        self.rows[0]["raw_score"] = 19.0
        self.persist()  # Rewrites engine hashes too; independently saved witness still rejects it.
        with self.assertRaisesRegex(ValueError, "changed after"):
            self.read(evidence)
        (self.root / MANIFEST).write_text("{}")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.read(evidence)

    def test_candidate_substitution_and_denominator_loss_fail(self):
        self.fixture("af3")
        self.rows[0]["compound_id"] = "substituted"
        self.persist()
        with self.assertRaisesRegex(ValueError, "identities"):
            collect_refinement_outputs(self.root, self.engine, self.contract)
        self.rows.pop()
        self.persist()
        with self.assertRaisesRegex(ValueError, "denominator"):
            collect_refinement_outputs(self.root, self.engine, self.contract)

    def test_failure_rows_remain_explicit_without_fake_scores_or_structures(self):
        self.fixture("af3")
        self.rows[1].update({"engine_status": "FAILED", "raw_score": None, "failure_reason": "fixture_engine_error"})
        self.persist()
        result = collect_refinement_outputs(self.root, self.engine, self.contract)
        self.assertEqual(result["candidates"][1]["failure_reason"], "fixture_engine_error")
        self.assertIsNone(result["candidates"][1]["structure"])
        self.rows[1]["failure_reason"] = None
        self.persist()
        with self.assertRaisesRegex(ValueError, "lacks a reason"):
            collect_refinement_outputs(self.root, self.engine, self.contract)

    def test_nonfinite_success_score_fails(self):
        self.fixture("af3")
        for score in (float("inf"), float("nan"), -1.0):
            self.rows[0]["raw_score"] = score
            self.persist()
            with self.assertRaisesRegex(ValueError, "invalid score"):
                collect_refinement_outputs(self.root, self.engine, self.contract)

    def test_structure_symlink_and_outside_path_fail(self):
        self.fixture("af3")
        original = Path(self.rows[0]["model_cif"])
        link = original.with_name("link.cif")
        link.symlink_to(original)
        self.rows[0]["model_cif"] = str(link)
        self.persist()
        with self.assertRaisesRegex(ValueError, "symbolic links"):
            collect_refinement_outputs(self.root, self.engine, self.contract)
        self.rows[0]["model_cif"] = "/etc/passwd"
        self.persist()
        with self.assertRaises(ValueError):
            collect_refinement_outputs(self.root, self.engine, self.contract)

    def test_covdock_audit_hash_and_pose_identity_and_score_are_checked(self):
        self.fixture("covdock")
        for field, value in (("covdock_inspection_sha256", None), ("covdock_pose_index", 20),
                             ("covdock_pose_index", 0.5), ("raw_score", -99.0),
                             ("covdock_title", "wrong-title")):
            original = self.rows[0][field]
            self.rows[0][field] = value
            self.persist()
            with self.assertRaises(ValueError):
                collect_refinement_outputs(self.root, self.engine, self.contract)
            self.rows[0][field] = original

    def test_changed_input_contract_cannot_be_collected(self):
        self.fixture("af3")
        with self.assertRaisesRegex(ValueError, "differs from frozen"):
            collect_refinement_outputs(self.root, self.engine, {**self.contract, "input_sha256": "b" * 64})

    def test_covdock_physical_measurements_must_match_the_actual_retained_audit(self):
        self.fixture("covdock")
        # Rewriting the table's ordinary manifest hash cannot invent geometry.
        self.rows[0]["covalent_bond_length_angstrom"] = 1.82
        self.persist()
        with self.assertRaisesRegex(ValueError, "physical measurement differs"):
            collect_refinement_outputs(self.root, self.engine, self.contract)
        audit = Path(self.rows[0]["covdock_inspection_file"])
        value = json.loads(audit.read_text())
        value["rows"][0]["covalent_bond_length_angstrom"] = 1.82
        value["rows"][0]["covalent_link_present"] = False
        audit.write_text(json.dumps(value))
        for row in self.rows:
            row["covdock_inspection_sha256"] = sha(audit)
        self.rows[0]["covalent_link_present"] = False
        self.persist()
        collect_refinement_outputs(self.root, self.engine, self.contract)
        self.rows[0]["covalent_link_present"] = True
        self.persist()
        with self.assertRaisesRegex(ValueError, "physical measurement differs"):
            collect_refinement_outputs(self.root, self.engine, self.contract)

    def test_changed_input_during_execution_and_historical_reuse_are_rejected(self):
        self.fixture("af3")
        self.manifest["inputs"]["candidate_table"]["sha256"] = "b" * 64
        self.persist()
        with self.assertRaisesRegex(ValueError, "final input checksum"):
            collect_refinement_outputs(self.root, self.engine, self.contract)
        self.manifest["inputs"]["candidate_table"]["sha256"] = self.contract["input_sha256"]
        self.manifest["execution"]["reparse_only"] = True
        self.persist()
        with self.assertRaisesRegex(ValueError, "historical"):
            collect_refinement_outputs(self.root, self.engine, self.contract)

    def test_actual_af3_selection_output_inventory_is_compatible(self):
        from covscreen.af3_selection import select_af3_candidates
        self.fixture("af3")
        actual = select_af3_candidates(pd.DataFrame(self.rows).assign(standardized_smiles="CC"),
                                      self.root, top_n=2, min_coverage=1)
        actual.update({key: self.manifest[key] for key in ("inputs", "execution")})
        (self.root / SPECS["af3"][0]).write_text(json.dumps(actual))
        result = self.read(freeze_refinement_outputs(self.root, self.engine, self.contract))
        self.assertEqual(len(result["artifacts"]), 14)
        self.assertIn("af3_failures.parquet", result["artifacts"])
        self.assertFalse(result["scientific_quality_verified"])

    def test_actual_covdock_gate_nested_artifacts_are_rehashed(self):
        from covscreen.covdock_gate import gate_covdock_candidates
        self.fixture("covdock")
        gate_covdock_candidates(pd.DataFrame(self.rows), self.root, final_n=2, min_success_rate=1)
        gate = self.root / "stage5_gate_manifest.json"
        self.manifest["outputs"][gate.name] = {"path": str(gate), "sha256": sha(gate)}
        (self.root / SPECS["covdock"][0]).write_text(json.dumps(self.manifest))
        evidence = freeze_refinement_outputs(self.root, self.engine, self.contract)
        result = self.read(evidence)
        self.assertIn("final_candidates.csv", result["artifacts"])
        self.assertFalse(result["scientific_quality_verified"])
        (self.root / "final_candidates.csv").write_text("altered nested artifact")
        with self.assertRaisesRegex(ValueError, "checksum"):
            self.read(evidence)

    def strict_gate_fixture(self):
        from covscreen.covdock_gate import STRICT_ALGORITHM_VERSION, gate_covdock_candidates
        self.fixture("covdock")
        gate_covdock_candidates(pd.DataFrame(self.rows), self.root, final_n=2, min_success_rate=1,
                               algorithm_version=STRICT_ALGORITHM_VERSION)
        gate = self.root / "stage5_gate_manifest.json"
        self.manifest["outputs"][gate.name] = {"path": str(gate), "sha256": sha(gate)}
        (self.root / SPECS["covdock"][0]).write_text(json.dumps(self.manifest))
        return STRICT_ALGORITHM_VERSION

    def test_strict_gate_requires_explicit_frozen_reader_selection(self):
        version = self.strict_gate_fixture()
        with self.assertRaisesRegex(ValueError, "algorithm differs"):
            freeze_refinement_outputs(self.root, self.engine, self.contract)
        evidence = freeze_refinement_outputs(self.root, self.engine, self.contract, expected_gate_version=version)
        result = read_refinement_outputs(self.root, self.engine, self.contract, evidence["sha256"], expected_gate_version=version)
        self.assertEqual(len(result["candidates"]), 2)
        self.assertFalse(result["scientific_quality_verified"])
        with self.assertRaisesRegex(ValueError, "algorithm differs"):
            self.read(evidence)

    def test_strict_reader_rejects_absent_gate_unknown_policy_and_other_engine(self):
        from covscreen.covdock_gate import STRICT_ALGORITHM_VERSION
        self.fixture("covdock")
        for engine, version, error in (("covdock", STRICT_ALGORITHM_VERSION, "requires its saved"),
                                      ("covdock", "unknown", "Unknown expected"),
                                      ("af3", STRICT_ALGORITHM_VERSION, "another engine")):
            with self.assertRaisesRegex(ValueError, error):
                collect_refinement_outputs(self.root, engine, self.contract, expected_gate_version=version)

    def test_strict_quality_decisions_are_independently_recomputed(self):
        version = self.strict_gate_fixture()
        gate_file = self.root / "stage5_gate_manifest.json"
        gate = json.loads(gate_file.read_text())
        table = self.root / "stage5_gated_candidates.parquet"
        data = pd.read_parquet(table)
        self.assertFalse(data.review_eligible.any())
        data["review_eligible"] = True
        data.to_parquet(table, index=False)
        gate["outputs"][table.name]["sha256"] = sha(table)
        gate_file.write_text(json.dumps(gate))
        self.manifest["outputs"][gate_file.name]["sha256"] = sha(gate_file)
        (self.root / SPECS["covdock"][0]).write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "independent row assessment"):
            collect_refinement_outputs(self.root, self.engine, self.contract, expected_gate_version=version)

    def test_rewritten_quality_table_and_hashes_cannot_turn_unknown_into_pass(self):
        from covscreen.covdock_gate import gate_covdock_candidates
        self.fixture("covdock")
        gate_covdock_candidates(pd.DataFrame(self.rows), self.root, final_n=2, min_success_rate=1)
        gate_file = self.root / "stage5_gate_manifest.json"
        gate = json.loads(gate_file.read_text())
        gated_file = self.root / "stage5_gated_candidates.parquet"
        frame = pd.read_parquet(gated_file)
        self.assertTrue(frame["physical_validity"].eq("UNKNOWN").all())
        frame["physical_validity"] = "PASS"
        frame.to_parquet(gated_file, index=False)
        gate["outputs"][gated_file.name]["sha256"] = sha(gated_file)
        gate_file.write_text(json.dumps(gate))
        self.manifest["outputs"][gate_file.name] = {"path": str(gate_file), "sha256": sha(gate_file)}
        (self.root / SPECS["covdock"][0]).write_text(json.dumps(self.manifest))
        with self.assertRaisesRegex(ValueError, "independent row assessment"):
            collect_refinement_outputs(self.root, self.engine, self.contract)

    def confidence_fixture(self):
        from covscreen.af3_results import confidence_metrics
        self.fixture("af3")
        for row in self.rows:
            folder = Path(row["model_cif"]).parent
            name = row["job_name"]
            summary = {"ranking_score": .8, "iptm": .7, "ptm": .6, "has_clash": False,
                       "chain_pair_pae_min": [[1., 2.], [row["raw_score"], 3.]]}
            confidence = {"token_chain_ids": ["B", "L"], "pae": [[1., 2.], [row["raw_score"], 3.]],
                          "atom_chain_ids": ["B", "L"], "atom_plddts": [90., 70.]}
            (folder / (name + "_summary_confidences.json")).write_text(json.dumps(summary))
            (folder / (name + "_confidences.json")).write_text(json.dumps(confidence))
            row.update(confidence_metrics(summary, confidence, "B", "L"))
        self.persist()

    def verify_confidence(self):
        evidence = collect_refinement_outputs(self.root, self.engine, self.contract)
        verify_af3_confidence(self.root, evidence, protein_chain="B", ligand_chain="L")
        return evidence

    def test_af3_original_confidence_is_recomputed_without_accepting_quality(self):
        self.confidence_fixture()
        before = {path: (path.stat().st_mtime_ns, sha(path)) for path in self.root.rglob("*") if path.is_file()}
        evidence = self.verify_confidence()
        self.assertFalse(evidence["scientific_quality_verified"])
        self.assertFalse(evidence["resource_approval_granted"])
        self.assertEqual(before, {path: (path.stat().st_mtime_ns, sha(path)) for path in self.root.rglob("*") if path.is_file()})
        with self.assertRaisesRegex(ValueError, "chain missing"):
            verify_af3_confidence(self.root, evidence, protein_chain="A", ligand_chain="L")

    def test_af3_rewritten_table_with_new_hashes_cannot_invent_confidence_metrics(self):
        self.confidence_fixture()
        for field, value in (("raw_score", 19.), ("mpae", .1), ("iptm", .99), ("has_clash", 1.),
                             ("ligand_plddt_mean", 99.), ("ranking_score", None)):
            original = self.rows[0][field]
            self.rows[0][field] = value
            self.persist()
            with self.assertRaisesRegex(ValueError, "metric differs"):
                self.verify_confidence()
            self.rows[0][field] = original

    def test_af3_malformed_json_and_missing_witness_source_cannot_pass(self):
        self.confidence_fixture()
        evidence = self.verify_confidence()
        summary = next(self.root.rglob("*_summary_confidences.json"))
        name = summary.relative_to(self.root).as_posix()
        del evidence["artifacts"][name]
        with self.assertRaisesRegex(ValueError, "absent"):
            verify_af3_confidence(self.root, evidence, protein_chain="B", ligand_chain="L")
        summary.write_text("{}")
        with self.assertRaises((ValueError, KeyError)):
            self.verify_confidence()

    def test_af3_failed_rows_remain_failures_without_confidence_fabrication(self):
        self.confidence_fixture()
        self.rows[0].update(engine_status="FAILED", raw_score=None, failure_reason="engine_failed")
        self.persist()
        evidence = self.verify_confidence()
        self.assertEqual(evidence["candidates"][0]["failure_reason"], "engine_failed")
        evidence["candidates"] = []
        with self.assertRaisesRegex(ValueError, "denominator"):
            verify_af3_confidence(self.root, evidence, protein_chain="B", ligand_chain="L")


if __name__ == "__main__":
    unittest.main()
