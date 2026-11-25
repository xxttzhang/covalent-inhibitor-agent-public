"""Bound native publication bytes over the existing authenticated host channel.

Callers hold the research workflow lock. Progress belongs to its state, not a
second job ledger. This module neither submits compute nor grants MD readiness.
"""
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Literal
from uuid import uuid4

from pydantic import Field
from covscreen.workflow_protocol import StrictModel, changed
from . import md_selection
from .md_candidate_pose import freeze_candidate_pose, load_candidate_pose_input, sha, _selected_row

ACTIONS = {"prepare_md_input"}
OPERATIONS = {"md_transfer_open", "md_transfer_chunk", "md_transfer_commit"}
CHUNK_BYTES = 128 * 1024
NAMES = {"candidate_table": "candidate-results.csv", "covdock_structure": "covdock-source.maegz"}


class Chunk(StrictModel):
    choice_id: str = Field(pattern=r"^md-choice-[a-f0-9]{32}$")
    file: Literal["candidate_table", "covdock_structure"]
    offset: int = Field(ge=0)
    data: str = Field(min_length=1, max_length=4 * ((CHUNK_BYTES + 2) // 3))


class TargetParameters(StrictModel):
    chain: str = Field(min_length=1, max_length=10)
    residue: Literal["CYS"]
    residue_number: int = Field(gt=0)
    reaction_type: str = Field(min_length=1, max_length=200)


class NativeTarget(StrictModel):
    schema_name: Literal["library-md-native-target-v1"] = Field(alias="schema")
    choice_id: str = Field(pattern=r"^md-choice-[a-f0-9]{32}$")
    native_workflow_id: str = Field(pattern=r"^refineflow-[a-f0-9]{24}$")
    native_workflow_digest: str = Field(pattern=r"^[a-f0-9]{64}$")
    task_id: str = Field(pattern=r"^screen-[a-f0-9]{24}$")
    artifact_id: str = Field(min_length=1, max_length=200)
    version_id: str = Field(min_length=1, max_length=200)
    artifact_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    parameters: TargetParameters


def native_target(choice, value):
    target = NativeTarget.model_validate(value).model_dump(by_alias=True)
    file = choice["files"]["covdock_structure"]
    if (target["choice_id"] != choice["id"]
        or any(target[key] != choice[key] for key in ("native_workflow_id", "native_workflow_digest", "task_id"))
        or any(target[key] != file[key] for key in ("artifact_id", "version_id"))
        or target["artifact_sha256"] != file["sha256"]):
        raise ValueError("Native target context differs from the chosen workflow or structure version")
    return target


def bound(state, identifier):
    choice = state.get("md_candidate")
    public = md_selection.public(state)
    if (not choice or identifier != choice["id"] or not public
        or public["state"] not in {"selected_asset_handoff_pending", "selected_input_frozen"}
        or (state["refinement_handoff"].get("execution_observation") or {}).get("execution_state") != "completed"):
        raise ValueError("MD transfer requires the exact choice and currently verified evidence")
    return choice


def safe_directory(path, anchor):
    path.relative_to(anchor)
    if any(p.is_symlink() for p in (path, *path.parents) if p == anchor or anchor in p.parents):
        raise ValueError("MD artifact directories cannot be symbolic links")
    path.mkdir(parents=True, exist_ok=True)
    return path


def location(folder, choice):
    return safe_directory(folder / "md-transfer" / choice["id"], folder)


def begin(folder, state, identifier, target):
    choice = bound(state, identifier)
    target = native_target(choice, target)
    current = state.get("md_transfer")
    if current:
        if current["choice_id"] != identifier or current["files"] != choice["files"]:
            raise ValueError("MD transfer cannot replace its frozen source identities")
        if current.get("native_target_context") is None:
            if state.get("md_input") or current["state"] == "frozen":
                raise ValueError("Frozen MD input predates native target binding; retain its frozen artifact")
            current["native_target_context"] = target
            changed(folder, state, "md_transfer_native_target_bound", {"choice_id": identifier})
        if current.get("native_target_context") != target:
            raise ValueError("MD transfer native target context changed; preserve its existing binding")
        directory = location(folder, choice)
        repaired = []
        for key, name in NAMES.items():
            path = directory / name
            offset = current["offsets"][key]
            if path.is_symlink() or path.exists() and not path.is_file():
                raise ValueError("MD transfer requires a regular file")
            invalid = (not path.exists() and offset > 0 or path.exists() and (
                path.stat().st_size < offset or offset == choice["files"][key]["bytes"]
                and (path.stat().st_size != offset or sha(path) != choice["files"][key]["sha256"])))
            if invalid:
                if state.get("md_input") or current["state"] == "frozen":
                    raise ValueError("Consumed MD bytes changed; preserve the frozen input for review")
                if path.exists():
                    path.rename(directory / (name + ".rejected-" + uuid4().hex))
                current["offsets"][key] = 0
                repaired.append(key)
        if repaired:
            changed(folder, state, "md_transfer_bytes_rejected", {"files": repaired})
        return
    location(folder, choice)
    state["md_transfer"] = {"schema": "library-md-transfer-v1", "choice_id": identifier,
        "files": json.loads(json.dumps(choice["files"])), "offsets": {key: 0 for key in NAMES},
        "state": "receiving", "native_target_context": target}
    changed(folder, state, "md_transfer_opened", {"choice_id": identifier})


def append(folder, state, value):
    chunk = Chunk.model_validate(value)
    choice = bound(state, chunk.choice_id)
    current = state.get("md_transfer")
    if not current or current["choice_id"] != choice["id"] or current["files"] != choice["files"]:
        raise ValueError("Open the exact MD transfer before sending bytes")
    data = base64.b64decode(chunk.data, validate=True)
    if not data or len(data) > CHUNK_BYTES or base64.b64encode(data).decode() != chunk.data:
        raise ValueError("Invalid MD byte chunk")
    offset = current["offsets"][chunk.file]
    end = chunk.offset + len(data)
    if chunk.offset > offset or end > choice["files"][chunk.file]["bytes"]:
        raise ValueError("MD byte chunk is outside the committed offset or file size")
    path = location(folder, choice) / NAMES[chunk.file]
    if path.is_symlink() or path.exists() and not path.is_file():
        raise ValueError("MD transfer requires a regular file")
    if not path.exists() and offset:
        raise ValueError("Committed MD transfer bytes are missing")
    # A lost state commit can leave a tail. Only bytes after the durable offset
    # may be replaced; the entire file is rehashed before it can be consumed.
    with path.open("r+b" if path.exists() else "x+b") as stream:
        if os.fstat(stream.fileno()).st_size < offset:
            raise ValueError("Committed MD transfer bytes are truncated")
        if chunk.offset < offset or current["state"] == "frozen":
            stream.seek(chunk.offset)
            if end > offset or stream.read(len(data)) != data:
                raise ValueError("Replayed MD byte chunk differs from committed bytes")
            return
        stream.truncate(offset)
        stream.seek(offset)
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    current["offsets"][chunk.file] = end
    changed(folder, state, "md_transfer_chunk_received", {"file": chunk.file, "offset": end})


def receptor_files(root, state, choice):
    from covscreen.execution import JobManager
    from .workflow import verify_preparation
    parents = choice["parents"]
    run = parents["preparation_run_id"]
    manager = JobManager(root)
    raw = root / "state/jobs" / run
    if any(p.is_symlink() for p in (raw, *raw.parents) if p == root or root in p.parents):
        raise ValueError("MD receptor job cannot be a symbolic link")
    spec = verify_preparation(root, state, run)
    result = manager.registry.load_result(run)
    if (spec.run_id != run or result.run_id != run or result.engine != spec.engine or result.route != spec.route
        or result.project_id != spec.project_id or spec.engine != "library-first-receptor-preparation"
        or result.state != "COMPLETED" or result.return_code != 0):
        raise ValueError("MD freezing requires the exact completed prepared receptor")
    prepared = manager.registry.job_dir(run) / "artifacts/receptor-preparation/prepared/prepared-receptor.json"
    context = root / "state/receptor_site_context" / parents["receptor_context"]["id"][17:] / "site-context.json"
    for path in (prepared, context):
        path.relative_to(root)
        if any(p.is_symlink() for p in (path, *path.parents) if p == root or root in p.parents):
            raise ValueError("MD receptor parents cannot be symbolic links")
    return prepared, context


def freeze(root, folder, state, identifier, target):
    choice = bound(state, identifier)
    target = native_target(choice, target)
    current = state.get("md_transfer")
    if not current or current["choice_id"] != identifier or current["files"] != choice["files"]:
        raise ValueError("Open the exact MD transfer before freezing")
    if current.get("native_target_context") != target:
        raise ValueError("Native target context changed before MD freezing")
    directory = location(folder, choice)
    for key, name in NAMES.items():
        path = directory / name
        file = choice["files"][key]
        if (current["offsets"][key] != file["bytes"] or path.is_symlink() or not path.is_file()
            or path.stat().st_size != file["bytes"] or sha(path) != file["sha256"]):
            raise ValueError("MD transfer is incomplete or its actual bytes changed")
    row = _selected_row(directory / NAMES["candidate_table"], **choice["candidate"])
    source = choice["files"]["covdock_structure"]
    if (row.get("task_id") != choice["native_workflow_id"] or row.get("covdock_artifact_id") != source["artifact_id"]
        or row.get("covdock_artifact_version") != source["version_id"]):
        raise ValueError("Candidate table replaced the native workflow or structure version")
    prepared, context = receptor_files(root, state, choice)
    identity = hashlib.sha256(json.dumps({"choice": choice, "native_target_context": target}, sort_keys=True,
        separators=(",", ":")).encode()).hexdigest()[:32]
    registry = safe_directory(root / "state/candidate_pose_input", root)
    output = registry / identity
    temporary = registry / (".freeze-" + uuid4().hex)
    artifact_id = "candidate-pose-input-" + identity
    try:
        parents = choice["parents"]
        manifest = freeze_candidate_pose(directory / NAMES["candidate_table"], choice["files"]["candidate_table"]["sha256"],
            directory / NAMES["covdock_structure"], source["sha256"], **choice["candidate"],
            prepared_receptor=prepared, prepared_receptor_sha256=parents["prepared_receptor_manifest_sha256"],
            receptor_context=context, receptor_context_sha256=parents["receptor_context"]["sha256"], output=temporary,
            native_target_context=target)
        for name in (*manifest["artifacts"], "candidate-pose-input.json"):
            with (temporary / name).open("rb") as stream:
                os.fsync(stream.fileno())
        if output.is_symlink():
            raise ValueError("Candidate pose output cannot be a symbolic link")
        if output.exists():
            existing = load_candidate_pose_input(root, artifact_id, sha(output / "candidate-pose-input.json"))
            if existing["manifest"] != manifest:
                raise ValueError("Existing frozen MD input differs from the exact selected evidence")
        else:
            temporary.rename(output)
        # Verify copied bytes, not merely the producer's successful return.
        checksum = sha(output / "candidate-pose-input.json")
        load_candidate_pose_input(root, artifact_id, checksum)
        value = {"choice_id": identifier, "artifact_id": artifact_id, "manifest_sha256": checksum,
            "status": manifest["status"], "native_graph_qc_completed": False,
            "parameterization_completed": False, "md_system_ready": False, "compute_submitted": False}
        if state.get("md_input") and state["md_input"] != value:
            raise ValueError("Frozen MD input cannot replace the existing binding")
        if state.get("md_input") != value or current["state"] != "frozen":
            state["md_input"] = value
            current["state"] = "frozen"
            changed(folder, state, "md_input_frozen", {"choice_id": identifier, "artifact_id": artifact_id})
    finally:
        if temporary.exists():
            shutil.rmtree(temporary)


def public(state):
    current = state.get("md_transfer")
    if not current:
        return None
    return {"choice_id": current["choice_id"], "state": current["state"],
        "received_bytes": sum(current["offsets"].values()), "total_bytes": sum(f["bytes"] for f in current["files"].values()),
        "compute_submitted": False, "md_ready": False}
