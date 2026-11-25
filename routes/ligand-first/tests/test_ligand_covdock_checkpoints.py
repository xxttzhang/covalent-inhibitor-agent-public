import importlib.util
import json
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pandas as pd


SCRIPT = (Path(__file__).resolve().parents[3] / "science") / "scripts/run_ligand_first_covdock_grouped.py"


def _load():
    spec = importlib.util.spec_from_file_location("ligand_covdock_grouped", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    table = tmp_path / "candidates.parquet"
    pd.DataFrame({
        "input_sdf_index": [11, 12, 13],
        "compound_id": ["candidate-a", "candidate-b", "candidate-c"],
        "standardized_smiles": ["CC", "CCC", "CCCC"],
        "covdock_reaction_type": ["Michael Addition"] * 3,
        "af3_mpae_rank": [1, 2, 3],
    }).to_parquet(table, index=False)
    receptor = tmp_path / "receptor.pdb"
    receptor.write_text("ATOM\n")
    schrodinger = tmp_path / "schrodinger"
    schrodinger.mkdir()
    license_file = tmp_path / "license"
    license_file.write_text("redacted")
    return table, receptor, schrodinger, license_file


def _fake_registered(calls: list[list[str]]):
    def run(command, check=False, env=None):
        calls.append(command)
        output = Path(command[command.index("--output-dir") + 1])
        candidate = Path(command[command.index("--candidate-table") + 1])
        frame = pd.read_parquet(candidate)
        output.mkdir(parents=True, exist_ok=True)
        gated = frame.copy()
        gated["covdock_gate"] = "A"
        gated["covdock_score"] = pd.NA
        gated.to_parquet(output / "stage5_gated_candidates.parquet", index=False)
        scores = frame[["input_sdf_index"]].copy()
        scores["covdock_score"] = [-1.0] * len(scores)
        scores.to_parquet(output / "covdock_lo_all_scores.parquet", index=False)
        gated.iloc[0:0].to_parquet(output / "stage5_failures.parquet", index=False)
        (output / "covdock_lo_registered_manifest.json").write_text(json.dumps({
            "status": "completed",
            "input_count": len(frame),
            "input_candidate_table": str(candidate.resolve()),
        }))
        return SimpleNamespace(returncode=0)
    return run


def _argv(inputs, output: Path, *extra: str) -> list[str]:
    table, receptor, schrodinger, license_file = inputs
    return [
        str(SCRIPT),
        "--candidate-table", str(table),
        "--receptor", str(receptor),
        "--attachment-residue", "A:797",
        "--output-dir", str(output),
        "--schrodinger-root", str(schrodinger),
        "--license-file", str(license_file),
        *extra,
    ]


def test_default_behavior_remains_one_reaction_batch(tmp_path, monkeypatch):
    module = _load()
    inputs = _inputs(tmp_path)
    calls = []
    monkeypatch.setattr(module.subprocess, "run", _fake_registered(calls))
    monkeypatch.setattr(sys, "argv", _argv(inputs, tmp_path / "default"))
    assert module.main() == 0
    assert len(calls) == 1
    assert "--reuse-artifact-dir" not in calls[0]
    manifest = json.loads((tmp_path / "default/covdock_lo_registered_manifest.json").read_text())
    assert manifest["checkpoint_size"] == 0
    assert manifest["status"] == "completed"
    assert len(pd.read_parquet(tmp_path / "default/stage5_gated_candidates.parquet")) == 3


def test_candidate_checkpoints_reuse_only_complete_exact_shards(tmp_path, monkeypatch):
    module = _load()
    inputs = _inputs(tmp_path)
    first_calls = []
    monkeypatch.setattr(module.subprocess, "run", _fake_registered(first_calls))
    first = tmp_path / "first"
    monkeypatch.setattr(sys, "argv", _argv(inputs, first, "--checkpoint-size", "1"))
    assert module.main() == 0
    assert len(first_calls) == 3

    reaction = "reaction_groups/michael_addition/shards"
    damaged = first / reaction / "shard_0001_i12/covdock_lo_registered_manifest.json"
    payload = json.loads(damaged.read_text())
    payload["status"] = "quality_gate_failed"
    damaged.write_text(json.dumps(payload))

    second_calls = []
    monkeypatch.setattr(module.subprocess, "run", _fake_registered(second_calls))
    second = tmp_path / "second"
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(inputs, second, "--checkpoint-size", "1", "--reuse-artifact-dir", str(first)),
    )
    assert module.main() == 0
    assert len(second_calls) == 3
    reused = ["--reuse-artifact-dir" in command for command in second_calls]
    assert reused == [True, False, True]

    manifest = json.loads((second / "covdock_lo_registered_manifest.json").read_text())
    group = manifest["reaction_groups"]["Michael Addition"]
    assert [record["reused"] for record in group["shards"]] == [True, False, True]
    assert len({record["identity_sha256"] for record in group["shards"]}) == 3
    merged = pd.read_parquet(second / "stage5_gated_candidates.parquet")
    assert merged["input_sdf_index"].tolist() == [11, 12, 13]
    assert merged["covdock_score"].isna().all()  # Missing fields remain missing, never zero-filled.


def test_candidate_checkpoints_run_in_bounded_parallel_with_unique_job_names(
    tmp_path, monkeypatch
):
    module = _load()
    inputs = _inputs(tmp_path)
    calls: list[tuple[list[str], dict[str, str]]] = []
    lock = threading.Lock()
    active = 0
    maximum = 0

    def run(command, check=False, env=None):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.03)
        output = Path(command[command.index("--output-dir") + 1])
        candidate = Path(command[command.index("--candidate-table") + 1])
        frame = pd.read_parquet(candidate)
        output.mkdir(parents=True, exist_ok=True)
        gated = frame.assign(covdock_gate="A", covdock_score=pd.NA)
        gated.to_parquet(output / "stage5_gated_candidates.parquet", index=False)
        frame[["input_sdf_index"]].assign(covdock_score=-1.0).to_parquet(
            output / "covdock_lo_all_scores.parquet", index=False
        )
        gated.iloc[0:0].to_parquet(output / "stage5_failures.parquet", index=False)
        (output / "covdock_lo_registered_manifest.json").write_text(json.dumps({
            "status": "completed",
            "input_count": len(frame),
            "input_candidate_table": str(candidate.resolve()),
        }))
        with lock:
            calls.append((command, env or {}))
            active -= 1
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", run)
    output = tmp_path / "parallel"
    monkeypatch.setattr(
        sys,
        "argv",
        _argv(
            inputs,
            output,
            "--checkpoint-size", "1",
            "--checkpoint-workers", "3",
            "--checkpoint-worker-cpus", "4",
        ),
    )
    assert module.main() == 0
    assert maximum == 3
    names = [command[command.index("--jobname-prefix") + 1] for command, _ in calls]
    assert len(names) == len(set(names)) == 3
    assert all(environment["OMP_NUM_THREADS"] == "4" for _, environment in calls)
    manifest = json.loads((output / "covdock_lo_registered_manifest.json").read_text())
    assert manifest["checkpoint_workers"] == 3
    assert manifest["checkpoint_worker_cpus"] == 4
