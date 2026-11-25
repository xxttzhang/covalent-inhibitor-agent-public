"""Upstream tool contracts and thin adapters; no scientific execution logic is duplicated."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .annotations import EXECUTION
from covscreen.target_hypothesis import Hypothesis, hypothesis_tool


TOOLS = [
    {
        "name": "covalent_target_hypothesis",
        "description": "Scientific reasoning and target hypothesis agent. collect retrieves UniProt, PDB, ChEMBL and literature evidence for an explicit target/species/Cys site (up to 10 minutes); read returns sealed evidence; finalize validates the current platform model's evidence-linked hypothesis and compiles the existing Library-first, Ligand-first or de-novo route plan when inputs are registered. De-novo requires warhead_selection: selected_warhead, statement explaining site suitability/mechanism/reactivity tradeoffs, evidence_ids, interpretation, and evidence-linked alternatives or no_alternatives_reason when none are supported. status=unresolved with pending_reason, selected_warhead=null, execution_branch_id=null and warhead_class=[] saves a non-executable hypothesis. Evidence-linked branches have branch_id and warhead; execution_branch_id explicitly selects primary or one branch. warhead_class must match only that branch and the registered generation request; other branches and alternatives do not authorize generation. Citation existence is not scientific verification. For Ligand-first, actively compare noncovalent parents: supplement refetches PDB:/ChEMBL:/PMID: source IDs into a NEW immutable evidence_id; search_parent_literature retrieves mechanism abstracts: prefer molecule_id (without query) to search the verified parent name, target and reversible/noncovalent terms within ABSTRACT; alternatively provide an explicit bounded Europe PMC query; measure_parent verifies full chemical identity, canonical Cys mapping and coordinate distances. Use the latest returned evidence_id serially. Concurrent additive operations are queued per evidence lineage and automatically append to the latest committed version; automatic_append_rebase reports this explicitly. finalize never rebases and rejects stale versions. read (bounded 30-second wait while running) and read_parent_evidence return compact facts; read_sources reads 1-3 complete metadata/abstract or geometry records by source_ids without file paths; it does not retrieve article full text. For mechanism evidence prefer Europe PMC ABSTRACT:(...) queries if broad results do not support the claim. read_parent_evidence returns compact facts and ParentComparison schema. finalize requires structured parent_comparison and two measured evidence-supported parents; unknown geometry never means infeasible. Never submits computation. For disease input first resolve an explicit target. Save returned report with artifact.save_file for platform Results.",
        "inputSchema": {"type": "object", "$defs": Hypothesis.model_json_schema()["$defs"], "properties": {
            "action": {"type": "string", "enum": ["collect", "read", "supplement", "search_parent_literature", "measure_parent", "read_parent_evidence", "read_sources", "finalize"]},
            "target": {"type": "string"}, "site": {"type": "string", "pattern": "^C[1-9][0-9]*$"},
            "taxon": {"type": "integer", "minimum": 1},
            "evidence_id": {"type": "string", "pattern": "^hyp-[a-f0-9]{24}$"},
            "hypothesis": Hypothesis.model_json_schema(),
            "source_ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 8},
            "query": {"type": "string", "maxLength": 300},
            "molecule_id": {"type": "string", "pattern": "^ChEMBL:CHEMBL[0-9]+$"},
            "pdb_id": {"type": "string", "pattern": "^[0-9][A-Z0-9]{3}$"},
            "component": {"type": "string", "pattern": "^[A-Z0-9]{1,3}$"},
            "label_chain": {"type": "string"}, "ligand_chain": {"type": "string", "description": "Omit BOTH ligand_chain and ligand_residue for unique-instance auto-selection. Never guess; on ambiguity select an actual returned pair."},
            "ligand_residue": {"type": "string"}, "model": {"type": "integer", "minimum": 1},
            "altloc": {"type": "string"},
            "scope": {"type": "string", "enum": ["pilot", "full"]}},
            "required": ["action"], "additionalProperties": False},
        "annotations": EXECUTION,
    },
]

TOOLS.append({
    "name": "covalent_ligand_design",
    "description": "Bounded Ligand-first design preparation and local RDKit graph generation from a finalized, sealed parent comparison. prepare requires evidence_id, proposal_id and proposal_sha256 returned by finalize; it revalidates evidence, verifies target identity and prepares the exact selected structure. generate requires design_id; it applies a fixed maximum of two sites/six candidates and only explicitly proposed acrylamide, re-reads saved SDF for chemistry review, and retains failures. plan_pose exports an identity-bound maximum-three-candidate pilot request with fixed resource limits; it does not register or submit compute. read inspects sealed artifacts. No GPU/docking job is submitted. NEEDS_POSE_EVALUATION and POSE_PLAN_READY are not completed computational acceptance. Never invent a proposal ID/hash or substitute a historical campaign.",
    "inputSchema": {"type": "object", "properties": {
        "action": {"type": "string", "enum": ["prepare", "generate", "plan_pose", "deliver", "read"]},
        "evidence_id": {"type": "string", "pattern": "^hyp-[a-f0-9]{24}$"},
        "proposal_id": {"type": "string", "pattern": "^proposal-[a-f0-9]{12}$"},
        "proposal_sha256": {"type": "string", "pattern": "^[a-f0-9]{64}$"},
        "design_id": {"type": "string", "pattern": "^design-[a-f0-9]{24}$"}},
        "required": ["action"], "additionalProperties": False},
    "annotations": EXECUTION,
})


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    def design():
        from covscreen.ligand_design import design_tool
        return design_tool(root, **arguments)
    return {
        'covalent_target_hypothesis': lambda: hypothesis_tool(root, **arguments),
        'covalent_ligand_design': design,
    }
