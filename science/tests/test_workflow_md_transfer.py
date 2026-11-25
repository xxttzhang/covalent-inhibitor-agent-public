"""Real signed transport and pose freezer over labelled software-only inputs."""
import base64
from copy import deepcopy
import csv
import hashlib
import io
import json

import pytest
from covscreen.library_workflow import RESEARCH
from covscreen.workflow_store import load, save
from covscreen.jobs import ArtifactRegistry, JobSpec, RunResult
from covscreen.approval_scope import normalize_spec
from covscreen.native_approval import CURRENT, signed, seal_spec, decode
from covscreen_library_first import md_transfer
from covscreen_library_first.md_candidate_pose import load_candidate_pose_input
from test_workflow_refinement_handoff import research, exchange
from test_workflow_refinement_results import fixture, publication, report
from test_workflow_md_selection import choose


def sha(data):
    return hashlib.sha256(data).hexdigest()


def ready(research, row_update=None, receptor_identity=None):
    root, folder, _, _ = research
    value, observed = fixture(research)
    state = load(folder)
    run = "software-preparation"
    spec = JobSpec(run_id=run, request_id="software-only", project_id="fixture", route="library-first",
        engine="library-first-receptor-preparation", command=["software-fixture-never-executed"],
        working_directory=str(root), output_directory=str(root / "state/jobs" / run),
        resource_budget={"max_runtime_minutes": 1, "gpu_count": 0, "cpu_count": 1, "memory_gb": 1, "max_candidates": 1},
        execution_mode="execute", human_approved=True, approval_note="Isolated software fixture; no worker launched")
    scope = {"kind": "job", "runtime_sha256": None, "spec": normalize_spec(spec)}
    token = CURRENT.set(signed({"scope": scope, "session": "fixture", "message": "fixture"}))
    try:
        spec.provenance["native_approval"] = seal_spec(spec)
    finally:
        CURRENT.reset(token)
    result = RunResult(run_id=run, state="COMPLETED", engine=spec.engine, route=spec.route, project_id=spec.project_id,
        return_code=0, submitted_at="2026-10-19T00:00:00Z", message="Software fixture; no scientific computation",
        stdout_path="unused", stderr_path="unused")
    registry = ArtifactRegistry(root)
    registry.create(spec, result)
    identity = receptor_identity or {"entry_id": "4Z3V", "uniprot": "Q06187", "canonical_cys": 481, "author_chain": "A", "author_residue": "481"}
    prepared = registry.job_dir(run) / "artifacts/receptor-preparation/prepared/prepared-receptor.json"
    prepared.parent.mkdir(parents=True)
    prepared.write_text(json.dumps({"schema": "prepared-receptor-artifact-v1", "status": "prepared_isolated_protein_scope",
        **identity, "receptor_prepared": True, "campaign_ready": False}))
    context = root / "state/receptor_site_context" / ("b" * 32) / "site-context.json"
    context.parent.mkdir(parents=True)
    context.write_text(json.dumps({"schema": "receptor-site-context-artifact-v1",
        "status": "descriptive_site_context_completed_review_pending", "identity": identity,
        "parents": {"prepared_receptor_sha256": sha(prepared.read_bytes())}, "campaign_ready": False}))
    state.update(preparation={"run_id": run, "state": "COMPLETED", "manifest_sha256": sha(prepared.read_bytes())},
        receptor_plan={"approved_scope": scope, "request_id": spec.request_id},
        receptor_context={"id": "receptor-context-" + "b" * 32, "sha256": sha(context.read_bytes())})
    save(folder, state)
    candidate = value["report"]["candidates"][0]
    structure = b"opaque software MAEGZ fixture, not a validated native graph\n" * 6000
    row = {"route": "library-first", "task_id": value["reference"]["workflow_id"], "result_scope": "candidate-results-v1",
        "online_structural_status": "BOTH_STRUCTURAL_QC_PASSED", "compound_id": candidate["compound_id"],
        "input_sdf_index": candidate["input_sdf_index"], "standardized_smiles": "C=CC(=O)NC", "warhead_class": "acrylamide",
        "covdock_structure_pose": 12, "covdock_artifact_id": "art_structure", "covdock_artifact_version": "ver_structure",
        "covdock_artifact_sha256": sha(structure), "covdock_target_context": json.dumps({"artifact_sha256": sha(structure),
            "parameters": {"chain": "A", "residue": "CYS", "residue_number": 481, "reaction_type": "Michael Addition"}})}
    row.update(row_update or {})
    # The actual native candidate producer does not write target context.
    if not row_update or "covdock_target_context" not in row_update:
        row.pop("covdock_target_context")
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=list(row))
    writer.writeheader()
    writer.writerow(row)
    files = {"candidate_table": stream.getvalue().encode(), "covdock_structure": structure}
    value["delivery"] = publication(value)
    value["delivery"]["files"][0].update(sha256=sha(files["candidate_table"]), bytes=len(files["candidate_table"]))
    value["delivery"]["files"].append({"name": md_transfer.md_selection.STRUCTURE, "artifact_id": "art_structure",
        "version_id": "ver_structure", "project_id": "fixture", "sha256": sha(structure), "bytes": len(structure)})
    report(research, value, observed)
    choose(research, value, {"compound_id": candidate["compound_id"], "input_sdf_index": candidate["input_sdf_index"],
        "reason": "Explicit software choice for testing asset transfer without scientific claims"})
    return files, value, observed


def send(research, operation, **payload):
    if operation in {"md_transfer_open", "md_transfer_commit"}:
        payload.setdefault("md_native_target", target(research))
    return exchange(research, "prepare_md_input", operation, payload=payload)


def target(research):
    choice = load(research[1])["md_candidate"]
    source = choice["files"]["covdock_structure"]
    return {"schema": "library-md-native-target-v1", "choice_id": choice["id"],
        **{key: choice[key] for key in ("native_workflow_id", "native_workflow_digest", "task_id")},
        **{key: source[key] for key in ("artifact_id", "version_id")}, "artifact_sha256": source["sha256"],
        "parameters": {"chain": "A", "residue": "CYS", "residue_number": 481, "reaction_type": "Michael Addition"}}


def start(research):
    choice = load(research[1])["md_candidate"]
    send(research, "open")
    send(research, "md_transfer_open", md_choice_id=choice["id"])
    return choice


def chunk(research, key, data, offset=0, **overrides):
    choice = load(research[1])["md_candidate"]
    return send(research, "md_transfer_chunk", md_chunk={"choice_id": choice["id"], "file": key, "offset": offset,
        "data": base64.b64encode(data).decode(), **overrides})


def upload(research, files):
    for key, data in files.items():
        for offset in range(0, len(data), md_transfer.CHUNK_BYTES):
            chunk(research, key, data[offset:offset + md_transfer.CHUNK_BYTES], offset)


def test_signed_bytes_freeze_into_existing_registry_and_recover_without_compute(research):
    files, value, _ = ready(research)
    choice = start(research)
    before = load(research[1])
    upload(research, files)
    frozen = send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
    state = load(research[1])
    assert state["screening_handoff"] == before["screening_handoff"]
    assert state["refinement_handoff"] == before["refinement_handoff"]
    assert frozen["md_input"]["md_system_ready"] is False
    assert frozen["md_input"]["compute_submitted"] is False
    assert frozen["md_transfer"]["received_bytes"] == sum(map(len, files.values()))
    assert "files" not in frozen["md_transfer"]
    assert "release_md_candidate" not in frozen["workflow"]["tools"][RESEARCH]["actions"]
    result = frozen["md_input"]
    loaded = load_candidate_pose_input(research[0], result["artifact_id"], result["manifest_sha256"])
    assert loaded["manifest"]["pose_index"] == 12
    assert (loaded["directory"] / "covdock-source.maegz").read_bytes() == files["covdock_structure"]
    assert loaded["manifest"]["delivered_artifact"]["version_id"] == choice["files"]["covdock_structure"]["version_id"]
    assert loaded["manifest"]["native_target_context"] == target(research)
    assert "covdock_target_context" not in loaded["row"]
    # Frozen output survives a lost workflow-state commit and is reconciled by
    # reproducing and replaying the real frozen manifest, never adopted blindly.
    state["md_input"] = None
    state["md_transfer"]["state"] = "receiving"
    save(research[1], state)
    assert send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)["md_input"] == result
    revision = load(research[1])["revision"]
    assert send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)["workflow"]["revision"] == revision


def test_lost_chunk_reply_and_uncommitted_tail_resume_at_durable_offset(research):
    files, _, _ = ready(research)
    choice = start(research)
    key = "covdock_structure"
    data = files[key][:md_transfer.CHUNK_BYTES]
    chunk(research, key, data)
    before = load(research[1])
    chunk(research, key, data)
    assert load(research[1]) == before
    with pytest.raises(ValueError, match="Replayed"):
        chunk(research, key, b"bad")
    path = md_transfer.location(research[1], choice) / md_transfer.NAMES[key]
    with path.open("ab") as stream:
        stream.write(b"incomplete write before state commit")
    reopened = decode(send(research, "md_transfer_open", md_choice_id=choice["id"])["_host_handoff"])
    assert reopened["md_transfer"]["offsets"][key] == len(data)
    chunk(research, key, files[key][len(data):2 * len(data)], len(data))
    assert path.read_bytes() == files[key][:2 * len(data)]


def test_unconsumed_corrupt_bytes_are_retained_and_retransmitted_from_zero(research):
    files, value, _ = ready(research)
    choice = start(research)
    upload(research, files)
    directory = md_transfer.location(research[1], choice)
    path = directory / md_transfer.NAMES["candidate_table"]
    path.write_bytes(b"bad bytes")
    with pytest.raises(ValueError, match="actual bytes"):
        send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
    reopened = decode(send(research, "md_transfer_open", md_choice_id=choice["id"])["_host_handoff"])
    assert reopened["md_transfer"]["offsets"]["candidate_table"] == 0
    rejected, = directory.glob("*.rejected-*")
    assert rejected.read_bytes() == b"bad bytes"
    chunk(research, "candidate_table", files["candidate_table"])
    frozen = send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
    assert frozen["md_input"]["choice_id"] == choice["id"]
    path.write_bytes(b"changed after freeze")
    with pytest.raises(ValueError, match="Consumed MD bytes"):
        send(research, "md_transfer_open", md_choice_id=choice["id"])


@pytest.mark.parametrize("field", ["task_id", "covdock_artifact_id", "covdock_artifact_version"])
def test_even_hash_verified_table_must_bind_exact_native_identity(research, field):
    files, value, _ = ready(research, {field: "different-identity"})
    choice = start(research)
    upload(research, files)
    with pytest.raises(ValueError, match="workflow or structure version"):
        send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
    assert not load(research[1]).get("md_input")


@pytest.mark.parametrize("field", ["choice_id", "native_workflow_id", "native_workflow_digest", "task_id", "artifact_id", "version_id", "artifact_sha256", "missing"])
def test_native_target_must_bind_exact_selected_parents(research, field):
    ready(research)
    value = target(research)
    choice = load(research[1])["md_candidate"]
    if field != "missing": value[field] = "different"
    before = load(research[1])
    with pytest.raises(ValueError):
        send(research, "md_transfer_open", md_choice_id=choice["id"], md_native_target=None if field == "missing" else value)
    assert load(research[1]) == before


def test_changed_native_target_is_refused_and_wrong_receptor_cannot_freeze(research):
    files, value, _ = ready(research)
    native = target(research)
    native["parameters"]["residue_number"] = 797
    choice = load(research[1])["md_candidate"]
    send(research, "md_transfer_open", md_choice_id=choice["id"], md_native_target=native)
    upload(research, files)
    with pytest.raises(ValueError, match="context changed"):
        send(research, "md_transfer_open", md_choice_id=choice["id"])
    with pytest.raises(ValueError, match="context changed"):
        send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
    with pytest.raises(ValueError, match="receptor or chemistry"):
        send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value, md_native_target=native)
    assert not load(research[1]).get("md_input")


def test_previous_unconsumed_transfer_can_bind_native_context_without_replacing_bytes(research):
    files, value, _ = ready(research)
    choice = start(research)
    chunk(research, "candidate_table", files["candidate_table"])
    state = load(research[1])
    before = deepcopy(state["md_transfer"]["offsets"])
    del state["md_transfer"]["native_target_context"]
    save(research[1], state)
    send(research, "md_transfer_open", md_choice_id=choice["id"])
    assert load(research[1])["md_transfer"]["offsets"] == before
    assert load(research[1])["md_transfer"]["native_target_context"] == target(research)
    upload(research, files)
    send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
    state = load(research[1])
    del state["md_transfer"]["native_target_context"]
    save(research[1], state)
    with pytest.raises(ValueError, match="predates native target"):
        send(research, "md_transfer_open", md_choice_id=choice["id"])
    assert load(research[1]) == state


@pytest.mark.parametrize("case", ["gap", "size", "choice", "path", "base64", "boolean", "extra", "scope", "purpose", "symlink"])
def test_invalid_chunk_cannot_advance_transfer(research, case):
    files, _, _ = ready(research)
    choice = start(research)
    before = load(research[1])
    values = {"choice_id": choice["id"], "file": "candidate_table", "offset": 0, "data": base64.b64encode(b"abc").decode()}
    if case == "gap": values["offset"] = 3
    if case == "size": values["data"] = base64.b64encode(b"a" * (md_transfer.CHUNK_BYTES + 1)).decode()
    if case == "choice": values["choice_id"] = "md-choice-" + "f" * 32
    if case == "path": values["file"] = "../escape"
    if case == "base64": values["data"] = "@@@@"
    if case == "boolean": values["offset"] = False
    if case == "extra": values["path"] = "/tmp/escape"
    if case == "symlink":
        (md_transfer.location(research[1], choice) / md_transfer.NAMES["candidate_table"]).symlink_to(research[0] / "absent")
    with pytest.raises(ValueError):
        if case == "scope":
            send(research, "md_transfer_chunk", md_chunk=values, scope={**research[3], "project": "other"})
        elif case == "purpose":
            exchange(research, "select_md_candidate", "md_transfer_chunk", values={"compound_id": "x", "input_sdf_index": 0,
                "reason": "Cannot repurpose a candidate selection into file transfer"}, payload={"md_chunk": values})
        else:
            send(research, "md_transfer_chunk", md_chunk=values)
    assert load(research[1]) == before


@pytest.mark.parametrize("case", ["incomplete", "bytes", "parent", "approval", "context", "report", "unavailable", "manifest"])
def test_freezing_rejects_incomplete_changed_or_unbound_evidence(research, case):
    files, value, observed = ready(research)
    choice = start(research)
    if case != "incomplete": upload(research, files)
    if case == "bytes":
        (md_transfer.location(research[1], choice) / md_transfer.NAMES["covdock_structure"]).write_bytes(b"bad")
    if case in {"parent", "approval"}:
        state = load(research[1])
        if case == "parent": state["preparation"]["manifest_sha256"] = "f" * 64
        if case == "approval": state["receptor_plan"]["request_id"] = "another-request"
        save(research[1], state)
    if case == "context":
        (research[0] / "state/receptor_site_context" / ("b" * 32) / "site-context.json").write_text("{}")
    if case == "report": value = {**value, "checked_at": 1200}
    if case == "unavailable":
        report(research, {**value, "checked_at": 1200, "delivery": {**value["delivery"], "verification": "evidence_rejected", "files": []}}, observed)
    if case == "manifest":
        frozen = send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
        path = research[0] / "state/candidate_pose_input" / frozen["md_input"]["artifact_id"][21:] / "candidate-pose-input.json"
        document = json.loads(path.read_text())
        document["compound_id"] = "other"
        path.write_text(json.dumps(document))
    before = load(research[1])
    with pytest.raises(ValueError):
        send(research, "md_transfer_commit", md_choice_id=choice["id"], refinement_report=value)
    assert load(research[1]) == before
