"""Immutable supplemental evidence and measured parent-comparison gates.

Network inputs are identifiers, never model-authored URLs or source text. Source
quotes establish provenance, not entailment; conservative gates retain unknowns.
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import fcntl
import hashlib
import json
import os
import re
import shutil
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from typing import Literal


class BindingEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: str
    quote: str = Field(min_length=20, max_length=2500, description="Exact retrieved excerpt explicitly attributing reversible/noncovalent binding to this parent at the requested target (or irreversible/covalent for exclusion). A title, mere inhibitor label, comparator mechanism, truncated negation or uncertain claim is insufficient. Use read_sources for complete retrieved titles/abstracts (not article full text); never compose a quotation.")


class ParentCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    molecule_id: str = Field(pattern=r"^ChEMBL:CHEMBL[0-9]+$")
    disposition: Literal["priority_for_geometry", "backup", "unresolved", "excluded_covalent"]
    binding_evidence: list[BindingEvidence] = Field(default_factory=list, max_length=5)
    geometry_id: str | None = Field(default=None, description="Exact geometry-... record ID returned by measure_parent/read_parent_evidence, NOT a PDB:... source ID. Read the existing record rather than recreating measurements when this reference is wrong.")
    rationale: str = Field(min_length=1, max_length=2000,
        description="Evidence-bounded research rationale. No invented affinity, conformation, resolution or geometry; machine-verified facts are rendered separately.")
    uncertainties: list[str] = Field(min_length=1, max_length=10)


class ParentComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidates: list[ParentCandidate] = Field(min_length=2, max_length=8)
    selected_molecule_id: str = Field(pattern=r"^ChEMBL:CHEMBL[0-9]+$")
    target_state: Literal["wild_type", "specified_mutant"]
    expected_mutations: list[str] = Field(default_factory=list, max_length=20, description="Exact deposited substitutions for specified_mutant; empty for wild_type. Unknown state cannot pass.")
    scope: str = Field(min_length=1, max_length=500,
        description="Explicit species and mutation/conformation scope; do not claim a universally best parent.")


def fetch(url: str, out: Path, name: str) -> dict:
    request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "CovalentParentEvidence/1.0"})
    with urllib.request.urlopen(request, timeout=40) as response:
        raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError("Source exceeds bounded response size")
    path = out / name
    path.write_bytes(raw)
    return json.loads(raw)


def source_spec(identifier: str) -> tuple[str, str]:
    if re.fullmatch(r"PDB:[0-9][A-Za-z0-9]{3}", identifier):
        return "pdb", "https://data.rcsb.org/rest/v1/core/entry/" + identifier[4:].upper()
    if re.fullmatch(r"ChEMBL:CHEMBL[0-9]+", identifier):
        return "molecule", "https://www.ebi.ac.uk/chembl/api/data/molecule/" + identifier[7:] + ".json"
    if re.fullmatch(r"PMID:[1-9][0-9]*", identifier):
        query = urllib.parse.urlencode({"query": "EXT_ID:" + identifier[5:] + " AND SRC:MED", "format": "json", "resultType": "core"})
        return "literature", "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + query
    raise ValueError("Only exact PDB:, ChEMBL: or PMID: identifiers are accepted")


def normalize(identifier: str, kind: str, raw: dict) -> dict:
    result = {"id": identifier, "kind": kind}
    if kind == "pdb":
        if raw.get("rcsb_id") != identifier[4:].upper():
            raise ValueError("PDB response identity mismatch")
        result.update(title=raw.get("struct", {}).get("title"),
            resolution_angstrom=raw.get("rcsb_entry_info", {}).get("resolution_combined"),
            primary_citation=raw.get("rcsb_primary_citation", {}),
            nonpolymer_entity_ids=raw.get("rcsb_entry_container_identifiers", {}).get("non_polymer_entity_ids", []),
            conformation="not_assessed")
    elif kind == "molecule":
        if raw.get("molecule_chembl_id") != identifier[7:]:
            raise ValueError("ChEMBL response identity mismatch")
        structures = raw.get("molecule_structures") or {}
        result.update(title=raw.get("pref_name"), molecule_type=raw.get("molecule_type"),
            smiles=structures.get("canonical_smiles"), inchikey=structures.get("standard_inchi_key"),
            parent_chembl_id=(raw.get("molecule_hierarchy") or {}).get("parent_chembl_id"),
            synonyms=[x.get("molecule_synonym") for x in raw.get("molecule_synonyms", [])],
            binding_mode="unknown")
        if result["smiles"]:
            from rdkit import Chem
            from rdkit.Chem import Descriptors, Lipinski
            mol = Chem.MolFromSmiles(result["smiles"])
            if mol is None:
                raise ValueError("Invalid deposited molecular structure")
            result["computed_properties"] = {"molecular_weight": Descriptors.MolWt(mol),
                "hbd": Lipinski.NumHDonors(mol), "hba": Lipinski.NumHAcceptors(mol),
                "fragments": len(Chem.GetMolFrags(mol))}
    else:
        rows = raw.get("resultList", {}).get("result", [])
        matches = [x for x in rows if str(x.get("pmid")) == identifier[5:]]
        if len(matches) != 1:
            raise ValueError("Exact PubMed record absent or ambiguous")
        row = matches[0]
        result.update(title=row.get("title"), abstract=re.sub("<[^>]+>", " ", row.get("abstractText", "")),
            publication_types=row.get("pubTypeList", {}).get("pubType", []), doi=row.get("doi"))
    return result


@contextmanager
def version_lock(directory: Path, name: str = ".version.lock"):
    """Serialize proposal publication and successor commitment for one version."""
    with (directory / name).open("a") as stream:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def latest_version(root: Path, evidence_id: str) -> str:
    from .target_hypothesis import folder
    latest, seen = evidence_id, set()
    while (folder(root, latest) / "successor.json").exists():
        if latest in seen:
            raise ValueError("Evidence version cycle detected")
        seen.add(latest)
        latest = json.loads((folder(root, latest) / "successor.json").read_text())["evidence_id"]
    return latest


def require_latest(root: Path, evidence_id: str):
    latest = latest_version(root, evidence_id)
    if latest != evidence_id:
        raise ValueError("STALE_EVIDENCE_VERSION: use latest evidence_id=" + latest +
                         "; read_parent_evidence then retry serially.")


@contextmanager
def append_version(root: Path, evidence_id: str):
    """Queue additive tool operations on a lineage; never rebase a proposal.

    All historical inputs must be committed and integrity checked. Fetching and
    sealing run under a separate lineage lock; short publication locks remain
    independent so finalize and commit have a consistent serialization point.
    """
    from .target_hypothesis import read, folder
    current = read(root, evidence_id)
    if current["state"] != "EVIDENCE_READY":
        raise ValueError("Wait for EVIDENCE_READY before adding evidence")
    ancestor, seen = evidence_id, set()
    while current["evidence"].get("parent_evidence"):
        if ancestor in seen:
            raise ValueError("Evidence ancestry cycle detected")
        seen.add(ancestor)
        ancestor = current["evidence"]["parent_evidence"]["id"]
        current = read(root, ancestor)
    with version_lock(folder(root, ancestor), ".append.lock"):
        latest = latest_version(root, evidence_id)
        committed = read(root, latest)
        if committed["state"] != "EVIDENCE_READY":
            raise ValueError("Latest evidence version is not committed and ready")
        yield latest


def branch(root: Path, evidence_id: str):
    from .target_hypothesis import read, folder, digest
    require_latest(root, evidence_id)
    prior = read(root, evidence_id)
    if prior["state"] != "EVIDENCE_READY":
        raise ValueError("Wait for EVIDENCE_READY before adding evidence")
    identifier = "hyp-" + uuid4().hex[:24]
    destination = folder(root, identifier)
    destination.mkdir(parents=True)
    # Preserve acquisition/measurement files; proposals are not evidence sources.
    old = folder(root, evidence_id)
    manifest = json.loads((old / "manifest.json").read_text())
    for name in manifest["files"]:
        if name in {"evidence.json", "worker.json", "status.json", "request.json"}:
            continue
        source = old / name
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    evidence = copy.deepcopy(prior["evidence"])
    evidence["parent_evidence"] = {"id": evidence_id, "manifest_sha256": digest(old / "manifest.json")}
    evidence.setdefault("supplemental_sources", {})
    evidence.setdefault("parent_geometry", {})
    return identifier, destination, evidence


def seal(identifier: str, out: Path, evidence: dict) -> dict:
    from .target_hypothesis import digest, save
    save(out / "evidence.json", evidence)
    save(out / "manifest.json", {"schema_version": "parent-evidence-v1", "files": {
        str(p.relative_to(out)): digest(p) for p in out.rglob("*") if p.is_file()}})
    save(out / "status.json", {"state": "EVIDENCE_READY"})
    parent = evidence.get("parent_evidence")
    if parent:
        try:
            # Publish a complete pointer atomically, without overwriting a winner.
            pointer = out / "commit-pointer.json"
            save(pointer, {"evidence_id": identifier})
            with version_lock(out.parent / parent["id"]):
                os.link(pointer, out.parent / parent["id"] / "successor.json")
        except FileExistsError:
            latest = json.loads((out.parent / parent["id"] / "successor.json").read_text())["evidence_id"]
            raise ValueError("STALE_EVIDENCE_VERSION: concurrent addition completed first; use evidence_id=" + latest + " and retry this addition. The abandoned branch is not a committed version.")
    return compact(identifier, evidence)


def compact(identifier, evidence):
    from .target_hypothesis import sources
    index = {}
    allowed = {"id", "kind", "title", "name", "url", "mechanism", "smiles", "inchikey",
        "parent_chembl_id", "computed_properties", "ligands", "proteins", "resolution_angstrom", "conformation"}
    for key, source in sources(evidence).items():
        row = {name: value for name, value in source.items() if name in allowed}
        text = source.get("abstract", "")
        if text:
            row["abstract_excerpt"] = text[:400]
            row["complete_abstract_available"] = True
            row["abstract_truncated"] = len(text) > 400
        if "proteins" in row:
            row["proteins"] = [{**{k: v for k, v in protein.items() if k != "organisms"},
                "organisms": [{k: v for k, v in organism.items() if k in {"ncbi_taxonomy_id", "scientific_name"}}
                    for organism in protein.get("organisms", [])]} for protein in row["proteins"]]
        index[key] = row
    result = {"state": "EVIDENCE_READY", "evidence_id": identifier,
        "target": {k: evidence[k] for k in ("target", "site", "taxon")},
        "identity": {k: v for k, v in evidence["identity"].items() if k != "sequence"},
        "source_index": index, "parent_geometry": {key: {**record,
            "coordinate_audit": {k: v for k, v in (record.get("coordinate_audit") or {}).items() if k != "ligand_heavy_atoms"},
            "chemical_identity": {k: v for k, v in (record.get("chemical_identity") or {}).items() if k != "coordinate_atom_order"}}
            for key, record in evidence.get("parent_geometry", {}).items()},
        "parent_comparison_schema": ParentComparison.model_json_schema(),
        "next_action": "Use this NEW evidence_id serially. Use read_sources(source_ids) for complete records by ID; do not parse output files or invent paths. Use supplement for missing sources and search_parent_literature for exact mechanism text. At least two measured evidence-supported parents are required. Unknown geometry never means infeasible; SG distance is not derivatization feasibility."}

    if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) > 30000:
        for row in result["source_index"].values():
            row.pop("abstract_excerpt", None)
            row["details_available_via"] = "read_sources"
    if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) > 30000:
        result["source_index"] = {key: {"id": key, "title": str(row.get("title") or row.get("name") or key)[:180],
            "kind": row.get("kind"), "details_available_via": "read_sources"} for key, row in index.items()}
    if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) > 30000:
        result["parent_geometry"] = {key: {k: record.get(k) for k in
            ("id", "molecule_id", "pdb_id", "status")} | {"details_available_via": "read_sources",
                "details_request": {"action": "read_sources", "source_ids": [key]}}
            for key, record in evidence.get("parent_geometry", {}).items()}
        result["next_action"] += " Geometry details are folded: use each details_request to read its exact geometry ID; repeating read_parent_evidence returns the same summary."
    if len(json.dumps(result, ensure_ascii=False, indent=2).encode()) > 30000:
        result["summary_truncated"] = True
        result["source_count"] = len(index)
        result["geometry_count"] = len(evidence.get("parent_geometry", {}))
        result["source_index"] = dict(list(result["source_index"].items())[-40:])
        result["parent_geometry"] = dict(list(result["parent_geometry"].items())[-20:])
        result["next_action"] += " Summary shows recent entries only; read_sources accepts any exact source or geometry ID from this evidence version."
    return result


def read_sources(root, evidence_id, source_ids):
    from .target_hypothesis import read, sources
    if not 1 <= len(source_ids) <= 3:
        raise ValueError("Read 1-3 exact source IDs per call")
    current = read(root, evidence_id)
    if current["state"] != "EVIDENCE_READY":
        return current
    index = {**sources(current["evidence"]), **current["evidence"].get("parent_geometry", {})}
    if any(key not in index for key in source_ids):
        raise ValueError("Unknown source ID; supplement first, then use its NEW evidence_id")
    return {"state": "EVIDENCE_READY", "evidence_id": evidence_id,
        "sources": {key: {name: value for name, value in index[key].items() if name != "raw_path"}
                    for key in source_ids},
        "content_scope": "Complete retrieved metadata and abstracts, not article full text. Storage files are backend-only; use source IDs, not filesystem paths.",
        "next_action": "If an abstract does not explicitly support the parent mechanism, search_parent_literature with narrower terms or Europe PMC ABSTRACT:(...) syntax, or supplement another discovered PMID. Do not invent a quote or search local files for article text."}


def supplement(root: Path, evidence_id: str, source_ids: list[str]) -> dict:
    if not 1 <= len(source_ids) <= 8 or len(source_ids) != len(set(source_ids)):
        raise ValueError("Supply 1-8 unique source IDs")
    specs = [(x, *source_spec(x)) for x in source_ids]  # validate before writes/network
    identifier, out, evidence = branch(root, evidence_id)
    for source_id, kind, url in specs:
        name = "source_" + source_id.replace(":", "_") + ".json"
        # Existing exact source is immutable; refreshing uses a separately collected evidence root.
        if source_id in evidence["supplemental_sources"]:
            continue
        raw = fetch(url, out, name)
        record = normalize(source_id, kind, raw)
        if kind == "pdb":
            entry = source_id[4:]
            ids = raw.get("rcsb_entry_container_identifiers", {})
            record["ligands"] = []
            record["proteins"] = []
            for entity_id in ids.get("non_polymer_entity_ids", [])[:8]:
                entity = fetch("https://data.rcsb.org/rest/v1/core/nonpolymer_entity/" + entry + "/" + entity_id,
                    out, "source_" + entry + "_nonpolymer_" + entity_id + ".json")
                record["ligands"].append(entity.get("pdbx_entity_nonpoly", {}))
            for entity_id in ids.get("polymer_entity_ids", [])[:8]:
                entity = fetch("https://data.rcsb.org/rest/v1/core/polymer_entity/" + entry + "/" + entity_id,
                    out, "source_" + entry + "_polymer_" + entity_id + ".json")
                entity_ids = entity.get("rcsb_polymer_entity_container_identifiers", {})
                record["proteins"].append({"entity_id": entity_id,
                    "uniprot_ids": entity_ids.get("uniprot_ids", []),
                    "label_chains": entity_ids.get("asym_ids", []),
                    "author_chains": entity_ids.get("auth_asym_ids", []),
                    "mutations": entity.get("rcsb_polymer_entity", {}).get("pdbx_mutation"),
                    "mutation_count": entity.get("entity_poly", {}).get("rcsb_mutation_count"),
                    "organisms": [{k: v for k, v in organism.items() if k in {"ncbi_taxonomy_id", "scientific_name"}}
                        for organism in entity.get("rcsb_entity_source_organism", [])]})
        record.update(url=url, raw_path=name, raw_sha256=hashlib.sha256((out / name).read_bytes()).hexdigest(),
                      retrieved_at=datetime.now(timezone.utc).isoformat())
        evidence["supplemental_sources"][source_id] = record
    return seal(identifier, out, evidence)


def verified_target_gene(evidence: dict) -> str:
    """Use the sealed exact identity without rewriting the requested target."""
    identity = evidence.get("identity") or {}
    target = evidence["target"]
    gene = identity.get("gene")
    if not gene:
        if target == identity.get("uniprot_id"):
            raise ValueError("Accession input requires a verified primary gene identity")
        return target  # Historical gene-input evidence retains its exact name.
    if (not isinstance(gene, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9-]{0,39}", gene)
            or target not in {gene, identity.get("uniprot_id")}
            or identity.get("requested_target") != target
            or identity.get("taxon") != evidence.get("taxon")
            or identity.get("canonical_cys_verified") is not True):
        raise ValueError("Primary gene identity does not match the sealed target/species binding")
    return gene


def parent_mechanism_query(evidence: dict, molecule_id: str) -> str:
    molecule = evidence.get("supplemental_sources", {}).get(molecule_id, {})
    if molecule.get("kind") != "molecule" or not molecule.get("title"):
        raise ValueError("Supplement the exact parent ChEMBL record before a molecule-specific search")
    def literal(value):
        return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return "ABSTRACT:(" + literal(molecule["title"]) + " AND " + literal(verified_target_gene(evidence)) + " AND (reversible OR noncovalent OR \"non-covalent\"))"


def literature(root: Path, evidence_id: str, query: str | None = None, molecule_id: str | None = None) -> dict:
    if molecule_id is not None and query is not None:
        raise ValueError("Provide query OR molecule_id, not both")
    if molecule_id is not None:
        from .target_hypothesis import read
        query = parent_mechanism_query(read(root, evidence_id)["evidence"], molecule_id)
    if not isinstance(query, str) or not 3 <= len(query) <= 300:
        raise ValueError("Supply a bounded query or a verified molecule_id")
    identifier, out, evidence = branch(root, evidence_id)
    params = urllib.parse.urlencode({"query": query, "format": "json", "resultType": "core", "pageSize": 8})
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + params
    name = "literature_" + uuid4().hex[:12] + ".json"
    raw = fetch(url, out, name)
    added = []
    for row in raw.get("resultList", {}).get("result", []):
        if not re.fullmatch(r"[1-9][0-9]*", str(row.get("pmid", ""))):
            continue
        key = "PMID:" + str(row["pmid"])
        if key in evidence["supplemental_sources"]:
            continue
        record = normalize(key, "literature", {"resultList": {"result": [row]}})
        record.update(url="https://pubmed.ncbi.nlm.nih.gov/" + str(row["pmid"]) + "/",
                      raw_path=name, raw_sha256=hashlib.sha256((out / name).read_bytes()).hexdigest())
        evidence["supplemental_sources"][key] = record
        added.append(key)
    result = seal(identifier, out, evidence)
    result.update(query=query, added_sources=added, total_hits=raw.get("hitCount"),
        returned_content="Title and abstract only; search matches can also occur in article full text.",
        search_guidance="For explicit mechanism statements in retrieved content, use Europe PMC ABSTRACT:(...) queries. An unmatched abstract is insufficient evidence even when its article appears in search results.")
    return result


def measure(root: Path, evidence_id: str, molecule_id: str, pdb_id: str, component: str,
            label_chain: str | None = None, ligand_chain: str | None = None,
            ligand_residue: str | None = None, model: int | None = None, altloc: str | None = None) -> dict:
    from .structure_acquisition import acquire_structure
    identifier, out, evidence = branch(root, evidence_id)
    records = evidence["supplemental_sources"]
    molecule = records.get(molecule_id, {})
    if molecule.get("kind") != "molecule" or not molecule.get("inchikey"):
        raise ValueError("Supplement the exact parent ChEMBL record first")
    if molecule.get("computed_properties", {}).get("fragments") != 1:
        raise ValueError("Parent is a salt/multicomponent record; supplement its parent_chembl_id explicitly")
    if records.get("PDB:" + pdb_id, {}).get("kind") != "pdb":
        raise ValueError("Supplement the exact PDB entry first; inspect ligand CCD identity")
    geometry_id = "geometry-" + uuid4().hex[:16]
    result = acquire_structure(pdb_id, evidence["identity"]["uniprot_id"], int(evidence["site"][1:]),
        component, out / geometry_id, label_chain=label_chain, ligand_chain=ligand_chain,
        ligand_residue=ligand_residue, model=model, altloc=altloc, expected_inchikey=molecule["inchikey"])
    record = {"id": geometry_id, "molecule_id": molecule_id, "pdb_id": pdb_id,
        "status": result["status"], "mapping": result.get("mappings"),
        "deposited_proteins": records["PDB:" + pdb_id].get("proteins", []),
        "coordinate_audit": result.get("coordinate_audit"), "chemical_identity": result.get("parent_chemical_identity"),
        "error": result.get("error"), "failures": result.get("mapping_failures"),
        "derivatization_feasibility": "unknown", "execution_ready": False,
        "limitations": ["Minimum SG distance is descriptive, not an exit-vector or reaction-geometry test.",
            "No DFG/alphaC conformation classifier was run; do not infer infeasibility from a conformation label."]}
    from .ligand_parent_pair import assess
    record['parent_cys_assessment'] = assess(
        result.get('coordinate_audit') or {}, result.get('parent_chemical_identity') or {})
    evidence["parent_geometry"][geometry_id] = record
    return seal(identifier, out, evidence)


def norm(text):
    return " ".join(text.casefold().split())


def mechanism_supported(record: dict, quote: str, source: dict, *, covalent=False, target: str | None = None) -> bool:
    """Conservative same-clause attribution, not general semantic entailment.

    The excerpt must name the parent, target and mechanism. Its enclosing source
    clause is checked too, so omitting negation/uncertainty cannot make it pass.
    """
    names = [record.get("title", ""), *(record.get("synonyms") or [])]
    aliases = [norm(x) for x in names if isinstance(x, str) and len(x) >= 4]
    # Exact nomenclature equivalents only; never infer a target from a drug,
    # receptor family, or fuzzy substring. Keep the retrieved quote unchanged.
    target_names = [norm(target)] if target else []
    if target_names == ["egfr"]:
        target_names.append("epidermal growth factor receptor")
    target_pattern = r"(?<!\w)(?:" + "|".join(re.escape(x) for x in target_names) + r")(?!\w)"
    splitter = r"[.!?;]+|\b(?:but|whereas|while|unlike)\b"
    quote = norm(quote)
    clauses = [norm(c).strip(" ,:") for text in (source.get("title") or "", source.get("abstract") or "")
               for c in re.split(splitter, norm(text))]
    for context in clauses:
        if not context:
            continue
        clause = context if context in quote else quote if quote in context else ""
        if not clause:
            continue
        if target and not re.search(target_pattern, clause):
            continue
        if not any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", clause) for alias in aliases):
            continue
        if re.search(r"\b(not|no|neither|without|whether|may|might|could|possibly|unknown|unclear|disproved|incorrect|inaccurate|falsely|unsubstantiated)\b", context):
            continue
        if re.search(r"\bnon[- ](?:reversible|irreversible)\b", context):
            continue
        if target and covalent:
            binding = re.fullmatch(
                r"([a-z][a-z0-9-]{2,40}) is a small molecule covalently binding and inhibiting the "
                r"([a-z0-9 ,]+) receptor tyrosine kinases", context)
            if binding and binding.group(1) in aliases:
                named_targets = re.split(r"\s*,\s*|\s+and\s+", binding.group(2))
                if any(name in named_targets for name in target_names):
                    return True
        # A shared, explicit inhibition predicate applies to both named subjects.
        # Anchor the full source clause so comparators cannot inherit its mechanism.
        if target and not covalent:
            coordinated = re.fullmatch(
                r"([a-z][a-z0-9-]{2,40}) and ([a-z][a-z0-9-]{2,40}) "
                r"(?:are [a-z-]+ derivatives that )?"
                r"(?:selectively and )?reversibly inhibit "
                r"(?:the tyrosine kinase activity of )?(?:the )?"
                + target_pattern, context)
            if coordinated and any(alias in coordinated.groups() for alias in aliases):
                return True
            coordinated_noun = re.fullmatch(
                r"([a-z][a-z0-9-]{2,40}) and ([a-z][a-z0-9-]{2,40}) "
                r"(?:are|were) (?:the (?:first )?two )?"
                r"(?:reversible|non[- ]?covalent) inhibitors of (?:the )?"
                + target_pattern + r"(?: kinase)?", context)
            if coordinated_noun and any(alias in coordinated_noun.groups() for alias in aliases):
                return True
        if target:
            # An explicit target-level "spanning from ... to ..." classification
            # attributes each mechanism to its own named class. Preserve the
            # enclosing sentence's uncertainty/negation checks above; never
            # transfer the opposite class's mechanism to the requested parent.
            contrast = re.search(
                target_pattern
                + r"\s+(?:tkis|inhibitors)\s*,\s*spanning from\s+"
                + r"([^;:.!?]{1,300}?)\s+to\s+([^,;:.!?]{1,300})",
                clause)
            if contrast:
                groups = [part for part in contrast.groups()
                          if any(re.search(r"(?<!\w)" + re.escape(alias) + r"(?!\w)", part)
                                 for alias in aliases)]
                if len(groups) != 1:
                    continue
                context = clause = groups[0]
        positive = bool(re.search(r"\b(reversible|non[- ]?covalent)\b", context))
        negative = bool(re.search(r"\b(irreversible|covalent)\b", re.sub(r"\bnon[- ]?covalent\b", "", context)))
        matches_mode = (negative and not positive) if covalent else (positive and not negative)
        if not matches_mode:
            continue
        mode = r"(?:irreversible|covalent)" if covalent else r"(?:reversible|non[- ]?covalent)"
        # A comparator in the same sentence is not the subject of the mechanism.
        # Accept explicit subject/predicate or a directly named inhibitor phrase.
        for alias in aliases:
            name = r"(?<!\w)" + re.escape(alias) + r"(?!\w)"
            subject = name + r"(?:\s*\([^()]{1,80}\))?\s*(?:,\s*(?:a|an)\s+|(?:is|was|are|were|acts as|functions as)\s+)"
            # Comma-separated adjectives do not introduce a new subject.
            # Enumerate descriptive phrases rather than allowing arbitrary
            # comma spans, which could transfer another drug's mechanism.
            descriptor = (r"(?:oral|orally (?:active|available|administered|bioavailable)|"
                          r"(?:(?:highly|very)\s+)?(?:selective|specific|potent))")
            adjectives = (r"(?:a|an)?\s*" + descriptor
                          + r"(?:\s*,\s*" + descriptor + r"){0,3}\s*,\s*(?:and\s+)?")
            if re.search(subject + adjectives + r"\b" + mode + r"\b", clause):
                return True
            if re.search(subject + r"[^,;:.!?]{0,100}?\b" + mode + r"\b", clause):
                between = re.search(subject + r"([^,;:.!?]{0,100}?)\b" + mode + r"\b", clause).group(1)
                between = re.sub(r"\band (?:functions|acts) as\b", "", between)
                # Coordinated route-of-administration adjectives retain the
                # same subject; do not relax arbitrary "and <other drug>".
                between = re.sub(r"\b(?:oral|orally administered|orally available|orally active|orally bioavailable)\s+and\s*$", "", between)
                # A complete adjective phrase preserves the same subject.
                # Anchor the whole span so another drug/predicate cannot hide
                # before the permitted adjective and inherit its mechanism.
                between = re.sub(r"^\s*(?:a\s+)?(?:highly\s+)?(?:specific|selective)\s+and\s*$", "", between)
                if not re.search(r"\b(and|or|than|compared|versus)\b", between):
                    return True
            prefix = r"\b" + mode + r"\s+(?:[a-z0-9αβγ/-]+\s+){0,6}"
            lead = r"\s*[,:(]?\s*(?:(?:such as|including)\s+)?"
            # Retrieved abstracts also render TKI as TKl; retain the raw quote.
            tki = r"(?:" + target_pattern + r"-)?tk[il]" if target else r"tk[il]"
            if re.search(prefix + r"(?:inhibitors?|" + tki + r"s?)" + lead + name, clause):
                return True
            # A comma after a singular comparator is not a coordinated list.
            # Later members need a plural class and an explicit and/or connector.
            listing = r"(?:[a-z][a-z0-9-]{2,40}\s*,\s*)*[a-z][a-z0-9-]{2,40}\s+(?:and|or)\s+"
            if re.search(prefix + r"(?:inhibitors|" + tki + r"s)" + lead + listing + name, clause):
                return True
    return False


def verify_binding(record: dict, citations: list[BindingEvidence], sources: dict, target: str | None = None) -> list[str]:
    reasons = []
    if not citations:
        return ["noncovalent_mechanism_evidence_missing"]
    supported = False
    for cite in citations:
        source = sources.get(cite.source_id, {})
        text = norm((source.get("title") or "") + " " + (source.get("abstract") or ""))
        quote = norm(cite.quote)
        if source.get("kind") != "literature" or quote not in text:
            reasons.append("exact_retrieved_mechanism_quote_required:" + cite.source_id)
            continue
        if not mechanism_supported(record, cite.quote, source, target=target):
            reasons.append("quote_must_explicitly_link_parent_target_and_reversible_or_noncovalent_binding:" + cite.source_id)
            continue
        supported = True
    if not supported:
        reasons.append("noncovalent_mechanism_not_supported")
    return reasons


def validate_comparison(evidence: dict, comparison: dict | None, parent: str | None) -> dict:
    from .target_hypothesis import sources
    if comparison is None:
        return {"state": "NEEDS_PARENT_COMPARISON", "blockers": ["structured_parent_comparison_required"]}
    value = ParentComparison.model_validate(comparison)
    ids = [row.molecule_id for row in value.candidates]
    if len(set(ids)) != len(ids) or value.selected_molecule_id not in ids:
        raise ValueError("Comparison parents must be unique and include the selected molecule")
    records = sources(evidence)
    geometries = evidence.get("parent_geometry", {})
    rows, blockers = [], []
    if evidence.get("identity", {}).get("canonical_cys_verified") is not True:
        blockers.append("canonical_target_cys_verification_required")
    for item in value.candidates:
        molecule = records.get(item.molecule_id, {})
        errors = []
        if molecule.get("kind") != "molecule" or molecule.get("computed_properties", {}).get("fragments") != 1:
            errors.append("verified_single_component_parent_required")
        if item.disposition in {"priority_for_geometry", "backup"}:
            errors.extend(verify_binding(molecule, item.binding_evidence, records, target=verified_target_gene(evidence)))
            if item.geometry_id not in geometries:
                errors.append("unknown_geometry_record_id:use_read_parent_evidence_then_reference_exact_geometry_id_not_PDB_source_id")
            geometry = geometries.get(item.geometry_id, {})
            if (geometry.get("molecule_id") != item.molecule_id
                or (geometry.get("chemical_identity") or {}).get("status") != "declared_parent_identity_and_heavy_atoms_verified"
                or (geometry.get("coordinate_audit") or {}).get("status") != "coordinate_checks_passed_linkage_review_pending"):
                errors.append("verified_matching_parent_geometry_required")
            audit = geometry.get("coordinate_audit") or {}
            chemical = geometry.get("chemical_identity") or {}
            if (audit.get("uniprot") != evidence["identity"]["uniprot_id"]
                or str(audit.get("uniprot_residue")) != evidence["site"][1:]
                or not molecule.get("inchikey")
                or chemical.get("expected_inchikey") != molecule.get("inchikey")
                or not audit.get("structure_sha256")
                or chemical.get("structure_sha256") != audit.get("structure_sha256")):
                errors.append("geometry_target_site_or_parent_provenance_mismatch")
            proteins = [p for p in geometry.get("deposited_proteins", [])
                if evidence["identity"]["uniprot_id"] in p.get("uniprot_ids", [])
                and audit.get("protein_author_chain") in p.get("author_chains", [])]
            if len(proteins) != 1:
                errors.append("selected_chain_target_state_not_verified")
            else:
                protein = proteins[0]
                taxa = {x.get("ncbi_taxonomy_id") for x in protein.get("organisms", [])}
                if taxa != {evidence["taxon"]}:
                    errors.append("selected_chain_species_mismatch_or_unknown")
                count = protein.get("mutation_count")
                if value.target_state == "wild_type":
                    if type(count) is not int or count != 0 or value.expected_mutations or protein.get("mutations") not in (None, "", "none", "None"):
                        errors.append("wild_type_scope_requires_zero_deposited_mutations")
                else:
                    deposited = {x.strip().upper() for x in (protein.get("mutations") or "").split(",") if x.strip()}
                    expected = {x.strip().upper() for x in value.expected_mutations}
                    if not expected or deposited != expected or count != len(expected):
                        errors.append("specified_mutant_scope_requires_exact_deposited_substitutions")
        else:
            geometry = geometries.get(item.geometry_id, {})
            if item.disposition == "excluded_covalent":
                supported = False
                for cite in item.binding_evidence:
                    source = records.get(cite.source_id, {})
                    text = norm((source.get("title") or "") + " " + (source.get("abstract") or ""))
                    quote = norm(cite.quote)
                    if (source.get("kind") == "literature" and quote in text
                        and mechanism_supported(molecule, cite.quote, source, covalent=True, target=verified_target_gene(evidence))):
                        supported = True
                    else:
                        errors.append("invalid_covalent_exclusion_citation:" + cite.source_id)
                if not supported:
                    errors.append("covalent_exclusion_requires_exact_parent_mechanism_evidence_or_use_unresolved")
        rows.append({"molecule_id": item.molecule_id, "name": molecule.get("title"),
            "computed_properties": molecule.get("computed_properties"), "disposition": item.disposition,
            "binding_evidence": [x.model_dump() for x in item.binding_evidence],
            "geometry": geometry, "rationale": item.rationale, "uncertainties": item.uncertainties,
            "available_geometry_ids": [key for key, record in geometries.items() if record.get("molecule_id") == item.molecule_id],
            "blockers": errors})
        blockers.extend(item.molecule_id + ":" + reason for reason in errors)
    measured = [row for row in rows if not row["blockers"] and row["disposition"] in {"priority_for_geometry", "backup"}]
    if len(measured) < 2:
        blockers.append("at_least_two_evidence_supported_measured_parents_required")
    keys = [records[row["molecule_id"]].get("inchikey") for row in measured]
    if len(set(keys)) != len(keys):
        blockers.append("comparison_requires_chemically_distinct_parents_not_duplicate_database_ids")
    chosen = next(row for row in rows if row["molecule_id"] == value.selected_molecule_id)
    if chosen["disposition"] != "priority_for_geometry":
        blockers.append("selected_parent_must_be_priority_for_geometry")
    if norm(parent or "") != norm(chosen["name"] or ""):
        blockers.append("parent_ligand_must_match_verified_selected_name")
    return {"state": "NEEDS_PARENT_EVIDENCE" if blockers else "PARENT_COMPARISON_REVIEW_READY",
        "scope": value.scope, "target_state": value.target_state, "expected_mutations": value.expected_mutations, "selected_molecule_id": value.selected_molecule_id, "candidates": rows,
        "blockers": blockers, "derivatization_feasibility": "unknown", "execution_ready": False,
        "next_gate": "Independent exit-vector/linker/warhead geometry planning; no automatic docking or campaign execution.",
        "limitations": ["Exact quotes establish attribution, not a general semantic proof; contradictions require review.",
            "Priority is a scoped design hypothesis, not proven optimality, affinity or covalent reactivity."]}
