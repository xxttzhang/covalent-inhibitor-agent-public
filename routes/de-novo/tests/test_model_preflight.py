"""Run the actual CPU preflight against real files and a temporary Git repository.

The checkpoint bytes are engineering fixtures, not a trained model. No GPU,
model import, Inspire transport, or authorization receipt is involved.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

GENERATOR = Path(__file__).resolve().parents[1] / "registry/egfr_c797_v1/generate.py"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ModelPreflight(unittest.TestCase):
    def test_real_preflight_rejects_missing_changed_and_unpinned_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            repo = root / "repo"
            repo.mkdir()
            subprocess.run(["git", "init", "-q", str(repo)], check=True)
            files = [repo / "scripts/sample_use.py",
                     repo / "data/trained_models/pxm/train_config/train.yml",
                     repo / "data/trained_models/pxm/checkpoints/pocketxmol.ckpt"]
            for file in files:
                file.parent.mkdir(parents=True, exist_ok=True)
                file.write_text("engineering fixture, not executable model\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                            "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"], check=True)
            commit = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
            profile = {"repo": str(repo), "repo_commit": commit, "max_slots": 1,
                       "external_hashes": {str(file): sha(file) for file in files}}

            def execute():
                (root / "profile.json").write_text(json.dumps(profile))
                (root / "request.json").write_text(json.dumps({
                    "slots": 1, "package_hashes": {"profile.json": sha(root / "profile.json")}}))
                return subprocess.run([sys.executable, str(GENERATOR), str(root), "--preflight"],
                                      capture_output=True, text=True)

            valid = execute()
            self.assertEqual(valid.returncode, 0, valid.stderr)
            receipt = json.loads(valid.stdout)
            self.assertEqual(receipt["state"], "ASSETS_VERIFIED")
            self.assertFalse(receipt["compute_submitted"])
            self.assertEqual(receipt["request_sha256"], sha(root / "request.json"))
            original = files[-1].read_bytes()
            files[-1].unlink()
            self.assertNotEqual(execute().returncode, 0)
            files[-1].write_bytes(b"changed checkpoint")
            self.assertIn("Model/input changed", execute().stderr)
            files[-1].write_bytes(original)
            del profile["external_hashes"][str(files[-1])]
            self.assertIn("Required model assets are not pinned", execute().stderr)
            self.assertFalse((root / "output").exists())
            self.assertFalse((root / "output.zip").exists())


if __name__ == "__main__":
    unittest.main()
