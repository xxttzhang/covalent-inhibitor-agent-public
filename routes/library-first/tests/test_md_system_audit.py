import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_system_audit import audit_openmm_conversion


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def test_rejects_inconsistent_parent_chain_before_openmm_read(tmp_path):
    source = tmp_path / "source.cms"
    source.write_text("fixture")
    audit = tmp_path / "parameter.json"
    audit.write_text(json.dumps({"parameterized_cms_sha256": sha(source),
        "total_atoms": 1, "system_id": "candidate-1"}))
    conversion = tmp_path / "conversion"
    conversion.mkdir()
    manifest = conversion / "conversion.json"
    manifest.write_text(json.dumps({"schema": "openmm-conversion-artifact-v1",
        "status": "conversion_written_forcefield_audit_pending",
        "source_cms_sha256": "0" * 64, "parameter_audit_sha256": sha(audit),
        "system_id": "candidate-1", "atoms": 1,
        "independent_forcefield_audit_completed": False,
        "simulation_started": False, "artifacts": {}}))
    with pytest.raises(ValueError, match="parents are inconsistent"):
        audit_openmm_conversion(source, sha(source), audit, sha(audit), conversion,
                                sha(manifest), tmp_path / "output")
    assert not (tmp_path / "output").exists()
