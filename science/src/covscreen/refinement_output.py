"""Read-only engine artifact evidence, distinct from scientific acceptance.

The host must derive the output directory from its approved task, verify the code
release, and retain the returned manifest digest independently. Neither a local
manifest nor this collector authorizes compute or proves a chemically valid pose.
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import os
import re
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path

import pandas as pd

from .refinement_input import verify_refinement_identities
from .covdock_gate import (STRAIN_COLUMNS, ALGORITHM_VERSION as COVDOCK_GATE_VERSION,
    STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION, assess_covdock_candidates)

VERSION = "refinement-artifact-evidence-v1"
MANIFEST = "refinement_artifacts.json"
MAX_FILE_BYTES = 512 * 1024 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_PARSE_BYTES = 64 * 1024 * 1024
SPECS = {
    "af3": ("stage4_selection_manifest.json", "af3_all_scores.parquet"),
    "covdock": ("covdock_lo_registered_manifest.json", "covdock_lo_all_scores.parquet"),
}
COVDOCK_INSPECTION_FIELDS = (*STRAIN_COLUMNS, "covalent_link_present", "covalent_bond_length_angstrom",
    "severe_clash", "minimum_nonbonded_heavy_atom_distance_angstrom", "geometry_inspection_status",
    "target_atom_match_count", "external_target_bond_count", "covalent_partner_atom_index",
    "covalent_partner_atom_name", "covalent_partner_residue_name", "ligand_heavy_atom_count")


def measurement(value):
    # Nullable Parquet numeric columns become floats/NaN, unlike their source JSON.
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError("CovDock inspection measurements must be scalar")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def contract(value):
    if (not isinstance(value, dict) or set(value) != {"input_count", "input_sha256", "identity_sha256",
            "verified", "grants_resource_approval"} or type(value["input_count"]) is not int or
            not 1 <= value["input_count"] <= 100 or value["verified"] is not True or
            value["grants_resource_approval"] is not False or any(
                not isinstance(value[name], str) or not re.fullmatch(r"[a-f0-9]{64}", value[name])
                for name in ("input_sha256", "identity_sha256"))):
        raise ValueError("An exact independently frozen refinement input contract is required")
    return value


class Files:
    def __init__(self, root):
        root = Path(root)
        if root.is_symlink():
            raise ValueError("Output root cannot be a symbolic link")
        self.root = root.resolve(strict=True)
        self.artifacts = {}
        self.sizes = {}

    def path(self, value):
        if not isinstance(value, str) or not value or re.search(r"[\\\x00-\x1f\x7f]", value):
            raise ValueError("Invalid artifact path")
        if any(part in (".", "..") for part in value.split("/")):
            raise ValueError("Artifact path traverses directories")
        path = Path(value)
        path = path if path.is_absolute() else self.root / path
        relative = path.relative_to(self.root)
        if any((self.root / Path(*relative.parts[:position])).is_symlink()
               for position in range(1, len(relative.parts) + 1)):
            raise ValueError("Artifact symbolic links are not allowed")
        path.resolve(strict=True).relative_to(self.root)
        if not path.is_file():
            raise ValueError("Artifact must be a regular file")
        return path, relative.as_posix()

    def read(self, value, expected=None, parse=False):
        path, name = self.path(value)
        before = path.stat()
        limit = MAX_PARSE_BYTES if parse else MAX_FILE_BYTES
        if before.st_size > limit or len(self.artifacts) >= 800 and name not in self.artifacts:
            raise ValueError("Artifact exceeds bounded verification limits")
        checksum = hashlib.sha256()
        chunks = []
        size = 0
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(block)
                if size > limit:
                    raise ValueError("Artifact grew beyond verification limit")
                checksum.update(block)
                if parse:
                    chunks.append(block)
        after = path.stat()
        if (before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
            raise ValueError("Artifact changed during verification")
        actual = checksum.hexdigest()
        if expected is not None and actual != expected or name in self.artifacts and self.artifacts[name] != actual:
            raise ValueError("Artifact checksum differs: " + name)
        self.artifacts[name], self.sizes[name] = actual, size
        if sum(self.sizes.values()) > MAX_TOTAL_BYTES:
            raise ValueError("Artifact inventory exceeds total verification limit")
        return b"".join(chunks) if parse else name

    def json(self, value, expected=None):
        return json.loads(self.read(value, expected, parse=True))


def collect_refinement_outputs(root, engine, expected_contract, *, expected_gate_version=COVDOCK_GATE_VERSION):
    # The caller's frozen plan selects the reader, never an output manifest.
    # Keep legacy callers byte-compatible and require explicit admission of v3.
    if expected_gate_version not in {COVDOCK_GATE_VERSION, STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION}:
        raise ValueError("Unknown expected CovDock gate algorithm version")
    if engine != "covdock" and expected_gate_version != COVDOCK_GATE_VERSION:
        raise ValueError("CovDock gate selection cannot apply to another engine")
    expected_contract = contract(expected_contract)
    if engine not in SPECS:
        raise ValueError("Unknown refinement engine")
    files = Files(root)
    manifest_name, table_name = SPECS[engine]
    manifest = files.json(manifest_name)
    recorded = manifest.get("inputs", {}).get("refinement_input_contract") if engine == "af3" else manifest.get("refinement_input_contract")
    if recorded != expected_contract:
        raise ValueError("Engine manifest input differs from frozen selection")
    input_hash = (manifest.get("inputs", {}).get("candidate_table", {}).get("sha256")
                  if engine == "af3" else manifest.get("input_candidate_table_sha256"))
    if input_hash != expected_contract["input_sha256"]:
        raise ValueError("Engine's final input checksum differs from its initial frozen contract")
    if engine == "af3":
        execution = manifest.get("execution", {})
        if execution.get("resume_enabled") is not False or execution.get("reparse_only") is not False:
            raise ValueError("Platform refinement evidence cannot silently reuse historical AF3 outputs")
    else:
        resume = manifest.get("resume", {})
        if any(resume.get(key) is not False for key in ("requested", "ligprep_reused", "covdock_reused")) or resume.get("source_artifact_dir") is not None:
            raise ValueError("Platform refinement evidence cannot silently reuse historical CovDock outputs")
    outputs = manifest.get("outputs")
    if not isinstance(outputs, dict) or table_name not in outputs or not outputs:
        raise ValueError("Engine manifest lacks its complete output inventory")
    for name, item in outputs.items():
        if not isinstance(item, dict) or set(item) != {"path", "sha256"}:
            raise ValueError("Malformed output inventory entry")
        if not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]):
            raise ValueError("Output inventory lacks a valid checksum")
        _, relative = files.path(item["path"])
        if name != relative:  # Current registered wrappers write top-level table names.
            raise ValueError("Engine output role differs from artifact path")
        files.read(item["path"], item["sha256"])
    gate = None
    if engine == "covdock" and "stage5_gate_manifest.json" in outputs:
        gate = files.json("stage5_gate_manifest.json")
        nested = gate.get("outputs")
        if not isinstance(nested, dict) or not nested:
            raise ValueError("CovDock gate manifest lacks its output inventory")
        for name, item in nested.items():
            if (not isinstance(item, dict) or set(item) != {"path", "sha256"} or
                    not isinstance(item["sha256"], str) or not re.fullmatch(r"[a-f0-9]{64}", item["sha256"]) or
                    files.path(item["path"])[1] != name):
                raise ValueError("Malformed CovDock gate artifact")
            files.read(item["path"], item["sha256"])
    if expected_gate_version in {STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION} and gate is None:
        raise ValueError("Explicit CovDock gate policy requires its saved gate manifest")
    frame = pd.read_parquet(io.BytesIO(files.read(table_name, outputs[table_name]["sha256"], parse=True)))
    if gate is not None:
        # Recompute saved row-level decisions without rewriting any output.
        # This checks the recorded policy, not its resource approval or whether
        # every physical measurement exists; those remain independent gates.
        if gate.get("algorithm_version") != expected_gate_version:
            raise ValueError("CovDock gate algorithm differs from the frozen reader")
        policy = gate["policy"]
        expected = assess_covdock_candidates(frame, max_local_strain=policy["max_local_strain"],
            hard_max_strain=policy["hard_max_strain"], min_covalent_bond_length=policy["covalent_bond_length_range"][0],
            max_covalent_bond_length=policy["covalent_bond_length_range"][1], max_pose_rmsd=policy["max_pose_rmsd"],
            algorithm_version=expected_gate_version)
        stored = gate["outputs"]["stage5_gated_candidates.parquet"]
        actual = pd.read_parquet(io.BytesIO(files.read(stored["path"], stored["sha256"], parse=True)))
        try:
            pd.testing.assert_frame_equal(expected.sort_values("input_sdf_index").reset_index(drop=True),
                actual.sort_values("input_sdf_index").reset_index(drop=True), check_exact=True)
        except AssertionError as error:
            raise ValueError("CovDock stored quality decisions differ from independent row assessment") from error
    if not {"input_sdf_index", "compound_id", "engine_status", "raw_score", "failure_reason", "score_direction"}.issubset(frame.columns):
        raise ValueError("Engine score table lacks required identity/status/score columns")
    rows = frame.to_dict(orient="records")
    verify_refinement_identities(rows, expected_contract["input_count"], expected_contract["identity_sha256"])
    candidates, locations = [], set()
    for row in rows:
        if row["score_direction"] != "lower_is_better" or row["engine_status"] not in ("SUCCESS", "FAILED"):
            raise ValueError("Unexpected engine status or score direction")
        candidate = {"input_sdf_index": row["input_sdf_index"], "compound_id": row["compound_id"],
                     "engine_status": row["engine_status"], "score": None, "structure": None,
                     "failure_reason": None}
        if row["engine_status"] == "FAILED":
            if not isinstance(row["failure_reason"], str) or not row["failure_reason"].strip():
                raise ValueError("Failed candidate lacks a reason")
            candidate["failure_reason"] = row["failure_reason"]
            candidates.append(candidate)
            continue
        score = row["raw_score"]
        if type(score) not in (int, float) or not math.isfinite(score) or engine == "af3" and score < 0:
            raise ValueError("Successful candidate has an invalid score")
        candidate["score"] = float(score)
        if engine == "af3":
            path = files.read(row.get("model_cif"))
            name = row.get("job_name")
            if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or Path(path).name != name + "_model.cif":
                raise ValueError("AF3 model filename differs from candidate job")
            parent = Path(path).parent
            files.json(str(parent / (name + "_summary_confidences.json")))
            files.json(str(parent / (name + "_confidences.json")))
            candidate["structure"] = {"path": path, "pose_index": None}
        else:
            if manifest.get("input_score_source") != "core":
                raise ValueError("CovDock source is not the approved core subset")
            path = files.read(row.get("covdock_pose_file"))
            index = row.get("covdock_pose_index")
            # Pandas may represent valid integers as floats when failed rows have NaN.
            if type(index) not in (int, float) or not math.isfinite(index) or index < 0 or int(index) != index:
                raise ValueError("CovDock requires a zero-based structure entry index")
            audit_hash = row.get("covdock_inspection_sha256")
            if not isinstance(audit_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", audit_hash):
                raise ValueError("CovDock audit requires its recorded checksum")
            audit = files.json(row.get("covdock_inspection_file"), audit_hash)
            if files.path(audit.get("input"))[1] != path:
                raise ValueError("CovDock audit refers to another structure file")
            poses = [item for item in audit.get("rows", []) if item.get("pose_index") == int(index)]
            if len(poses) != 1 or poses[0].get("title") != row.get("covdock_title"):
                raise ValueError("CovDock structure entry is missing or ambiguous")
            identity = re.search(r"__i(\d+)(?:\D|$)", str(poses[0].get("title")))
            prop = manifest.get("score_property")
            if not identity or int(identity[1]) != row["input_sdf_index"] or not isinstance(prop, str) or poses[0].get(prop) != score:
                raise ValueError("CovDock audit identity or score differs from candidate table")
            for field in COVDOCK_INSPECTION_FIELDS:
                actual, recorded = measurement(row.get(field)), measurement(poses[0].get(field))
                if actual != recorded or isinstance(actual, bool) != isinstance(recorded, bool):
                    raise ValueError("CovDock physical measurement differs from structure audit: " + field)
            candidate["structure"] = {"path": path, "pose_index": int(index)}
        location = (candidate["structure"]["path"], candidate["structure"]["pose_index"])
        if location in locations:
            raise ValueError("Different candidates refer to the same structure entry")
        if files.sizes[location[0]] == 0:
            raise ValueError("Candidate structure is empty")
        locations.add(location)
        candidates.append(candidate)
    # Catch modifications while multiple related files were being collected.
    for name, checksum in list(files.artifacts.items()):
        files.read(name, checksum)
    return {"version": VERSION, "engine": engine, "input_contract": expected_contract,
            "candidates": sorted(candidates, key=lambda row: row["input_sdf_index"]),
            "artifacts": dict(sorted(files.artifacts.items())), "score_direction": "lower_is_better",
            "scientific_quality_verified": False, "resource_approval_granted": False}


def freeze_refinement_outputs(root, engine, expected_contract, *, expected_gate_version=COVDOCK_GATE_VERSION):
    value = collect_refinement_outputs(root, engine, expected_contract, expected_gate_version=expected_gate_version)
    contents = canonical(value) + b"\n"
    destination = Path(root) / MANIFEST
    # Do not silently replace an earlier result snapshot, including on retries.
    with destination.open("xb") as stream:
        stream.write(contents)
        stream.flush()
        os.fsync(stream.fileno())
    return {"path": str(destination), "sha256": hashlib.sha256(contents).hexdigest()}


def _covdock_sdk(request, runtime, script, timeout=45):
    """Bounded vendor reader process; never return vendor stderr or credentials."""
    encoded = canonical(request)
    if len(encoded) > 128 * 1024:
        raise ValueError("CovDock SDK request exceeds readback bounds")
    env = {key: os.environ[key] for key in ("HOME", "TMPDIR", "TEMP", "TMP") if key in os.environ}
    env.update(PATH=os.defpath, SCHRODINGER=runtime["schrodinger_root"],
               SCHROD_LICENSE_FILE=runtime["license_file"],
               PYTHONDONTWRITEBYTECODE="1", CUDA_VISIBLE_DEVICES="", OMP_NUM_THREADS="1")
    if runtime.get('runtime_lib_dir'):
        env['LD_LIBRARY_PATH'] = runtime['runtime_lib_dir']
    command = [str(Path(runtime["schrodinger_root"]) / "run"), "python3", "-B", str(script), "--readback-stdin"]
    try:
        return _read_covdock_response(command, env, str(Path(script).parent), encoded, timeout)
    except ValueError as error:
        from .covdock_readback import CovDockReadbackUnavailable
        raise CovDockReadbackUnavailable(str(error)) from None


def _read_covdock_response(command, env, cwd, encoded, timeout):
    """Internal transport, separated for real subprocess failure-path tests."""
    with tempfile.TemporaryFile() as source:
        source.write(encoded)
        source.seek(0)
        try:
            process = subprocess.Popen(command, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                cwd=cwd, env=env, start_new_session=True)
        except OSError:
            raise ValueError("CovDock SDK readback could not start") from None
        deadline = time.monotonic() + timeout
        output = bytearray()
        errors = 0
        try:
            with selectors.DefaultSelector() as streams:
                streams.register(process.stdout, selectors.EVENT_READ, "stdout")
                streams.register(process.stderr, selectors.EVENT_READ, "stderr")
                while streams.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise ValueError("CovDock SDK readback timed out")
                    for key, _ in streams.select(min(remaining, .2)):
                        block = os.read(key.fileobj.fileno(), 65536)
                        if not block:
                            streams.unregister(key.fileobj)
                            continue
                        if key.data == "stdout":
                            output.extend(block)
                        else:
                            errors += len(block)
                        if len(output) > 4 * 1024 * 1024 or errors > 64 * 1024:
                            raise ValueError("CovDock SDK response exceeds readback bounds")
            try:
                code = process.wait(timeout=max(.001, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                raise ValueError("CovDock SDK readback timed out") from None
            if code:
                raise ValueError("CovDock SDK readback failed (exit " + str(code) + ")")
            try:
                return json.loads(output)
            except (ValueError, UnicodeError):
                raise ValueError("CovDock SDK did not return valid readback JSON") from None
        finally:
            # The wrapper may have exited while a descendant still holds a pipe.
            # This is an isolated read-only process group, never a compute job.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            process.stdout.close()
            process.stderr.close()


def verify_covdock_structures(root, evidence, *, runtime, script, assets):
    """Reopen original Maestro containers with the frozen vendor SDK.

    Matching retained measurements proves provenance, not chemical validity or
    affinity. Missing/negative geometry remains negative; never turn it into PASS.
    The caller must validate the full immutable code release before invoking this.
    """
    if (evidence.get("engine") != "covdock" or evidence.get("scientific_quality_verified") is not False or
            runtime.get("name") != "covdock"):
        raise ValueError("CovDock readback requires frozen unaccepted engine evidence and target roles")
    from .covdock_receptor import attachment_site
    attachment_site(runtime.get('attachment_residue'))
    files = Files(root)
    hashes = evidence["artifacts"]
    for name, checksum in hashes.items():
        files.read(name, checksum)
    # Preserve the artifact witness rather than trusting another regenerated manifest.
    manifest = files.json(SPECS["covdock"][0], hashes[SPECS["covdock"][0]])
    rows = pd.read_parquet(io.BytesIO(files.read(SPECS["covdock"][1], hashes[SPECS["covdock"][1]], parse=True))).to_dict(orient="records")
    expected = contract(evidence["input_contract"])
    verify_refinement_identities(rows, expected["input_count"], expected["identity_sha256"])
    verify_refinement_identities(evidence["candidates"], expected["input_count"], expected["identity_sha256"])
    successful = [row for row in rows if row["engine_status"] == "SUCCESS"]
    names = sorted({files.path(row["covdock_pose_file"])[1] for row in successful})
    if not names:
        return  # All failures remain failures; no SDK output is invented.
    source = Files("/")
    required = [str(script), str(Path(runtime["schrodinger_root"]) / "run"), runtime["license_file"]]
    for name in required:
        checksum = assets.get(name)
        if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
            raise ValueError("CovDock SDK reader, launcher and license must be hash-bound")
        source.read(name, checksum)
    request = {"version": "covdock-structure-readback-v1", "attachment_residue": runtime["attachment_residue"],
               "files": [str(files.path(name)[0]) for name in names]}
    actual = _covdock_sdk(request, runtime, script)
    compare_covdock_inspections(files, hashes, manifest, successful, request, actual)
    for name, checksum in list(files.artifacts.items()):
        files.read(name, checksum)
    for name in required:
        source.read(name, assets[name])


def compare_covdock_inspections(files, hashes, manifest, successful, request, actual):
    """Compare actual SDK observations with the separately witnessed old audit."""
    if (not isinstance(actual, dict) or set(actual) != {"version", "attachment_residue", "audits", "scientific_quality_verified"} or
            actual["version"] != request["version"] or actual["attachment_residue"] != request["attachment_residue"] or
            actual["scientific_quality_verified"] is not False or not isinstance(actual["audits"], list) or
            len(actual["audits"]) != len(request["files"])):
        raise ValueError("CovDock SDK response differs from frozen request")
    observed = {}
    for audit in actual["audits"]:
        if (not isinstance(audit, dict) or audit.get("input") not in request["files"] or
                audit["input"] in observed or audit.get("geometry_requested") is not True or
                not isinstance(audit.get("rows"), list) or type(audit.get("pose_count")) is not int or
                not 1 <= audit["pose_count"] == len(audit["rows"]) <= 1000 or
                any(not isinstance(row, dict) or type(row.get("pose_index")) is not int or row["pose_index"] != index
                    for index, row in enumerate(audit["rows"]))):
            raise ValueError("CovDock SDK returned ambiguous structure entries")
        observed[audit["input"]] = audit
    for row in successful:
        name = str(files.path(row["covdock_pose_file"])[0])
        audit_name = files.path(row["covdock_inspection_file"])[1]
        if audit_name not in hashes or hashes[audit_name] != row["covdock_inspection_sha256"]:
            raise ValueError("CovDock original inspection is absent from artifact witness")
        retained = files.json(audit_name, hashes[audit_name])
        actual_rows = observed[name]["rows"]
        if (not isinstance(retained.get("rows"), list) or len(retained["rows"]) != len(actual_rows) or
                retained.get("geometry_requested") is not True):
            raise ValueError("CovDock original container differs from retained inspection")
        for previous, current in zip(retained["rows"], actual_rows):
            for field in ("pose_index", "title", manifest["score_property"], *COVDOCK_INSPECTION_FIELDS):
                if (field in previous) != (field in current) or canonical(previous.get(field)) != canonical(current.get(field)):
                    raise ValueError("CovDock original structure differs from retained inspection: " + field)


def verify_af3_confidence(root, evidence, *, protein_chain, ligand_chain):
    """Recompute scores from witnessed JSON, never grant scientific acceptance.

    Chain roles come from the independently frozen runtime, not the score table.
    This checks confidence provenance only, not pose geometry or binding activity.
    """
    from .af3_results import confidence_metrics

    if evidence.get("engine") != "af3" or evidence.get("scientific_quality_verified") is not False:
        raise ValueError("AF3 confidence verification requires unaccepted artifact evidence")
    files = Files(root)
    hashes = evidence["artifacts"]

    def read(name):
        if name not in hashes:
            raise ValueError("AF3 metric source is absent from the retained artifact witness")
        return files.read(name, hashes[name], parse=True)

    frame = pd.read_parquet(io.BytesIO(read(SPECS["af3"][1])))
    rows = frame.to_dict(orient="records")
    expected = contract(evidence["input_contract"])
    verify_refinement_identities(rows, expected["input_count"], expected["identity_sha256"])
    verify_refinement_identities(evidence["candidates"], expected["input_count"], expected["identity_sha256"])
    sources = {row["input_sdf_index"]: row for row in rows}
    measurements = []
    for candidate in evidence["candidates"]:
        row = sources[candidate["input_sdf_index"]]
        if row["compound_id"] != candidate["compound_id"] or row["engine_status"] != candidate["engine_status"]:
            raise ValueError("AF3 metrics differ from witnessed candidate identity/status")
        if candidate["engine_status"] == "FAILED":
            measurements.append({"input_sdf_index": candidate["input_sdf_index"],
                "compound_id": candidate["compound_id"], "state": "NOT_EVALUATED", "measurements": None})
            continue  # Missing engine output remains an explicit failure.
        structure = candidate["structure"]["path"]
        name = row.get("job_name")
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", name) or Path(structure).name != name + "_model.cif":
            raise ValueError("AF3 metric source differs from witnessed model identity")
        parent = Path(structure).parent
        metrics = confidence_metrics(
            json.loads(read((parent / (name + "_summary_confidences.json")).as_posix())),
            json.loads(read((parent / (name + "_confidences.json")).as_posix())),
            protein_chain, ligand_chain)
        for field, value in {**metrics, "raw_score": metrics["mpae"]}.items():
            recorded = row.get(field)
            if type(recorded) not in (int, float) or not math.isfinite(recorded) or not math.isclose(recorded, value, rel_tol=1e-9, abs_tol=1e-9):
                raise ValueError("AF3 table metric differs from original confidence evidence: " + field)
        if candidate["score"] != row["raw_score"]:
            raise ValueError("AF3 witnessed score differs from its table")
        measurements.append({"input_sdf_index": candidate["input_sdf_index"],
            "compound_id": candidate["compound_id"], "state": "MEASURED", "measurements": metrics})
    for name, checksum in list(files.artifacts.items()):
        files.read(name, checksum)
    return {"version": "af3-confidence-evidence-v1", "candidates": measurements,
            "scientific_quality_verified": False}


def read_refinement_outputs(root, engine, expected_contract, expected_manifest_sha256, *, expected_gate_version=COVDOCK_GATE_VERSION):
    if not isinstance(expected_manifest_sha256, str) or not re.fullmatch(r"[a-f0-9]{64}", expected_manifest_sha256):
        raise ValueError("An independently retained result manifest checksum is required")
    saved = Files(root).json(MANIFEST, expected_manifest_sha256)
    fresh = collect_refinement_outputs(root, engine, expected_contract, expected_gate_version=expected_gate_version)
    if canonical(saved) != canonical(fresh):
        raise ValueError("Refinement files or candidate evidence changed after completion")
    return fresh


def verify_af3_geometry(root, evidence, *, candidate_table, target_config, target_sha256, runtime):
    """Recompute geometry from exact model bytes and the frozen molecular input.

    These measurements cannot complete a run without a separately approved
    quality policy. Never trust SMILES, warhead or distances from the score row.
    """
    import yaml
    from .af3_results import reactive_geometry

    if evidence.get("engine") != "af3" or evidence.get("scientific_quality_verified") is not False:
        raise ValueError("AF3 geometry requires unaccepted artifact evidence")
    expected = contract(evidence["input_contract"])
    files = Files(root)
    hashes = evidence["artifacts"]

    def frozen(path, checksum):
        path = Path(path)
        if not path.is_absolute() or path.resolve(strict=True) != path or path.is_symlink():
            raise ValueError("Frozen geometry input path cannot be redirected")
        return Files(path.parent).read(path.name, checksum, parse=True)

    def read(name):
        if name not in hashes:
            raise ValueError("Geometry source is absent from retained artifact witness")
        return files.read(name, hashes[name], parse=True)

    contents = frozen(candidate_table, expected["input_sha256"])
    from .refinement_input import read_candidate_bytes
    source = read_candidate_bytes(candidate_table, contents).to_dict(orient="records")
    verify_refinement_identities(source, expected["input_count"], expected["identity_sha256"])
    verify_refinement_identities(evidence["candidates"], expected["input_count"], expected["identity_sha256"])
    config = yaml.safe_load(frozen(target_config, target_sha256))
    if (config.get("protein_chain") != runtime["protein_chain"] or config.get("ligand_chain", "L") != runtime["ligand_chain"] or
        config.get("cysteine_sequence_index") != runtime["cysteine_sequence_index"] or
        config.get("residue_number") != runtime["source_residue_number"]):
        raise ValueError("AF3 geometry target differs from approved runtime")
    scores = pd.read_parquet(io.BytesIO(read(SPECS["af3"][1]))).to_dict(orient="records")
    verify_refinement_identities(scores, expected["input_count"], expected["identity_sha256"])
    inputs = {row["input_sdf_index"]: row for row in source}
    rows = {row["input_sdf_index"]: row for row in scores}
    result = []
    for candidate in evidence["candidates"]:
        index = candidate["input_sdf_index"]
        row, original = rows[index], inputs[index]
        if original["compound_id"] != candidate["compound_id"] or row["compound_id"] != candidate["compound_id"] or row["engine_status"] != candidate["engine_status"]:
            raise ValueError("AF3 geometry candidate identity/status differs")
        if candidate["engine_status"] == "FAILED":
            result.append({"input_sdf_index": index, "compound_id": candidate["compound_id"], "state": "NOT_EVALUATED", "measurements": None})
            continue
        if not isinstance(original.get("standardized_smiles"), str) or not isinstance(original.get("warhead_class"), str):
            raise ValueError("Frozen molecular input lacks SMILES or declared warhead")
        model = candidate["structure"]["path"]
        values = reactive_geometry(read(model).decode("utf-8"), original["standardized_smiles"], config, warhead_class=original["warhead_class"])
        for field in ("source_residue", "af3_sequence_residue", "nucleophilic_atom", "electrophilic_atom"):
            if row.get(field) != values[field]:
                raise ValueError("AF3 score-table geometry identity differs from original structure: " + field)
        value = row.get("reactive_distance_angstrom")
        if type(value) not in (int, float) or not math.isfinite(value) or not math.isclose(value, values["reactive_distance_angstrom"], rel_tol=1e-6, abs_tol=1e-6):
            raise ValueError("AF3 score-table distance differs from original structure")
        result.append({"input_sdf_index": index, "compound_id": candidate["compound_id"], "state": "MEASURED", "measurements": values})
    for name, checksum in list(files.artifacts.items()):
        files.read(name, checksum)
    frozen(candidate_table, expected["input_sha256"])
    frozen(target_config, target_sha256)
    return {"version": "af3-geometry-evidence-v1", "input_sha256": expected["input_sha256"],
        "target_sha256": target_sha256, "candidates": result, "scientific_quality_verified": False}
