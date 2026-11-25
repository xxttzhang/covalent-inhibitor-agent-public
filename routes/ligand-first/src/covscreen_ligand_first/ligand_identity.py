"""Bounded public-name resolution. Identity candidates are not campaign approval."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from covscreen.target_identity import target_plan, parse_targets

RCSB = "https://search.rcsb.org/rcsbsearch/v2/query"


def identity_plan(gene: str, taxon: int, cys: int, ligand: str) -> dict:
    target = target_plan(gene, taxon)
    if type(cys) is not int or cys < 1:
        raise ValueError("An explicit positive canonical Cys position is required")
    if not isinstance(ligand, str) or not ligand.strip() or len(ligand) > 200 or any(
        ord(char) < 32 or char in "`;|<>" for char in ligand
    ):
        raise ValueError("Invalid ligand name")
    return {"gene": gene, "taxon": taxon, "cys": cys, "ligand_name": ligand.strip(),
            "uniprot_url": target["uniprot_url"],
            "pubchem_url": "https://pubchem.ncbi.nlm.nih.gov/rest/pug/compound/name/"
                + urllib.parse.quote(ligand.strip(), safe="") + "/property/InChIKey/JSON",
            "max_calls": 3, "requires_compute": False,
            "policy": "Reviewed exact gene and explicit species; full InChIKey CCD match; no salt stripping or fuzzy fallback."}


def target_candidates(payload: dict, plan: dict, total: int | None) -> dict:
    result = parse_targets(payload, plan["gene"], plan["taxon"], total)
    hits = [{"uniprot_id": row["uniprot_id"], "taxon": row["taxon"],
             "sequence_length": row["sequence_length"],
             "canonical_cys_verified": plan["cys"] in row["canonical_cysteines"],
             "sequence_sha256": row["sequence_sha256"]} for row in result["candidates"]]
    return {**{k: result[k] for k in ("server_total", "retrieved", "local_filtered", "complete_search")},
            "candidates": hits,
            "unique_verified_target": result["unique_verified_target"] if len(hits) == 1
                and hits[0]["canonical_cys_verified"] else None}


def compound_candidates(payload: dict) -> list[dict]:
    rows = payload.get("PropertyTable", {}).get("Properties")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 20:
        raise ValueError("PubChem name lookup has no bounded property results")
    seen = set()
    for row in rows:
        cid, key = row.get("CID"), row.get("InChIKey")
        if type(cid) is not int or cid < 1 or cid in seen or not isinstance(key, str) or not re.fullmatch(
            "[A-Z]{14}-[A-Z]{10}-[A-Z]", key
        ):
            raise ValueError("Invalid/duplicate PubChem identity")
        seen.add(cid)
    return [{"cid": row["CID"], "inchikey": row["InChIKey"]} for row in rows]


def ccd_query(inchikey: str) -> dict:
    if not re.fullmatch("[A-Z]{14}-[A-Z]{10}-[A-Z]", inchikey):
        raise ValueError("Invalid InChIKey")
    return {"query": {"type": "terminal", "service": "text_chem", "parameters": {
        "attribute": "rcsb_chem_comp_descriptor.InChIKey", "operator": "exact_match", "value": inchikey}},
        "return_type": "mol_definition", "request_options": {"paginate": {"start": 0, "rows": 20}}}


def resolve_names(gene: str, taxon: int, cys: int, ligand: str, out: Path) -> dict:
    plan = identity_plan(gene, taxon, cys, ligand)
    out.mkdir(parents=True, exist_ok=False)
    evidence = []

    def fetch(name, url, body=None):
        request = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(),
            headers={"Accept": "application/json", "Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(2_000_001)
            if len(raw) > 2_000_000:
                raise ValueError("Database response exceeds size limit")
            headers = dict(response.headers)
            status = response.status
        (out / (name + ".json")).write_bytes(raw)
        evidence.append({"source": name, "url": url, "request_body": body,
                         "sha256": hashlib.sha256(raw).hexdigest(), "http_status": status})
        return (json.loads(raw) if status != 204 else {"total_count": 0, "result_set": []}), headers

    result = {"status": "unresolved", "plan": plan, "campaign_ready": False,
              "accessed_at": datetime.now(timezone.utc).isoformat(), "evidence": evidence,
              "limitations": ["Canonical Cys identity does not verify a PDB chain or mutation state.",
                              "Name/InChIKey mapping is not evidence of noncovalent binding or activity.",
                              "No automatic campaign registration or compute submission."]}
    try:
        protein, headers = fetch("uniprot", plan["uniprot_url"])
        count = next((v for k, v in headers.items() if k.lower() == "x-total-results"), None)
        result["target"] = target_candidates(protein, plan, int(count) if count is not None else None)
        compounds, _ = fetch("pubchem", plan["pubchem_url"])
        result["compounds"] = compound_candidates(compounds)
        if len(result["compounds"]) != 1:
            result["status"] = "ambiguous_compound"
        else:
            ccd, _ = fetch("rcsb_ccd", RCSB, ccd_query(result["compounds"][0]["inchikey"]))
            ids = [row["identifier"] for row in ccd.get("result_set", [])]
            total = ccd.get("total_count")
            if (type(total) is not int or total < len(ids) or len(ids) > 20 or len(set(ids)) != len(ids)
                    or any(not isinstance(i, str) or not re.fullmatch("[A-Z0-9]{1,5}", i) for i in ids)):
                raise ValueError("Invalid, unsupported or inconsistent RCSB CCD results")
            result["ccd"] = {"ids": ids, "server_total": total, "retrieved": len(ids),
                             "complete_search": total == len(ids)}
            if result["target"]["unique_verified_target"] and total == len(ids) == 1:
                result["status"] = "identifiers_resolved_structure_validation_required"
                result["structure_discovery_inputs"] = {
                    "uniprot_id": result["target"]["unique_verified_target"], "component_id": ids[0]}
            else:
                result["status"] = "ambiguous_or_unmatched_identity"
    except (ValueError, KeyError, TypeError, urllib.error.URLError) as exc:
        result["status"] = "lookup_failed"
        result["error"] = str(exc)
    (out / "identity_resolution.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
