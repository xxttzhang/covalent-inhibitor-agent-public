"""Real artifact and chemistry tests; no model, docking, network or mock service."""
import copy
import csv
import importlib.util
import json
from pathlib import Path
import shutil
import tempfile
import unittest

from rdkit import Chem

SOURCE = Path(__file__).with_name("research-scientific-evaluator.py")
SPEC = importlib.util.spec_from_file_location("scientific_evaluator", SOURCE)
science = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(science)


def states(checks):
    return {check["id"]: check["status"] for check in checks}


class ScientificEvidenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.manifest = science.load_manifest()
        cls.pdb = science.WORKSPACE / cls.manifest["target"]["receptor_path"]
        cls.artifacts = science.WORKSPACE / "reports/layout-migration-20261012/gpu-final"
        cls.rows = science.csv_rows(cls.artifacts / "prepare/stage2_candidates.csv")
        cls.inputs = list(Chem.SDMolSupplier(str(cls.artifacts / "prepare/stage2_candidates.sdf")))
        cls.poses = list(Chem.SDMolSupplier(str(cls.artifacts / "carsidock/carsidock.sdf"), sanitize=False, removeHs=False))
        cls.config = science.yaml.safe_load((science.WORKSPACE / cls.manifest["recipe"]["path"]).read_text())
        cls.audit, cls.atoms = science.receptor_audit(cls.pdb, cls.manifest)

    def test_deposited_site_mapping_passes_but_unprepared_receptor_is_not_accepted(self):
        self.assertEqual(self.audit["mapping_status"], "pass")
        self.assertEqual(states(self.audit["checks"])["unique_reactive_atom"], "pass")
        self.assertEqual(self.audit["status"], "fail")
        unresolved = next(c for c in self.audit["checks"] if c["id"] == "protein_coordinates")["measurements"]["unresolved_atoms"]
        self.assertTrue(any(atom["occupancy"] == 0 for atom in unresolved))
        self.assertEqual({atom["number"] for atom in unresolved if atom["altloc"]}, {751, 831})

    def test_mutated_dbref_and_reactive_atom_are_detected(self):
        raw = self.pdb.read_text()
        changed = raw.replace("EGFR_HUMAN     695   1022", "EGFR_HUMAN     696   1023")
        lines = changed.splitlines()
        for index, line in enumerate(lines):
            if line.startswith("ATOM    788"):
                lines[index] = line[:16] + "A" + line[17:]
        with tempfile.TemporaryDirectory() as directory:
            pdb = Path(directory) / "changed.pdb"
            pdb.write_text("\n".join(lines) + "\n")
            result, _ = science.receptor_audit(pdb, self.manifest)
        self.assertEqual(states(result["checks"])["dbref_mapping"], "fail")
        self.assertEqual(states(result["checks"])["unique_reactive_atom"], "fail")
        self.assertEqual(result["mapping_status"], "fail")

    def test_changed_evaluator_or_runtime_binding_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            for field in ("sha256", "packages"):
                manifest = copy.deepcopy(self.manifest)
                manifest["evaluator"][field] = "modified"
                path.write_text(json.dumps(manifest))
                with self.assertRaises(ValueError):
                    science.load_manifest(path)

    def test_actual_selected_molecules_match_original_recipe(self):
        for row, sdf in zip(self.rows, self.inputs):
            self.assertEqual(science.combined(science.candidate_chemistry(row, sdf, self.config)), "pass")

    def test_parent_and_electrophile_mismatch_do_not_pass(self):
        row = {**self.rows[0], "parent_inchikey": self.rows[1]["parent_inchikey"], "electrophile_atom_index": "999"}
        result = states(science.candidate_chemistry(row, self.inputs[0], self.config))
        self.assertEqual(result["molecular_identity"], "fail")
        self.assertEqual(result["declared_warhead"], "fail")

    def test_real_products_recover_exact_declared_parent_and_wrong_parent_fails(self):
        for index, row in enumerate(self.rows):
            self.assertEqual(len(science.recover_parent(Chem.Mol(self.poses[index * 9]), Chem.MolFromSmiles(row["standardized_smiles"]))), 1)
        self.assertEqual(science.recover_parent(Chem.Mol(self.poses[0]), Chem.MolFromSmiles(self.rows[1]["standardized_smiles"])), [])

    def test_wrong_sulfur_position_fails_anchor_and_bond_checks(self):
        pose = Chem.Mol(self.poses[0])
        sulfur = science.recover_parent(pose, Chem.MolFromSmiles(self.rows[0]["standardized_smiles"]))[0][0]
        point = pose.GetConformer().GetAtomPosition(sulfur)
        pose.GetConformer().SetAtomPosition(sulfur, [point.x + 10, point.y, point.z])
        result = states(science.geometry_checks(pose, self.rows[0], self.atoms, self.manifest["geometry"]))
        self.assertEqual(result["covalent_anchor"], "fail")
        self.assertEqual(result["bond_lengths"], "fail")

    def test_mirrored_existing_stereochemistry_fails(self):
        pose = Chem.Mol(self.poses[18])
        for index, point in enumerate(pose.GetConformer().GetPositions()):
            pose.GetConformer().SetAtomPosition(index, [-float(point[0]), float(point[1]), float(point[2])])
        result = states(science.geometry_checks(pose, self.rows[2], self.atoms, self.manifest["geometry"]))
        self.assertEqual(result["retained_stereochemistry"], "fail")

    def test_historical_gpu_success_is_not_scientific_success(self):
        result = science.evaluate(self.artifacts, self.pdb, self.manifest)
        self.assertEqual(result["outcome"], "insufficient_evidence")
        self.assertEqual(result["counts"], {"pass": 0, "fail": 0, "unknown": 5})
        self.assertEqual(result["accepted_candidate_ids"], [])
        self.assertFalse(result["new_remote_compute"])

    def test_missing_pose_and_missing_artifacts_are_unknown(self):
        self.assertEqual(science.combined(science.geometry_checks(None, self.rows[0], self.atoms, self.manifest["geometry"])), "unknown")
        with tempfile.TemporaryDirectory() as directory:
            result = science.evaluate(Path(directory), self.pdb, self.manifest)
        self.assertEqual(result["outcome"], "insufficient_evidence")

    def test_duplicate_engine_denominator_is_integrity_error(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for relative in self.manifest["artifacts"].values():
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(self.artifacts / relative, path)
            path = root / self.manifest["artifacts"]["scoring_csv"]
            lines = path.read_text().splitlines()
            path.write_text("\n".join([*lines, lines[1]]) + "\n")
            with self.assertRaisesRegex(ValueError, "denominator"):
                science.evaluate(root, self.pdb, self.manifest)


if __name__ == "__main__":
    unittest.main(verbosity=2)
