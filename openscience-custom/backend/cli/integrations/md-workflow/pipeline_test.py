"""Actual filesystem/subprocess tests; deliberately not an MD acceptance test."""
import json
import fcntl
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

import pipeline
from pipeline import STAGES, digest, plan_read, read, run


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="md-workflow-contract-")
        self.root = Path(self.temp.name)
        self.package = self.root / "package"
        self.package.mkdir()
        self.output = self.root / "output"
        shutil.copyfile(Path(__file__).with_name("contract_fixture.py"), self.package / "adapter.py")
        self.plan = {
            "schema": "md-workflow-plan-v1", "run_id": "library-candidate-r1",
            "kernel_sha256": digest(pipeline.__file__),
            "phase": "pre_covalent", "engine": "OpenMM", "forcefield": {"family": "OPLS", "version": "OPLS_2005"},
            "evidence_scope": "software_test", "production_ps": 100, "frame_interval_ps": 10,
            "wall_seconds": 35, "files": {"adapter.py": digest(self.package / "adapter.py")},
            "stages": [{"name": name, "script": "adapter.py", "python": str(Path(sys.executable).resolve()),
                        "python_sha256": digest(sys.executable), "timeout_seconds": 5} for name in STAGES],
        }

    def tearDown(self):
        self.temp.cleanup()

    def freeze(self):
        (self.package / "plan.json").write_text(json.dumps(self.plan))
        return digest(self.package / "plan.json")

    def execute(self):
        return run(self.package, self.output, self.freeze())

    def test_all_stages_execute_once_and_complete_only_as_software_test(self):
        state = self.execute()
        self.assertEqual(state["state"], "SOFTWARE_TEST_COMPLETE_NOT_MD")
        self.assertEqual(list(state["stages"]), list(STAGES))
        first = (self.output / "production/evidence.json").stat().st_mtime_ns
        second = run(self.package, self.output, digest(self.package / "plan.json"))
        self.assertEqual(second["state"], state["state"])
        self.assertEqual((self.output / "production/evidence.json").stat().st_mtime_ns, first)

    def test_reactive_geometry_loss_is_not_a_prep_failure(self):
        self.execute()
        qc = read(self.output / "short_qc/evidence.json")
        self.assertEqual(qc["reactive_geometry_observation"], "lost")
        self.assertTrue((self.output / "production/evidence.json").exists())

    def test_adduct_rejects_prereactive_gating_claim(self):
        self.plan.update(phase="covalent_adduct", fixture="adduct_gating")
        with self.assertRaisesRegex(ValueError, "not prereactive"):
            self.execute()
        self.assertFalse((self.output / "report").exists())

    def test_adduct_uses_its_own_geometry_analysis(self):
        self.plan["phase"] = "covalent_adduct"
        self.assertEqual(self.execute()["state"], "SOFTWARE_TEST_COMPLETE_NOT_MD")

    def test_missing_loop_and_bad_chemistry_stop_before_production(self):
        for index, mode in enumerate(("missing_loop", "bad_chemistry")):
            with self.subTest(mode=mode):
                self.output = self.root / f"output-{index}"
                self.plan["fixture"] = mode
                with self.assertRaisesRegex(ValueError, "checks did not pass"):
                    self.execute()
                self.assertFalse((self.output / "production").exists())

    def test_failed_stage_is_not_retried(self):
        self.plan["fixture"] = "nonzero"
        frozen = self.freeze()
        with self.assertRaisesRegex(ValueError, "Adapter failed"):
            run(self.package, self.output, frozen)
        self.assertEqual(read(self.output / "status.json")["state"], "NEEDS_REVIEW")
        before = (self.output / "short_qc/stdout.log").stat().st_mtime_ns
        with self.assertRaisesRegex(ValueError, "never replay"):
            run(self.package, self.output, frozen)
        self.assertEqual((self.output / "short_qc/stdout.log").stat().st_mtime_ns, before)

    def test_incomplete_trajectory_cannot_reach_analysis(self):
        self.plan["fixture"] = "incomplete"
        with self.assertRaisesRegex(ValueError, "Incomplete trajectory"):
            self.execute()
        self.assertFalse((self.output / "analysis").exists())

    def test_source_change_prevents_execution(self):
        frozen = self.freeze()
        (self.package / "adapter.py").write_text("raise SystemExit(0)")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            run(self.package, self.output, frozen)
        self.assertFalse(self.output.exists())

    def test_old_evidence_is_reverified_and_tampering_does_not_recompute(self):
        self.execute()
        (self.output / "prepare/software-test.txt").write_text("changed")
        with self.assertRaisesRegex(ValueError, "hash mismatch"):
            run(self.package, self.output, digest(self.package / "plan.json"))
        self.assertEqual(read(self.output / "status.json")["state"], "NEEDS_REVIEW")

    def test_identity_scope_and_upstream_are_mandatory(self):
        for mode in ("wrong_run", "fake_science", "wrong_upstream", "escape"):
            with self.subTest(mode=mode):
                self.output = self.root / mode
                self.plan["fixture"] = mode
                with self.assertRaises(ValueError):
                    self.execute()
                self.assertEqual(read(self.output / "status.json")["state"], "NEEDS_REVIEW")

    def test_timeout_preserves_failure_and_kills_own_child(self):
        self.plan["fixture"] = "timeout"
        self.plan["stages"][0]["timeout_seconds"] = 1
        with self.assertRaises(subprocess.TimeoutExpired):
            self.execute()
        self.assertEqual(read(self.output / "status.json")["state"], "NEEDS_REVIEW")
        self.assertFalse((self.output / "parameters").exists())

    def test_missing_stage_and_forcefield_fallback_are_rejected(self):
        self.plan["stages"].pop(2)
        with self.assertRaisesRegex(ValueError, "All workflow stages"):
            plan_read(self.package, self.freeze())
        self.plan["forcefield"]["family"] = "GAFF2"
        with self.assertRaisesRegex(ValueError, "No engine/forcefield fallback"):
            plan_read(self.package, self.freeze())

    def test_stage_limits_cannot_exceed_frozen_budget(self):
        self.plan["wall_seconds"] = 1
        with self.assertRaisesRegex(ValueError, "exceed allocation"):
            self.execute()

    def test_concurrent_worker_cannot_start_a_second_pipeline(self):
        frozen = self.freeze()
        self.output.mkdir()
        with (self.output / ".lock").open("a") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(BlockingIOError):
                run(self.package, self.output, frozen)
        self.assertFalse((self.output / "prepare").exists())


if __name__ == "__main__":
    unittest.main()
