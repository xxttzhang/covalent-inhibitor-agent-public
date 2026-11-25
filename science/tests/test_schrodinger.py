from covscreen.schrodinger import validate_license_config


def test_license_config_accepts_server_syntax():
    assert validate_license_config("27008@license.example.edu")[0] is True


def test_license_config_rejects_marker_in_value():
    accepted, reason = validate_license_config("/tmp/patcher/license")
    assert accepted is False
    assert "unsafe marker" in reason


def test_license_config_does_not_infer_entitlement_from_unused_sibling(tmp_path):
    license_path = tmp_path / "license"
    license_path.write_text("vendor-issued-placeholder")
    (tmp_path / "Crack_Only.rar").write_text("")
    accepted, reason = validate_license_config(str(license_path))
    assert accepted is True
    assert reason == "configured"
    assert license_path.read_text() == "vendor-issued-placeholder"


def test_license_config_rejects_empty_configuration():
    assert validate_license_config("  ")[0] is False
