"""Replay an actual historical receptor; synthetic job signature grants no compute."""
from copy import deepcopy
import json
import os
from pathlib import Path
import shutil

import pytest

from covscreen import receptor_evidence
from covscreen.jobs import ArtifactRegistry, sha256_file
from covscreen.approval_scope import normalize_spec
from covscreen.native_approval import CURRENT, signed, seal_spec
from covscreen.workflow_store import load, save
from covscreen.library_workflow import ENTRY, RESEARCH
from covscreen.mcp.server import dispatch
from test_library_workflow import call, state_file

ROOT = Path(__file__).resolve().parents[2]
LIVE = Path(os.environ.get("COVSCREEN_RECEPTOR_FIXTURE_ROOT", "/missing-external-receptor-fixture"))
RUN = "20261014T072946-library-first-receptor-preparation-cd6429155a"
ACQUISITION = "3fe7754be1b14546a7141aa26cebde6b"


def project(root):
    if not (LIVE / 'fixture-origin.json').is_file():
        raise RuntimeError('Historical replay requires an explicit external COVSCREEN_RECEPTOR_FIXTURE_ROOT with fixture-origin.json')
    origin = json.loads((LIVE / 'fixture-origin.json').read_text())
    for item in origin['files']:
        path = LIVE / item['path']
        if not path.resolve().is_relative_to(LIVE.resolve()) or sha256_file(path) != item['sha256']:
            raise ValueError('External receptor fixture changed')
    acquisition = LIVE / "state/target_acquisition" / ACQUISITION
    raw = json.loads((acquisition / "target_acquisition.json").read_text())
    parent = raw["parents"]
    directories = [acquisition, LIVE / "state/target_identity" / parent["target_artifact_id"][7:],
        LIVE / "state/target_structures" / parent["discovery_artifact_id"][11:], LIVE / "state/jobs" / RUN]
    prepared = directories[-1] / "artifacts/receptor-preparation/prepared/prepared-receptor.json"
    context, = [path for path in (LIVE / "state/receptor_site_context").glob("*/site-context.json")
                if json.loads(path.read_text())["parents"]["prepared_receptor_sha256"] == sha256_file(prepared)]
    directories.append(context.parent)
    for source in directories:
        shutil.copytree(source, root / source.relative_to(LIVE))
    registry = ArtifactRegistry(root)
    spec = registry.load_spec(RUN)
    # Historical coordinates in an isolated registry; no new scientific run.
    spec = type(spec).model_validate(json.loads(json.dumps(spec.model_dump(mode="json")).replace(origin['original_root'], str(root))))
    spec.provenance["test_fixture"] = "historical receptor revalidation; no worker launched"
    scope = {"kind": "job", "runtime_sha256": None, "spec": normalize_spec(spec)}
    token = CURRENT.set(signed({"scope": scope, "session": "test-only", "message": "test-only"}))
    try:
        spec.provenance["native_approval"] = seal_spec(spec)
    finally:
        CURRENT.reset(token)
    registry.write_spec(spec)
    started = dispatch(root, ENTRY, {"action": "start", "request_id": "historical-receptor-replay", "target": "EGFR", "taxon": 9606})
    folder = state_file(root, started)
    state = load(folder)
    state.update(stage="SCREENING_PLAN", target_identity={"id": parent["target_artifact_id"], "sha256": parent["target_manifest_sha256"]},
        site_selection={"site": "C797", "reason": "Historical fixture to verify existing code; no biological claim", "reactivity_validated": False},
        discovery={"id": parent["discovery_artifact_id"], "sha256": parent["discovery_manifest_sha256"]},
        receptor_input={"id": "receptor-input-" + ACQUISITION, "sha256": sha256_file(acquisition / "target_acquisition.json")},
        receptor_plan={"approved_scope": scope, "request_id": spec.request_id},
        preparation={"run_id": RUN, "state": "COMPLETED", "manifest_sha256": sha256_file(prepared)},
        receptor_context={"id": "receptor-context-" + context.parent.name, "sha256": sha256_file(context)})
    save(folder, state)
    return dispatch(root, ENTRY, {"action": "status", "workflow_id": state["workflow_id"]})


@pytest.fixture
def bound(tmp_path, monkeypatch):
    key = tmp_path / "test-only.key"
    key.write_bytes(b"isolated-receptor-handoff-fixture-key-123456")
    key.chmod(0o600)
    monkeypatch.setenv("COVALENT_APPROVAL_KEY_FILE", str(key))
    return tmp_path, project(tmp_path)


def test_actual_audits_freeze_workflow_evidence_then_read_without_new_tasks(bound):
    root, current = bound
    before = sorted((root / "state/jobs").iterdir())
    result = call(root, current, RESEARCH, "review_receptor")
    record = result["result"]
    assert record["result"]["status"] == "RECEPTOR_EVIDENCE_REPLAYED"
    assert record["result"]["screening_authorized"] is False
    assert record["result"]["campaign_ready"] is False
    assert record["result"]["identity"]["canonical_cys"] == 797
    assert record["result"]["receptor"]["sha256"] == sha256_file(root / "state/jobs" / RUN / "artifacts/receptor-preparation/prepared/prepared-receptor.pdb")
    assert "path" not in record["result"]["receptor"]
    assert "inventory" not in record["result"]
    assert result["assessment"]["state"] == "not_evaluated"
    assert any(block["scope"] == "workflow_screening_handoff" for block in result["blockers"])
    reread = call(root, result, RESEARCH, "read_screening_receptor")
    assert reread["result"] == record
    assert reread["workflow"]["revision"] == result["workflow"]["revision"]
    assert sorted((root / "state/jobs").iterdir()) == before
    path = root / "state/screening_receptors" / record["artifact_id"][19:] / "screening-receptor.json"
    stored = path.read_bytes()
    proof = json.loads(stored)
    proof["result"]["screening_authorized"] = True
    path.write_text(json.dumps(proof))
    with pytest.raises(ValueError, match="checksum mismatch"):
        call(root, result, RESEARCH, "read_screening_receptor")
    path.write_bytes(stored)
    state = load(state_file(root, result))
    from covscreen_library_first import receptor_handoff
    altered = deepcopy(state)
    altered["workflow_id"] = "library-flow-" + "f" * 24
    with pytest.raises(ValueError, match="another workflow"):
        receptor_handoff.read(root, altered)
    reconsidered = call(root, result, ENTRY, "reconsider", return_to="structure", reason="Review a different construct before screening")
    assert load(state_file(root, reconsidered))["screening_receptor"] is None
    assert reconsidered["branches"][-1]["screening_receptor"]["id"] == record["artifact_id"]


@pytest.mark.parametrize("mutation", ["source", "copied_coordinate", "foreign_run", "context_parent", "symlink"])
def test_changed_parent_or_coordinates_stop_before_expensive_replay(bound, mutation):
    root, current = bound
    state = load(state_file(root, current))
    from covscreen_library_first.receptor_handoff import arguments
    args = arguments(state)
    pipeline = root / "state/jobs" / RUN / "artifacts/receptor-preparation"
    if mutation == "source":
        path = root / "state/target_acquisition" / ACQUISITION / "structure.pdb"
        path.write_bytes(path.read_bytes() + b"REMARK changed source\n")
    if mutation == "copied_coordinate":
        path = pipeline / "prepared/prepared-receptor.pdb"
        path.write_bytes(path.read_bytes() + b"REMARK changed prepared receptor\n")
    if mutation == "foreign_run":
        spec = ArtifactRegistry(root).load_spec(RUN)
        spec.parameters["manifest_sha256"] = "f" * 64
        ArtifactRegistry(root).write_spec(spec)
    if mutation == "context_parent":
        path = root / "state/receptor_site_context" / args["context_artifact_id"][17:] / "site-context.json"
        value = json.loads(path.read_text()); value["parents"]["prepared_receptor_sha256"] = "f" * 64
        path.write_text(json.dumps(value)); args["context_sha256"] = sha256_file(path)
    if mutation == "symlink":
        path = pipeline / "prepared"
        destination = root / "relocated-prepared"
        path.rename(destination); path.symlink_to(destination, target_is_directory=True)
    with pytest.raises(ValueError):
        receptor_evidence.inputs(root, **args)


def test_rehashed_false_quality_report_is_rejected_by_actual_auditor(bound):
    root, current = bound
    state = load(state_file(root, current))
    from covscreen_library_first.receptor_handoff import arguments
    pipeline = root / "state/jobs" / RUN / "artifacts/receptor-preparation"
    quality = pipeline / "identity-quality.json"
    record = json.loads(quality.read_text()); record["stereocenters_checked"] += 1
    quality.write_text(json.dumps(record))
    path = pipeline / "pipeline.json"
    manifest = json.loads(path.read_text()); manifest["artifacts"]["identity-quality.json"] = sha256_file(quality)
    path.write_text(json.dumps(manifest))
    evidence = receptor_evidence.inputs(root, **arguments(state))
    with pytest.raises(ValueError, match="identity audit differs from recomputation"):
        receptor_evidence.replay(root, evidence)
