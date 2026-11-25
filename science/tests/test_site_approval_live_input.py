"""Read-only integration check against explicitly supplied retained public inputs.

No prediction, signing or submission is performed. Run on the deployment host
with SITE_APPROVAL_TEST_ROOT, SITE_APPROVAL_TEST_ARTIFACT and
SITE_APPROVAL_TEST_SHA set to a verified acquisition from a real test session.
"""
import os
from pathlib import Path

import pytest

from covscreen.approval_scope import prepare


@pytest.mark.skipif(not os.environ.get("SITE_APPROVAL_TEST_ROOT"), reason="Explicit real-input fixture required")
def test_site_prediction_prepares_stable_exact_scope_without_submitting():
    root = Path(os.environ["SITE_APPROVAL_TEST_ROOT"])
    arguments = {
        "action": "start",
        "receptor_input_artifact_id": os.environ["SITE_APPROVAL_TEST_ARTIFACT"],
        "manifest_sha256": os.environ["SITE_APPROVAL_TEST_SHA"],
        "human_approved": True,
        "approval_note": "Read-only preparation regression; not an approval",
    }
    jobs = root / "state/jobs"
    before = sorted(str(p) for p in jobs.glob("*/spec.json"))
    result = prepare(root, "covalent_predict_cysteine_sites", arguments)
    again = prepare(root, "covalent_predict_cysteine_sites", arguments)
    assert result["status"] == "NATIVE_APPROVAL_PREPARED"
    assert result["compute_submitted"] is False
    assert result["scope_sha256"] == again["scope_sha256"]
    scope = result["scope"]
    assert scope["kind"] == "job"
    assert scope["spec"]["engine"] == "shared-deepcosi-site-prediction"
    assert scope["spec"]["parameters"]["manifest_sha256"] == arguments["manifest_sha256"]
    assert scope["spec"]["resource_budget"]["gpu_count"] == 0
    assert sorted(str(p) for p in jobs.glob("*/spec.json")) == before
