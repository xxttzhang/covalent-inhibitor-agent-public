"""Acquire one explicit PDB entry and map a canonical Cys before coordinate audit.

No receptor preparation, chemical conversion, campaign approval or compute launch.
"""
import hashlib
import json
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from .rcsb_mapping import map_cysteine
from .structure_site_audit import audit_structure_site
from .target_site_audit import audit_target_site


def acquisition_plan(entry, uniprot, cys, component):
    return _acquisition_plan(entry, uniprot, cys, component, target_only=False)


def target_acquisition_plan(entry, uniprot, cys):
    return _acquisition_plan(entry, uniprot, cys, None, target_only=True)


def _acquisition_plan(entry, uniprot, cys, component, *, target_only):
    identifiers = [(entry, r"[0-9][A-Z0-9]{3}"), (uniprot, r"[A-Z0-9]{6,10}")]
    if not target_only:
        identifiers.append((component, r"[A-Z0-9]{1,3}"))
    for value, pattern in identifiers:
        if not isinstance(value, str) or not re.fullmatch(pattern, value):
            raise ValueError("Use explicit PDB, UniProt and PDB-compatible CCD identifiers")
    if type(cys) is not int or cys < 1:
        raise ValueError("Canonical Cys must be a positive integer")
    return {"entry": entry, "uniprot": uniprot, "cys": cys, "component": component,
            "max_entities": 8, "max_instances": 16, "max_calls": 26,
            "coordinate_format": "PDB", "campaign_registered": False,
            "policy": "Keep downloaded bytes; no chain/ligand/model/altloc guessing."}


def acquire_structure(entry, uniprot, cys, component, out: Path, *, label_chain=None,
                      ligand_chain=None, ligand_residue=None, model=None, altloc=None,
                      expected_inchikey=None):
    return _acquire_structure(entry, uniprot, cys, component, out,
        label_chain=label_chain, ligand_chain=ligand_chain, ligand_residue=ligand_residue,
        model=model, altloc=altloc, expected_inchikey=expected_inchikey, target_only=False)


def acquire_target_structure(entry, uniprot, cys, out: Path, *, label_chain=None, model=None, altloc=None):
    return _acquire_structure(entry, uniprot, cys, None, out,
        label_chain=label_chain, model=model, altloc=altloc, target_only=True)


def _acquire_structure(entry, uniprot, cys, component, out: Path, *, label_chain=None,
                       ligand_chain=None, ligand_residue=None, model=None, altloc=None,
                       expected_inchikey=None, target_only=False):
    plan = _acquisition_plan(entry, uniprot, cys, component, target_only=target_only)
    if expected_inchikey is not None:
        if not isinstance(expected_inchikey, str) or not re.fullmatch(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", expected_inchikey):
            raise ValueError("Invalid expected full InChIKey")
        plan.update(expected_inchikey=expected_inchikey, max_calls=27)
    if label_chain is not None and not re.fullmatch(r"[A-Za-z0-9]{1,8}", label_chain):
        raise ValueError("Invalid label chain")
    out.mkdir(parents=True, exist_ok=False)
    result = {"status": "acquiring", "plan": plan, "sources": [], "mappings": [],
              "mapping_failures": [], "campaign_ready": False,
              "accessed_at": datetime.now(timezone.utc).isoformat()}
    core = "https://data.rcsb.org/rest/v1/core/"

    def fetch(url, name, is_json=True):
        with urllib.request.urlopen(url, timeout=30) as response:
            raw = response.read(20_000_001)
        if len(raw) > 20_000_000:
            raise ValueError("Response exceeds acquisition size limit")
        (out / name).write_bytes(raw)
        result["sources"].append({"url": url, "path": name,
                                  "sha256": hashlib.sha256(raw).hexdigest()})
        return json.loads(raw) if is_json else raw

    try:
        metadata = fetch(core + "entry/" + entry, "entry.json")
        ids = metadata["rcsb_entry_container_identifiers"]
        if ids.get("entry_id") != entry:
            raise ValueError("Entry response identity mismatch")
        entities = ids.get("polymer_entity_ids", [])
        if not isinstance(entities, list) or not 1 <= len(entities) <= plan["max_entities"]:
            raise ValueError("Entry entity count requires narrower selection")
        if len(set(entities)) != len(entities) or any(not re.fullmatch(r"[0-9]+", e) for e in entities):
            raise ValueError("Invalid entity identifiers")
        instance_count = 0
        for entity_id in entities:
            entity = fetch(core + f"polymer_entity/{entry}/{entity_id}", f"entity_{entity_id}.json")
            ei = entity["rcsb_polymer_entity_container_identifiers"]
            if ei.get("entry_id") != entry or ei.get("entity_id") != entity_id:
                raise ValueError("Polymer entity identity mismatch")
            if uniprot not in ei.get("uniprot_ids", []):
                continue
            chains = ei.get("asym_ids", [])
            if not isinstance(chains, list) or len(set(chains)) != len(chains) or any(
                not isinstance(c, str) or not re.fullmatch(r"[A-Za-z0-9]{1,8}", c) for c in chains
            ):
                raise ValueError("Invalid label chain identifiers")
            for chain in chains:
                if label_chain is not None and chain != label_chain:
                    continue
                instance_count += 1
                if instance_count > plan["max_instances"]:
                    raise ValueError("Too many chain instances; select a label chain explicitly")
                instance = fetch(core + f"polymer_entity_instance/{entry}/{chain}", f"chain_{chain}.json")
                if instance["rcsb_polymer_entity_instance_container_identifiers"].get("asym_id") != chain:
                    raise ValueError("Chain response identity mismatch")
                try:
                    result["mappings"].append(map_cysteine(entity, instance, uniprot, cys))
                except ValueError as exc:
                    result["mapping_failures"].append({"chain": chain, "reason": str(exc)})
        fetch(f"https://files.rcsb.org/download/{entry}.pdb", "structure.pdb", False)
        if len(result["mappings"]) != 1:
            result["status"] = "cys_mapping_missing_or_ambiguous"
        else:
            mapping = result["mappings"][0]
            (out / "cys_mapping.json").write_text(json.dumps(mapping, indent=2))
            result["coordinate_audit"] = (audit_target_site(out / "structure.pdb", mapping, model=model, altloc=altloc)
                if target_only else audit_structure_site(out / "structure.pdb", mapping, component,
                    ligand_chain=ligand_chain, ligand_residue=ligand_residue, model=model, altloc=altloc))
            if expected_inchikey is not None:
                from .parent_chemical_identity import audit_parent_chemical_identity
                fetch(f"https://files.rcsb.org/ligands/download/{component}.cif", "component.cif", False)
                result["parent_chemical_identity"] = audit_parent_chemical_identity(
                    out / "component.cif", result["coordinate_audit"], expected_inchikey)
            result["status"] = "acquired_coordinate_audit_completed_review_pending"
    except (ValueError, KeyError, TypeError, urllib.error.URLError, TimeoutError) as exc:
        result["status"] = "acquisition_or_audit_failed"
        result["error"] = str(exc)
    (out / "structure_acquisition.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
