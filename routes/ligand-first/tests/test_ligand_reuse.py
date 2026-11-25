import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from covscreen.ligand_reuse import read_minimal_reuse


class ReuseTests(unittest.TestCase):
    def test_real_v4_contains_verified_pose_evidence(self):
        root = (Path(__file__).resolve().parents[3] / "science")
        result = read_minimal_reuse(root, target="EGFR", site="C797", parent="erlotinib")
        self.assertEqual(result["candidate_count"], 2)
        self.assertTrue(result["source_hashes_verified"])
        self.assertIn("2D chemical structures", result["artifact_notes"]["stage6_final_candidates.sdf"])
        self.assertIn("without ellipses", result["artifact_notes"]["paths"])
        self.assertIn("Input candidates: 2", result["report_markdown"])
        self.assertIn("not the chat workspace", result["artifact_path_scope"])
        self.assertEqual(len(result["artifact_sha256"]["stage6_selection_report.md"]), 64)
        self.assertEqual([r["input_sdf_index"] for r in result["candidates"]], ["65", "59"])
        for row in result["candidates"]:
            self.assertEqual(row["covdock_gate"], "A")
            self.assertEqual(row["pose_topology_status"], "VERIFIED_FIXED_NAME_GRAPH")
            self.assertLess(float(row["af3_covdock_ligand_rmsd_angstrom"]), 3)

    def test_corrupt_v4_never_silently_falls_back(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = root / "reports/ligand_minimal_reuse_20260924_v4"
            report.mkdir(parents=True)
            (report / "reuse_manifest.json").write_text("{}")
            with self.assertRaisesRegex(ValueError, "Unexpected reuse scope"):
                read_minimal_reuse(root, target="EGFR", site="C797", parent="erlotinib")

    def test_wrong_identity_never_returns_egfr(self):
        self.assertEqual(read_minimal_reuse(Path("."), target="BTK", site="C481",
                                           parent="erlotinib")["status"], "no_matching_reuse")

    def test_missing_install(self):
        with tempfile.TemporaryDirectory() as temp:
            self.assertEqual(read_minimal_reuse(Path(temp), target="EGFR", site="C797",
                                               parent="erlotinib")["status"], "reuse_not_installed")

    def test_hash_change_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report = root / "reports/ligand_minimal_reuse_20260924_v3"
            report.mkdir(parents=True)
            evidence = root / "evidence.csv"
            evidence.write_text("modified")
            (report / "reuse_manifest.json").write_text(json.dumps({
                "mode": "historical_result_reuse", "selected_input_indices": [59, 65],
                "platform_acceptance_completed": False, "new_calculation_submitted": False,
                "source_evidence": [{"path": "evidence.csv",
                                     "sha256": hashlib.sha256(b"original").hexdigest()}]}))
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                read_minimal_reuse(root, target="EGFR", site="C797", parent="erlotinib")


if __name__ == "__main__":
    unittest.main()
