import hashlib
import json
import os
from pathlib import Path

import pytest

from covscreen.receptor_input import load_receptor_preparation_input
from covscreen.receptor_site_context import assess_receptor_site_context


ROOT = Path(__file__).resolve().parents[2]
REPORT = Path(os.environ.get("COVSCREEN_RECEPTOR_FIXTURE_ROOT", str(ROOT / "reports/library-first-target-identity-20261014")))
STATE = REPORT / "state"
CASES = [
    ("3fe7754be1b14546a7141aa26cebde6b",
     "20261014T072946-library-first-receptor-preparation-cd6429155a"),
    ("43ef986bc59c43e695498b656735bcf8",
     "20261014T074853-library-first-receptor-preparation-bdab52d146"),
]


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


@pytest.mark.parametrize("artifact_hex,job_id", CASES)
def test_real_prepared_receptors_produce_descriptive_context(tmp_path, artifact_hex, job_id):
    acquisition = STATE / "target_acquisition" / artifact_hex
    acquisition_sha = sha(acquisition / "target_acquisition.json")
    loaded = load_receptor_preparation_input(REPORT,
        "receptor-input-" + artifact_hex, acquisition_sha)
    prepared = STATE / "jobs" / job_id / "artifacts/receptor-preparation/prepared"
    prepared_sha = sha(prepared / "prepared-receptor.json")
    result = assess_receptor_site_context(acquisition, acquisition_sha, loaded["acquisition"],
        prepared, prepared_sha, tmp_path / artifact_hex)
    assert result["status"] == "descriptive_site_context_completed_review_pending"
    assert result["prepared_isolated_protein"]["sg_sasa_angstrom_squared"] >= 0
    assert result["prepared_isolated_protein"]["cysteine_residue_sasa_angstrom_squared"] >= 0
    assert result["prepared_isolated_protein"]["protein_shell"]
    assert result["binding_site_suitability_validated"] is False
    assert result["reactivity_validated"] is False
    assert result["campaign_ready"] is False
    assert result["calculation_submitted"] is False
    assert len(result["parents"]["target_acquisition_sha256"]) == 64
    assert len(result["parents"]["prepared_receptor_sha256"]) == 64


def test_prepared_manifest_cannot_be_rehashed_to_change_parent(tmp_path):
    artifact_hex, job_id = CASES[0]
    acquisition = STATE / "target_acquisition" / artifact_hex
    acquisition_sha = sha(acquisition / "target_acquisition.json")
    loaded = load_receptor_preparation_input(REPORT,
        "receptor-input-" + artifact_hex, acquisition_sha)
    source = STATE / "jobs" / job_id / "artifacts/receptor-preparation/prepared"
    prepared = tmp_path / "prepared"
    prepared.mkdir()
    for path in source.iterdir():
        if path.is_file():
            (prepared / path.name).write_bytes(path.read_bytes())
    manifest = json.loads((prepared / "prepared-receptor.json").read_text())
    manifest["parents"]["target_acquisition_sha256"] = "0" * 64
    (prepared / "prepared-receptor.json").write_text(json.dumps(manifest))
    with pytest.raises(ValueError, match="matching scoped artifact"):
        assess_receptor_site_context(acquisition, acquisition_sha, loaded["acquisition"], prepared,
            sha(prepared / "prepared-receptor.json"), tmp_path / "context")


def test_output_directory_is_immutable(tmp_path):
    artifact_hex, job_id = CASES[0]
    acquisition = STATE / "target_acquisition" / artifact_hex
    acquisition_sha = sha(acquisition / "target_acquisition.json")
    loaded = load_receptor_preparation_input(REPORT,
        "receptor-input-" + artifact_hex, acquisition_sha)
    prepared = STATE / "jobs" / job_id / "artifacts/receptor-preparation/prepared"
    with pytest.raises(ValueError, match="new directory"):
        assess_receptor_site_context(acquisition, acquisition_sha, loaded["acquisition"], prepared,
            sha(prepared / "prepared-receptor.json"), tmp_path)
