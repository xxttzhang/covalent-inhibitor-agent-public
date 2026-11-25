"""Bounded, auditable RCSB entry discovery; not executable campaign approval."""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ENDPOINT = "https://search.rcsb.org/rcsbsearch/v2/query"


def build_target_query(uniprot: str, limit: int) -> dict:
    if not isinstance(uniprot, str) or not re.fullmatch(r"[A-Z0-9]{6,10}(?:-\d+)?", uniprot):
        raise ValueError("Use an explicit UniProt accession, not a free-text target")
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Discovery limit must be an integer between 1 and 100")

    def terminal(attribute, value, operator="exact_match"):
        return {"type": "terminal", "service": "text", "parameters": {
            "attribute": attribute, "operator": operator, "value": value}}

    prefix = "rcsb_polymer_entity_container_identifiers.reference_sequence_identifiers."
    return {"query": {"type": "group", "logical_operator": "and", "nodes": [
        terminal(prefix + "database_accession", uniprot),
        terminal(prefix + "database_name", "UniProt"),
    ]}, "return_type": "entry", "request_options": {"paginate": {"start": 0, "rows": limit}}}


def build_query(uniprot: str, component: str, limit: int) -> dict:
    query = build_target_query(uniprot, limit)
    if not isinstance(component, str) or not re.fullmatch(r"[A-Z0-9]{1,5}", component):
        raise ValueError("Use an explicit CCD component identifier")
    query['query']['nodes'].append({'type': 'terminal', 'service': 'text', 'parameters': {
        'attribute': 'rcsb_nonpolymer_entity_container_identifiers.nonpolymer_comp_id',
        'operator': 'in', 'value': [component]}})
    return query


def summarize(response: dict, query: dict) -> dict:
    if not isinstance(response, dict) or "total_count" not in response:
        raise ValueError("RCSB response lacks an explicit total_count")
    rows = response.get("result_set", [])
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("Malformed RCSB result_set")
    identifiers = [row.get("identifier") for row in rows]
    if any(not isinstance(value, str) or not re.fullmatch(
        r"(?:[0-9][A-Za-z0-9]{3}|pdb_[A-Za-z0-9]{8})", value
    ) for value in identifiers):
        raise ValueError("Invalid RCSB entry identifier")
    total = response["total_count"]
    if type(total) is not int or total < len(identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("RCSB response count/identity inconsistency")
    limit = query.get("request_options", {}).get("paginate", {}).get("rows", 100)
    if len(identifiers) > limit:
        raise ValueError("RCSB response exceeds requested record limit")
    return {"schema_version": "1.0", "status": "candidates_require_structure_validation",
        "accessed_at": datetime.now(timezone.utc).isoformat(), "endpoint": ENDPOINT,
        "query": query, "server_total": total, "retrieved": len(identifiers),
        "pages": 1, "complete_search": total == len(identifiers),
        "entry_ids": sorted(identifiers), "local_filters": [],
        "warnings": ["Entry-level co-occurrence is not proof of direct binding.",
            "Protein chain, organism, mutation state, ligand stereochemistry, noncovalent linkage and Cys mapping remain unverified.",
            "Bounded discovery only; no automatic campaign registration or computation."]}


def discover(uniprot: str, component: str, limit: int, output_dir: Path) -> dict:
    return _discover(build_query(uniprot, component, limit), output_dir)


def discover_target(uniprot: str, limit: int, output_dir: Path) -> dict:
    return _discover(build_target_query(uniprot, limit), output_dir)


def _discover(query: dict, output_dir: Path) -> dict:
    output_dir.mkdir(parents=True, exist_ok=False)
    request = urllib.request.Request(ENDPOINT, data=json.dumps(query).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=30) as reply:
        raw = reply.read(10_000_001)
        if len(raw) > 10_000_000:
            raise ValueError("RCSB response exceeds bounded payload limit")
        response = json.loads(raw) if reply.status != 204 else {"total_count": 0, "result_set": []}
    (output_dir / "rcsb_response.json").write_bytes(raw)
    result = summarize(response, query)
    result["raw_response_sha256"] = hashlib.sha256(raw).hexdigest()
    (output_dir / "structure_discovery.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def load_target_discovery(directory: Path, manifest_sha256: str) -> dict:
    """Verify a target-bound discovery before selecting any returned entry."""
    if not isinstance(manifest_sha256, str) or not re.fullmatch('[a-f0-9]{64}', manifest_sha256):
        raise ValueError('Explicit structure manifest SHA256 required')
    paths = [directory / 'target_structure_discovery.json', directory / 'rcsb_response.json']
    if directory.is_symlink() or any(p.is_symlink() or not p.is_file() for p in paths):
        raise ValueError('Structure discovery requires regular artifact files')
    raw, source = (p.read_bytes() for p in paths)
    if hashlib.sha256(raw).hexdigest() != manifest_sha256:
        raise ValueError('Structure manifest checksum differs')
    result = json.loads(raw)
    if (result.get('schema') != 'target-structure-discovery-v1'
            or result.get('status') != 'structures_discovered_validation_required'
            or result.get('campaign_ready') is not False):
        raise ValueError('Completed discovery evidence is required')
    plan, discovery = result['plan'], result['discovery']
    query = build_target_query(plan['uniprot'], plan['query']['request_options']['paginate']['rows'])
    if plan['query'] != query or discovery['query'] != query:
        raise ValueError('Structure discovery query differs from plan')
    if hashlib.sha256(source).hexdigest() != discovery['raw_response_sha256']:
        raise ValueError('RCSB source checksum differs')
    checked = summarize(json.loads(source) if source else {'total_count': 0, 'result_set': []}, query)
    for key in ('entry_ids', 'server_total', 'retrieved', 'pages', 'complete_search', 'endpoint', 'status'):
        if checked[key] != discovery[key]:
            raise ValueError('Structure discovery differs from raw source: ' + key)
    return result
