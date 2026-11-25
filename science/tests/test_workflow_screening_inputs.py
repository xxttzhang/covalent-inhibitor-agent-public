"""Real receptor/reference audits; synthetic CSV records are software fixtures."""
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest
import yaml

from covscreen.jobs import sha256_file
from covscreen.layout import enable_library_first
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.workflow_store import load
from test_library_workflow import call, state_file
from test_receptor_handoff import bound, ROOT, ACQUISITION

enable_library_first()
from covscreen_library_first import library_subset, screening_inputs


def library(path):
    # Deliberately repeated rows verify record identity/denominators, not chemistry.
    path.write_text("SMILES,Compound_ID\n" + "C,synthetic-test-record\n" * 20001)
    return path


def registration(root):
    # macOS host tmpdir can use /var while its canonical location is /private/var.
    root = Path(root).resolve()
    source = library(root / "synthetic-source.csv")
    config = root / "configs"
    config.mkdir()
    (config / "local.inspire.yaml").write_text(yaml.safe_dump({"covindb": {"virtual_screening_library": str(source)}}))
    reference = config / "reference.sdf"
    shutil.copyfile(Path(__file__).parent / "fixtures/structures/1M17_AQ4_parent_pose.sdf", reference)
    pdb = root / "state/target_acquisition" / ACQUISITION / "structure.pdb"
    asset = {"pdb": str(pdb), "pdb_sha256": sha256_file(pdb), "reference_ligand": str(reference),
        "reference_sha256": sha256_file(reference), "component_id": "AQ4", "component_chain": "A", "component_residue_id": "999"}
    registry = config / "library_docking_references.json"
    registry.write_text(json.dumps({"schema": "library-docking-references-v1", "references": [asset]}))
    return source, registry


def test_real_workflow_freezes_bound_input_roles_without_jobs_and_invalidates_on_reconsider(bound):
    root, initial = bound
    source, registry = registration(root)
    before = sorted((root / "state/jobs").iterdir())
    with pytest.raises(ValueError, match="unavailable"):
        call(root, initial, RESEARCH, "prepare_screening_inputs", profile=screening_inputs.PROFILE)
    reviewed = call(root, initial, RESEARCH, "review_receptor")
    options = call(root, reviewed, RESEARCH, "screening_profiles")
    assert options["workflow"]["revision"] == reviewed["workflow"]["revision"]
    assert options["result"]["profiles"][0]["source_records"] == 20000
    with pytest.raises(ValueError, match="unexpected arguments"):
        call(root, reviewed, RESEARCH, "prepare_screening_inputs", profile=screening_inputs.PROFILE, library_path=str(source))
    frozen = call(root, reviewed, RESEARCH, "prepare_screening_inputs", profile=screening_inputs.PROFILE)
    result = frozen["result"]
    assert result["calculation_submitted"] is False and result["screening_authorized"] is False
    assert result["full_library_screening"] is False
    assert result["reference"]["component_id"] == "AQ4"
    assert result["reference"]["matched_heavy_atoms"] == 29
    assert result["input_roles"]["library"]["records"] == 20000
    assert str(root) not in json.dumps(result)
    state = load(state_file(root, frozen))
    record = screening_inputs.load_record(root, state)
    assert record["research_inputs"]["receptor"]["sha256"] == result["input_roles"]["receptor"]["sha256"]
    recipe = yaml.safe_load(Path(record["research_inputs"]["science_config"]["path"]).read_text())
    assert recipe["structure"]["pdb"] == record["research_inputs"]["receptor"]["path"]
    assert recipe["structure"]["reference_ligand"] == record["research_inputs"]["reference"]["path"]
    assert recipe["reference_ligand"] == "0WN"  # chemical prescreen reference remains distinct from AQ4
    assert call(root, frozen, RESEARCH, "read_screening_inputs")["result"] == result
    repeated = call(root, frozen, RESEARCH, "prepare_screening_inputs", profile=screening_inputs.PROFILE)
    assert repeated["result"] == result
    assert sorted((root / "state/jobs").iterdir()) == before
    # Read the frozen version without silently substituting an altered mutable source.
    source.write_text(source.read_text() + "CC,extra-test-record\n")
    assert call(root, repeated, RESEARCH, "read_screening_inputs")["result"] == result
    updated = call(root, repeated, RESEARCH, "prepare_screening_inputs", profile=screening_inputs.PROFILE)
    assert updated["result"]["source_library_sha256"] != result["source_library_sha256"]
    assert updated["result"]["artifact_id"] != result["artifact_id"]
    assert updated["branches"][-1]["screening_inputs"]["id"] == result["artifact_id"]
    state = load(state_file(root, updated))
    record = screening_inputs.load_record(root, state)
    reference = Path(record["research_inputs"]["reference"]["path"])
    original = reference.read_bytes()
    reference.write_bytes(original + b"changed\n")
    with pytest.raises(ValueError, match="checksum mismatch"):
        call(root, updated, RESEARCH, "read_screening_inputs")
    reference.write_bytes(original)
    parent = dict(state, workflow_id="library-flow-" + "f" * 24)
    with pytest.raises(ValueError, match="another workflow"):
        screening_inputs.load_record(root, parent)
    with pytest.raises(ValueError, match="Unsupported screening profile"):
        screening_inputs.sources(root, state, "unregistered")
    saved = registry.read_bytes()
    bad = json.loads(saved)
    bad["references"][0]["component_id"] = "0WN"
    registry.write_text(json.dumps(bad))
    with pytest.raises(ValueError, match="coverage differs"):
        screening_inputs.sources(root, state, screening_inputs.PROFILE)
    registry.write_bytes(saved)
    reconsidered = call(root, updated, ENTRY, "reconsider", return_to="structure", reason="Choose a different construct for the next scientific branch")
    assert load(state_file(root, reconsidered))["screening_inputs"] is None
    assert reconsidered["branches"][-1]["screening_inputs"]["id"] == updated["result"]["artifact_id"]
    assert sorted((root / "state/jobs").iterdir()) == before
    assert not list((root / "state/screening_inputs").glob(".preparing-*"))


def test_subset_preserves_legacy_provenance_and_cli_calls_the_canonical_freezer(tmp_path):
    source = library(tmp_path / "source.csv")
    output = tmp_path / "frozen"
    first = library_subset.freeze(source, output, 20000, sha256_file(source))
    assert first["resumed"] is False
    manifest = output / "manifest.json"
    data = json.loads(manifest.read_text())
    assert data["selection"]["unique_row_hashes"] == 1
    assert len(data["records"]) == 20000
    data["freezer_sha256"] = library_subset.LEGACY_FREEZER
    manifest.write_text(json.dumps(data, indent=2) + "\n")
    expected = manifest.read_bytes()
    resumed = library_subset.freeze(source, output, 20000, sha256_file(source))
    assert resumed["resumed"] is True
    assert resumed["manifest_sha256"] == sha256_file(manifest)
    assert manifest.read_bytes() == expected
    cli = subprocess.run([sys.executable, str(ROOT / "ops/freeze_research_library_subset.py"),
        "--source", str(source), "--output", str(output), "--expected-sha256", sha256_file(source)],
        capture_output=True, text=True, timeout=30, check=True)
    assert json.loads(cli.stdout) == resumed
    data["freezer_sha256"] = "f" * 64
    manifest.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="differs"):
        library_subset.freeze(source, output, 20000, sha256_file(source))


@pytest.mark.parametrize("change", ["source_hash", "records", "row", "output_symlink"])
def test_subset_rejects_changed_source_selection_or_frozen_records(tmp_path, change):
    source = library(tmp_path / "source.csv")
    output = tmp_path / "frozen"
    expected = sha256_file(source)
    library_subset.freeze(source, output, 20000, expected)
    if change == "source_hash":
        expected = "f" * 64
    if change == "row":
        (output / "library-prefix.csv").write_text("replaced")
    if change == "output_symlink":
        other = tmp_path / "other"
        output.rename(other)
        output.symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError):
        library_subset.freeze(source, output, 10 if change == "records" else 20000, expected)
