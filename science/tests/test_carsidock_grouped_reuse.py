import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from covscreen.jobs import sha256_file

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location("registered_carsi_reuse", SCRIPTS / "run_carsidock_registered.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(tmp_path):
    paths = {}
    for name in ("pdb", "reference_ligand", "checkpoint", "sdf"):
        paths[name] = tmp_path / name
        paths[name].write_text(name)
    args = SimpleNamespace(**{k: paths[k] for k in ("pdb", "reference_ligand", "checkpoint")},
                           covres="A:CYS:773", seed=20260725, num_conformer=1)
    prefix = tmp_path / "carsidock_michael_addition"
    for suffix in (".sdf", "_nodumat.sdf", "_withpckt.pdb"):
        Path(f"{prefix}{suffix}").write_text("retained test output")
    manifest = {"status": "SUCCESS", "return_code": 0,
                "enforce_electrophile_map": True,
                "availability": {"checkpoint_sha256": sha256_file(paths["checkpoint"])},
                "command": ["-p", str(paths["pdb"]), "-rl", str(paths["reference_ligand"]),
                            "-l", str(paths["sdf"]), "-rectype", "Michael Addition",
                            "-covres", args.covres, "--seed", str(args.seed), "--num_conformer", "1"]}
    path = Path(f"{prefix}.run_manifest.json")
    path.write_text(json.dumps(manifest))
    return args, prefix, paths["sdf"], path, manifest


def test_reuse_records_legacy_limit_without_claiming_new_docking(tmp_path):
    args, prefix, sdf, _, _ = fixture(tmp_path)
    result = module.validate_retained_group(prefix, sdf, args, "Michael Addition")
    assert not result["new_docking_performed"]
    assert not result["historical_input_hashes_verified"]
    assert len(result["output_hashes"]) == 3


def test_reuse_rejects_mismatched_input_and_recorded_hash(tmp_path):
    args, prefix, sdf, path, manifest = fixture(tmp_path)
    different = tmp_path / "different.sdf"
    different.write_text("different")
    with pytest.raises(ValueError, match="input differs"):
        module.validate_retained_group(prefix, different, args, "Michael Addition")
    manifest["input_sha256"] = {str(sdf.resolve()): "wrong historical hash"}
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="historical input hash mismatch"):
        module.validate_retained_group(prefix, sdf, args, "Michael Addition")


def test_reuse_rejects_failed_upstream(tmp_path):
    args, prefix, sdf, path, manifest = fixture(tmp_path)
    manifest["return_code"] = 1
    path.write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="did not complete"):
        module.validate_retained_group(prefix, sdf, args, "Michael Addition")
