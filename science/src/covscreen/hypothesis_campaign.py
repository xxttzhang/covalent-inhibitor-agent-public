"""Create an isolated route configuration from an operator-registered template."""
import fcntl
import hashlib
import json
from pathlib import Path

import yaml

from .ligand_chain import LigandFirstChainConfig, STAGE_ORDER


def isolate_library(root: Path, evidence_id: str, hypothesis: dict, template: str, target: str, site: str) -> dict:
    """Pin immutable inputs but never inherit a project's previous run registry."""
    from .library_chain import LibraryFirstChainConfig
    root = root.resolve()
    source = (root / template).resolve()
    source.relative_to(root)
    original = source.read_bytes()
    config = LibraryFirstChainConfig.model_validate(yaml.safe_load(original))
    target_path = (root / config.target_config).resolve()
    target_path.relative_to(root)
    target_bytes = target_path.read_bytes()
    identity = yaml.safe_load(target_bytes)
    if identity.get("target_id") != target or identity.get("site_id") != target + "_" + site:
        raise ValueError("Library template target/site does not match hypothesis")
    origin = {"evidence_id": evidence_id, "hypothesis": hypothesis, "template": template,
        "template_sha256": hashlib.sha256(original).hexdigest(),
        "target_sha256": hashlib.sha256(target_bytes).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(origin, sort_keys=True).encode()).hexdigest()
    identifier = "hypothesis-library-" + fingerprint[:24]
    relative = "configs/library_first/" + identifier + ".yaml"
    values = config.model_dump()
    values.update(campaign_id=identifier, project_id=identifier, seed_run_ids={})
    text = yaml.safe_dump(LibraryFirstChainConfig.model_validate(values).model_dump(), sort_keys=False)
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.resolve().relative_to(root)
    record = root / "results/target-hypotheses" / evidence_id / (identifier + ".registration.json")
    record.parent.mkdir(parents=True, exist_ok=True)
    with (record.parent / "registration.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        if destination.exists():
            if destination.read_text() != text:
                raise ValueError("Immutable hypothesis campaign changed: " + relative)
        else:
            with destination.open("x") as stream:
                stream.write(text)
        receipt = {**origin, "fingerprint": fingerprint, "campaign_id": identifier,
            "config_path": relative, "config_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "source_assets_reused": True, "historical_runs_reused": False}
        if not record.exists():
            with record.open("x") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
    return receipt


def isolate(root: Path, evidence_id: str, hypothesis: dict, template: str, *, evidence: dict | None = None) -> dict:
    root = root.resolve()
    source = (root / template).resolve()
    if not source.is_relative_to(root):
        raise ValueError("Campaign template must be inside the scientific root")
    original = source.read_bytes()
    config = LigandFirstChainConfig.model_validate(yaml.safe_load(original))
    campaign_source = (root / config.campaign_path).resolve()
    if not campaign_source.is_relative_to(root):
        raise ValueError("Campaign input must be inside the scientific root")
    campaign_bytes = campaign_source.read_bytes()
    campaign = json.loads(campaign_bytes)
    if evidence is not None:
        target = campaign.get("target", {})
        site = target.get("nucleophile", {})
        parent = campaign.get("parent_pose", {})
        actual = (target.get("symbol"), target.get("uniprot_id"),
            site.get("residue_name"), site.get("uniprot_residue_number"), site.get("atom_name"))
        expected = (evidence["target"], evidence["identity"].get("uniprot_id"),
            "CYS", int(evidence["site"][1:]), "SG")
        if actual != expected or evidence["taxon"] != 9606 or target.get("organism") != "Homo sapiens":
            raise ValueError("Ligand campaign target/site/species does not match evidence")
        if (not hypothesis.get("parent_ligand") or hypothesis.get("parent_binding_mode") != "noncovalent"
            or str(parent.get("ligand_name", "")).casefold() != hypothesis["parent_ligand"].casefold()):
            raise ValueError("Ligand campaign parent does not match explicit noncovalent hypothesis")
        if set(campaign.get("allowed_warheads", [])) != set(hypothesis.get("warhead_class", [])):
            raise ValueError("Ligand campaign warheads do not match hypothesis")
    origin = {"evidence_id": evidence_id, "hypothesis": hypothesis, "template": template,
        "template_sha256": hashlib.sha256(original).hexdigest(),
        "campaign_sha256": hashlib.sha256(campaign_bytes).hexdigest()}
    fingerprint = hashlib.sha256(json.dumps(origin, sort_keys=True).encode()).hexdigest()
    identifier = "hypothesis-" + fingerprint[:24]
    relative = "configs/ligand_first/" + identifier + ".yaml"
    destination = root / relative
    # Keep structural asset paths relative to their original parent. The new
    # JSON gets a new identity; no source structures or existing runs are edited.
    campaign_path = campaign_source.with_name(identifier + ".json")
    campaign["campaign_id"] = identifier
    values = config.model_dump()
    values.update(campaign_id=identifier, project_id=identifier,
        campaign_path=str(campaign_path.relative_to(root)), trace_chain_config=relative,
        stage_order=list(STAGE_ORDER), seed_run_ids={}, target_aliases=[], ligand_aliases=[],
        dispatch_policy=None, dispatch_batch=None)
    validated = LigandFirstChainConfig.model_validate(values)
    config_text = yaml.safe_dump(validated.model_dump(), sort_keys=False)
    campaign_text = json.dumps(campaign, ensure_ascii=False, indent=2)
    record = root / "results/target-hypotheses" / evidence_id / (identifier + ".registration.json")
    record.parent.mkdir(parents=True, exist_ok=True)
    with (record.parent / "registration.lock").open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for filename, text in ((campaign_path, campaign_text), (destination, config_text)):
            if filename.exists():
                if filename.read_text() != text:
                    raise ValueError("Immutable hypothesis campaign changed: " + str(filename))
            else:
                with filename.open("x") as stream:
                    stream.write(text)
        receipt = {**origin, "fingerprint": fingerprint, "campaign_id": identifier,
            "config_path": relative, "config_sha256": hashlib.sha256(config_text.encode()).hexdigest(),
            "input_binding": {
                "target": campaign.get("target", {}),
                "parent_pose": campaign.get("parent_pose", {}),
                "allowed_warheads": campaign.get("allowed_warheads", []),
                "paths": {name: getattr(validated, name) for name in (
                    "campaign_path", "target_config", "pdb", "reference_ligand",
                    "af3_source_data_json", "covdock_receptor")},
                "attachment_residue": validated.attachment_residue,
                "covres": validated.covres,
                "project_id": validated.project_id,
                "stage_order": list(validated.stage_order),
                "seed_run_ids": dict(validated.seed_run_ids),
                "interpretation": "Declared compiler inputs; not proof that input assets exist or computation succeeded",
            },
            "source_assets_reused": True, "historical_runs_reused": False}
        if not record.exists():
            with record.open("x") as stream:
                json.dump(receipt, stream, ensure_ascii=False, indent=2)
    return receipt
