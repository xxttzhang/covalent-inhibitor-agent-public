"""Freeze workflow-owned input roles for the existing explicit-input worker.

No docking, task submission, resource approval or biological acceptance occurs
here. Operator-registered paths stay behind the workflow boundary.
"""
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

import yaml

from covscreen.docking_reference import validate_reference
from covscreen.jobs import sha256_file
from covscreen.layout import enable_library_first
from covscreen.receptor_evidence import regular
from covscreen.receptor_input import load_receptor_preparation_input
from . import library_subset, receptor_handoff

PROFILE = "egfr-c797-acrylamide-research-pilot-v1"
SCHEMA = "library-screening-inputs-v1"


def encoded(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False) + "\n").encode()


def registered_file(value, *, large=False):
    path = Path(value)
    if not path.is_absolute() or path.resolve(strict=True) != path:
        raise ValueError("Registered screening input must be an absolute file without symlinks")
    if large:
        if not path.is_file():
            raise ValueError("Registered library is not a regular file")
        return path, sha256_file(path)
    return regular(Path(path.anchor), path)


def profiles(state):
    supported = state["target"] == {"target": "EGFR", "taxon": 9606} and state["site_selection"]["site"] == "C797"
    return {"profiles": [{"id": PROFILE, "mode": "research_pilot_v1", "source_records": 20000,
        "maximum_shortlist": 5, "selection": "frozen-file-order-prefix", "reaction": "Michael Addition",
        "warhead": "acrylamide", "target_site_supported": supported,
        "input_readiness_verified": False, "full_library_screening": False}],
        "calculation_submitted": False}


def sources(root, state, profile):
    if profile != PROFILE or not profiles(state)["profiles"][0]["target_site_supported"]:
        raise ValueError("Unsupported screening profile/target; no fallback to EGFR or a smaller mode")
    evidence = receptor_handoff.load_record(root, state)
    identity = evidence["result"]["identity"]
    expected = {"entry_id": "1M17", "uniprot": "P00533", "canonical_cys": 797,
                "author_chain": "A", "author_residue": "773"}
    if identity != expected:
        raise ValueError("The installed screening adapter does not support this receptor identity")
    loaded = load_receptor_preparation_input(root, state["receptor_input"]["id"], state["receptor_input"]["sha256"])
    deposited, deposited_sha = regular(root, loaded["acquisition_directory"] / "structure.pdb")
    receptor = evidence["result"]["receptor"]
    prepared, prepared_sha = regular(root, receptor["path"], receptor["sha256"])
    config, _ = regular(root, root / "configs/local.inspire.yaml")
    settings = yaml.safe_load(config.read_text())
    library, library_sha = registered_file(settings["covindb"]["virtual_screening_library"], large=True)
    registry, registry_sha = regular(root, root / "configs/library_docking_references.json")
    registered = json.loads(registry.read_text())
    if registered.get("schema") != "library-docking-references-v1" or not isinstance(registered.get("references"), list):
        raise ValueError("Unsupported docking-reference registry")
    matches = [item for item in registered["references"] if item.get("pdb_sha256") == deposited_sha]
    if len(matches) != 1:
        raise ValueError("Exactly one registered reference for the workflow's deposited structure is required")
    asset = matches[0]
    registered_file(asset["pdb"])
    reference, reference_sha = registered_file(asset["reference_ligand"])
    model = evidence["result"]["context"]["deposited_environment"]["model"]
    verified = validate_reference(asset, {"input_sha256": deposited_sha, "model": model},
        {"author_chain": identity["author_chain"], "author_residue_id": identity["author_residue"], "canonical_position": 797})
    recipe, recipe_sha = registered_file(str(enable_library_first() / "covindb-configs/EGFR_C797_mvp_v1.yaml"))
    science = yaml.safe_load(recipe.read_text())
    if (science.get("target_id") != "EGFR" or science.get("site_id") != "EGFR_C797"
        or science.get("reaction_type") != "Michael Addition" or science.get("warhead_class") != "acrylamide"
        or science.get("protein_chain") != "A" or science.get("residue_number") != 797
        or science.get("structure", {}).get("carsidock_covres") != verified["covres"]):
        raise ValueError("Registered science configuration differs from the supported target/reaction")
    binding = {"workflow_id": state["workflow_id"], "profile": profile,
        "receptor_evidence": state["screening_receptor"], "library": {"path": str(library), "sha256": library_sha},
        "prepared_receptor_sha256": prepared_sha, "reference_sha256": reference_sha,
        "deposited_sha256": deposited_sha, "reference_registry_sha256": registry_sha,
        "science_config_sha256": recipe_sha,
        "implementation": {"screening_inputs": sha256_file(Path(__file__)), "library_subset": sha256_file(Path(library_subset.__file__))}}
    return {"binding": binding, "library": library, "prepared": prepared, "reference": reference,
        "registry": registry, "recipe": recipe,
        "science": science, "reference_evidence": verified, "receptor_evidence": evidence}


def folder(root, identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r"screening-inputs-[a-f0-9]{32}", identifier):
        raise ValueError("Invalid workflow screening input identity")
    result = root / "state/screening_inputs" / identifier[17:]
    if any(p.is_symlink() for p in (root / "state", result.parent, result)):
        raise ValueError("Screening input directory cannot be a symlink")
    return result


def present(identifier, checksum, record):
    return {"artifact_id": identifier, "manifest_sha256": checksum, "profile": record["binding"]["profile"],
        "input_roles": {name: {k: v for k, v in value.items() if k not in {"path", "source", "manifest"}}
                        for name, value in record["research_inputs"].items() if isinstance(value, dict)},
        "source_library_sha256": record["binding"]["library"]["sha256"],
        "reference": {k: v for k, v in record["reference_evidence"].items() if k not in {"pdb", "reference_ligand"}},
        "full_library_screening": False, "scientific_review_required": record["scientific_review_required"],
        "calculation_submitted": False, "screening_authorized": False}


def load_record(root, state):
    root = Path(root).resolve()
    ref = state.get("screening_inputs")
    if not ref:
        raise ValueError("No screening inputs are bound to this workflow")
    directory = folder(root, ref["id"])
    path, _ = regular(root, directory / "screening-inputs.json", ref["sha256"])
    record = json.loads(path.read_text())
    binding = record.get("binding", {})
    receptor_handoff.load_record(root, state)
    if (record.get("schema") != SCHEMA or binding.get("workflow_id") != state["workflow_id"]
        or binding.get("receptor_evidence") != state["screening_receptor"]
        or ref["id"] != "screening-inputs-" + hashlib.sha256(encoded(binding)).hexdigest()[:32]):
        raise ValueError("Screening inputs belong to another workflow or receptor evidence")
    for name, checksum in record["inventory"].items():
        regular(root, directory / name, checksum)
    roles = record["research_inputs"]
    for name in ("library", "receptor", "science_config", "reference"):
        item = roles[name]
        relative = Path(item["path"]).relative_to(directory).as_posix()
        if record["inventory"].get(relative) != item["sha256"]:
            raise ValueError("Screening role differs from its frozen inventory")
    return record


def read(root, state):
    record = load_record(root, state)
    return present(state["screening_inputs"]["id"], state["screening_inputs"]["sha256"], record)


def freeze(root, state, profile):
    root = Path(root).resolve()
    source = sources(root, state, profile)
    binding = source["binding"]
    identifier = "screening-inputs-" + hashlib.sha256(encoded(binding)).hexdigest()[:32]
    destination = folder(root, identifier)
    destination.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".preparing-", dir=destination.parent))
    try:
        library_subset.freeze(source["library"], staging / "library", 20000, binding["library"]["sha256"])
        for name, original, expected in [("receptor.pdb", source["prepared"], binding["prepared_receptor_sha256"]),
                                         ("reference.sdf", source["reference"], binding["reference_sha256"])]:
            shutil.copyfile(original, staging / name)
            regular(root, staging / name, expected)
        science = deepcopy(source["science"])
        science["structure"].update(pdb=str(destination / "receptor.pdb"), reference_ligand=str(destination / "reference.sdf"))
        (staging / "science.yaml").write_text(yaml.safe_dump(science, sort_keys=False))
        inventory = {path.relative_to(staging).as_posix(): sha256_file(path)
                     for path in sorted(staging.rglob("*")) if path.is_file()}
        def role(name):
            return {"path": str(destination / name), "sha256": inventory[name]}
        inputs = {"version": "screening-research-inputs-v1",
            "library": {**role("library/library-prefix.csv"), "records": 20000, "selection": "frozen-file-order-prefix",
                        "source": binding["library"], "manifest": role("library/manifest.json")},
            "receptor": role("receptor.pdb"), "reference": role("reference.sdf"), "science_config": role("science.yaml")}
        record = {"schema": SCHEMA, "binding": binding, "research_inputs": inputs, "inventory": inventory,
            "reference_evidence": source["reference_evidence"],
            "scientific_review_required": ["Isolated-chain construct and removed-component relevance",
                "Reaction and warhead scope; structural checks do not establish cysteine reactivity",
                "Frozen file prefix is not random sampling or whole-library coverage"],
            "screening_authorized": False, "calculation_submitted": False}
        content = encoded(record)
        # Keep the source proof and copied bytes consistent across the full
        # freeze, including the streamed library read.
        receptor_handoff.load_record(root, state)
        regular(root, source["registry"], binding["reference_registry_sha256"])
        _, recipe_sha = registered_file(str(source["recipe"]))
        if recipe_sha != binding["science_config_sha256"]:
            raise ValueError("Scientific configuration changed during input freezing")
        (staging / "screening-inputs.json").write_bytes(content)
        checksum = hashlib.sha256(content).hexdigest()
        if destination.exists():
            regular(root, destination / "screening-inputs.json", checksum)
            for name, expected in inventory.items():
                regular(root, destination / name, expected)
        else:
            os.rename(staging, destination)
        return present(identifier, checksum, record)
    finally:
        if staging.exists():
            shutil.rmtree(staging)
