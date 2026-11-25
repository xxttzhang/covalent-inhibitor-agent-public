import hashlib
import json
from pathlib import Path

import pytest

from covscreen_library_first.md_system_conversion import convert_parameterized_cms


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def contract(tmp_path):
    source = tmp_path / "parameterized.cms"
    source.write_text("f_m_ct {\ns_m_title\n:::\n title \n}\n")
    source_sha = sha(source)
    audit = tmp_path / "parameterization-audit.json"
    audit.write_text(json.dumps({"system_id": "candidate-1",
        "status": "COVALENT_PARAMETERIZATION_AUDITED", "forcefield": "OPLS_2005",
        "water": "TIP3P", "simulation_state": "covalent_adduct",
        "parameters_audited": True, "simulation_started": False, "total_atoms": 1,
        "total_charge_e": 0.0, "parameterized_cms_sha256": source_sha}))
    return source, source_sha, audit, sha(audit)


def test_rejects_changed_parameter_audit_before_optional_runtime_import(tmp_path):
    source, source_sha, audit, audit_sha = contract(tmp_path)
    audit.write_text("{}")
    with pytest.raises(ValueError, match="Parameter audit checksum"):
        convert_parameterized_cms(source, source_sha, audit, audit_sha,
                                  tmp_path / "missing-intermol", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_rejects_unapproved_parameter_state_before_creating_output(tmp_path):
    source, source_sha, audit, _ = contract(tmp_path)
    value = json.loads(audit.read_text())
    value["status"] = "INCOMPLETE"
    audit.write_text(json.dumps(value))
    with pytest.raises(ValueError, match="conversion contract"):
        convert_parameterized_cms(source, source_sha, audit, sha(audit),
                                  tmp_path / "missing-intermol", tmp_path / "output")
    assert not (tmp_path / "output").exists()


def test_existing_output_is_rejected_before_optional_runtime_import(tmp_path):
    source, source_sha, audit, audit_sha = contract(tmp_path)
    output = tmp_path / "output"
    output.mkdir()
    intermol = tmp_path / "InterMol/intermol/desmond"
    intermol.mkdir(parents=True)
    (intermol / "desmond_parser.py").write_text("")
    with pytest.raises(ValueError, match="new directory"):
        convert_parameterized_cms(source, source_sha, audit, audit_sha,
                                  tmp_path / "InterMol", output)
