"""Immutable, workflow-owned receptor evidence for the screening boundary."""
import hashlib
import json
from pathlib import Path
import re

from covscreen import receptor_evidence
from covscreen.jobs import ArtifactRegistry

SCHEMA = "library-screening-receptor-evidence-v1"


def present(identifier, checksum, record):
    result = record["result"]
    summary = {key: value for key, value in result.items() if key not in {"inventory", "sources", "receptor"}}
    summary["receptor"] = {"role": "prepared_isolated_protein", "sha256": result["receptor"]["sha256"]}
    summary["verification"] = {"input_files": len(result["inventory"]), "audit_implementations": len(result["sources"]),
        "evidence_scope": "replayed_structure_quality_and_descriptive_context", "full_evidence_in_bound_artifact": True}
    return {"artifact_id": identifier, "manifest_sha256": checksum, "result": summary, "calculation_submitted": False}


def arguments(state):
    return {"preparation_run_id": state["preparation"]["run_id"],
        "receptor_input_artifact_id": state["receptor_input"]["id"],
        "acquisition_sha256": state["receptor_input"]["sha256"],
        "prepared_sha256": state["preparation"]["manifest_sha256"],
        "context_artifact_id": state["receptor_context"]["id"], "context_sha256": state["receptor_context"]["sha256"]}


def freeze(root, state):
    root = Path(root).resolve()
    parents = arguments(state)
    evidence = receptor_evidence.inputs(root, **parents)
    result = receptor_evidence.replay(root, evidence)
    record = {"schema": SCHEMA, "workflow_id": state["workflow_id"], "parents": parents, "result": result}
    record = json.loads(json.dumps(record, sort_keys=True, allow_nan=False))
    encoded = (json.dumps(record, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode()
    checksum = hashlib.sha256(encoded).hexdigest()
    identifier = "screening-receptor-" + checksum[:32]
    folder = root / "state/screening_receptors" / checksum[:32]
    if any(path.is_symlink() for path in (root / "state", folder.parent, folder)):
        raise ValueError("Screening receptor evidence directory cannot be a symbolic link")
    folder.mkdir(parents=True, exist_ok=True)
    manifest = folder / "screening-receptor.json"
    # A crash before this single atomic commit never leaves an adoptable record.
    if manifest.exists():
        receptor_evidence.regular(root, manifest, checksum)
    else:
        ArtifactRegistry._write_json(manifest, record)
    return present(identifier, checksum, record)


def load_record(root, state):
    root = Path(root).resolve()
    ref = state.get("screening_receptor")
    if not ref or not re.fullmatch(r"screening-receptor-[a-f0-9]{32}", ref["id"]):
        raise ValueError("No verified screening receptor is bound to this workflow")
    manifest = root / "state/screening_receptors" / ref["id"][19:] / "screening-receptor.json"
    path, digest = receptor_evidence.regular(root, manifest, ref["sha256"])
    if ref["id"] != "screening-receptor-" + digest[:32]:
        raise ValueError("Screening receptor evidence content identity changed")
    record = json.loads(path.read_text())
    if record.get("schema") != SCHEMA or record.get("workflow_id") != state["workflow_id"] or record.get("parents") != arguments(state):
        raise ValueError("Screening receptor evidence belongs to another workflow or parent")
    current = receptor_evidence.inputs(root, **record["parents"])
    if record["result"]["inventory"] != current["inventory"] or record["result"]["sources"] != current["sources"]:
        raise ValueError("Screening receptor inputs or audit implementation changed; review again")
    return record


def read(root, state):
    record = load_record(root, state)
    ref = state["screening_receptor"]
    return present(ref["id"], ref["sha256"], record)
