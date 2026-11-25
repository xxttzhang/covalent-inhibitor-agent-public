"""Ligand first tool contracts and thin adapters; no scientific execution logic is duplicated."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .annotations import READ_ONLY, EXECUTION
from covscreen.tool_api import (
    enumerate_covalent_candidates_tool,
    evaluate_ligand_first_candidate_tool,
    ligand_first_audit_structure_site_tool,
    ligand_first_campaign_tool,
    ligand_first_chain_tool,
    ligand_first_discover_structures_tool,
    ligand_first_intake_tool,
    ligand_first_prepare_inputs_tool,
    ligand_first_resolve_names_tool,
    plan_attachments_tool,
    plan_ligand_first_tool,
)


TOOLS = [
    {
        "name": "covalent_ligand_first_intake",
        "description": (
            "Resolve a natural-language Ligand-first request containing a target, covalent "
            "cysteine and known noncovalent parent ligand to one frozen executable campaign. "
            "Returns explicit target, ligand, site or input blockers and never launches work."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string", "minLength": 1}},
            "required": ["query"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_ligand_first_resolve_names",
        "description": "Resolve a gene symbol, explicit species taxon, canonical Cys position and ligand name through public UniProt/PubChem/RCSB. Default plan has no network request. Search saves evidence and returns structure-discovery arguments only for unique verified identifiers. Does not approve/register a campaign or establish binding/activity.",
        "inputSchema": {
            "type": "object", "properties": {
                "gene": {"type": "string"}, "taxon": {"type": "integer", "minimum": 1},
                "cys": {"type": "integer", "minimum": 1}, "ligand": {"type": "string"},
                "action": {"type": "string", "enum": ["plan", "search"], "default": "plan"}},
            "required": ["gene", "taxon", "cys", "ligand"], "additionalProperties": False},
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_ligand_first_prepare_inputs",
        "description": "Use frozen identity/discovery evidence and an explicit discovered PDB entry to acquire structure, verify CCD/coordinates and prepare native campaign inputs. Default plan has no network/writes. Prepare does not register runtime configuration or launch docking. Noncovalent-parent assertion is recorded as caller-supplied, not independently verified.",
        "inputSchema": {"type": "object", "properties": {
            "identity_directory": {"type": "string"}, "discovery_directory": {"type": "string"},
            "entry": {"type": "string"}, "noncovalent_parent_assertion": {"type": "string", "minLength": 1},
            "action": {"type": "string", "enum": ["plan", "prepare"], "default": "plan"},
            "label_chain": {"type": "string"}, "ligand_chain": {"type": "string"},
            "ligand_residue": {"type": "string"}, "model": {"type": "integer", "minimum": 1},
            "altloc": {"type": "string"}},
            "required": ["identity_directory", "discovery_directory", "entry", "noncovalent_parent_assertion"],
            "additionalProperties": False},
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_ligand_first_discover_structures",
        "description": "Find candidate RCSB entries for verified UniProt and CCD identifiers, including unregistered target/ligand pairs. Default plan sends no request; search queries public RCSB and saves provenance. Does not validate binding/Cys geometry, register a campaign, or launch docking.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "uniprot": {"type": "string", "pattern": "^[A-Z0-9]{6,10}(-[0-9]+)?$"},
                "component": {"type": "string", "pattern": "^[A-Z0-9]{1,5}$"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                "action": {"type": "string", "enum": ["plan", "search"], "default": "plan"},
            },
            "required": ["uniprot", "component"], "additionalProperties": False,
        },
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_ligand_first_audit_structure_site",
        "description": "Check an existing RCSB Cys mapping JSON against a local PDB and parent ligand. Preserves author insertion codes, requires explicit model/altloc/ligand-instance disambiguation, measures SG distance and reports linkage records. Does not establish chemical identity/noncovalent binding or approve a campaign. Paths must stay within project.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "pdb_path": {"type": "string"}, "mapping_path": {"type": "string"},
                "component": {"type": "string", "pattern": "^[A-Z0-9]{1,3}$"},
                "ligand_chain": {"type": ["string", "null"]},
                "ligand_residue": {"type": ["string", "null"]},
                "model": {"type": ["integer", "null"], "minimum": 1},
                "altloc": {"type": ["string", "null"]},
            },
            "required": ["pdb_path", "mapping_path", "component"], "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_ligand_first_chain",
        "description": (
            "Inspect the parent-linked Ligand-first chain from frozen-source structure intake "
            "through deterministic Covalentizer/RDKit enumeration, CarsiDock, AF3, "
            "reaction-routed CovDock-LO, independent evaluation and Ligand-first Pareto "
            "selection; optionally submit its sole legal next stage."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "config_path": {"type": "string"},
                "execution_mode": {"type": "string", "enum": ["plan", "execute"], "default": "plan"},
                "submit": {"type": "boolean", "default": False},
                "human_approved": {"type": "boolean", "default": False},
                "approval_note": {"type": ["string", "null"]},
            },
            "required": ["config_path"],
            "additionalProperties": False,
        },
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_ligand_first_campaign",
        "description": "Plan, explicitly approve/start, or inspect a bounded eight-stage Ligand-first supervisor. Requires a configured execution host; does not provision GPU/CPU nodes. Stops on failed stages or quality gates.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "config_path": {"type": "string"},
                "action": {"type": "string", "enum": ["plan", "start", "status"], "default": "plan"},
                "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 10080, "default": 240},
                "human_approved": {"type": "boolean", "default": False},
                "approval_note": {"type": ["string", "null"]},
                "approved_plan_sha256": {"type": ["string", "null"]},
                "controller_id": {"type": ["string", "null"]},
            },
            "required": ["config_path"], "additionalProperties": False,
        },
        "annotations": EXECUTION,
    },
    {
        "name": "covalent_plan_attachments",
        "description": (
            "Rank ligand exit vectors, linker ranges, and warhead hypotheses against a "
            "Target Passport. Produces hypotheses and controls; never claims synthesis or validation."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "ligand_id": {"type": "string"},
                "ligand_smiles": {"type": "string"},
                "covalent_mode": {
                    "type": "string",
                    "enum": ["reversible", "irreversible", "either"],
                },
                "pose_source": {
                    "type": "string",
                    "enum": [
                        "experimental",
                        "carsidock",
                        "karmadock",
                        "vina",
                        "glide",
                        "alphafold3",
                        "other",
                        "none",
                    ],
                },
                "pose_reference": {"type": ["string", "null"]},
                "scaffold_core_atom_indices": {"type": "array", "items": {"type": "integer"}},
                "key_interaction_atom_indices": {"type": "array", "items": {"type": "integer"}},
                "attachment_atom_indices": {"type": "array", "items": {"type": "integer"}},
                "protected_atom_indices": {"type": "array", "items": {"type": "integer"}},
                "solvent_exposed_atom_indices": {"type": "array", "items": {"type": "integer"}},
                "distance_to_target_angstrom": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
                "vector_to_target_angle_degrees": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
                "steric_clash_fraction": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
                "interaction_retention_fraction": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
                "synthetic_accessibility_score": {
                    "type": "object",
                    "additionalProperties": {"type": "number"},
                },
                "linker_environment": {
                    "type": "string",
                    "enum": ["polar", "hydrophobic", "mixed", "unknown"],
                },
                "linker_topology_preferences": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["chain", "cyclic"]},
                },
                "warhead_names": {"type": "array", "items": {"type": "string"}},
                "max_plans": {"type": "integer", "minimum": 1, "maximum": 100},
                "route_override_approved": {"type": "boolean", "default": False},
            },
            "required": ["project_id", "ligand_id", "ligand_smiles"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_enumerate_warheads",
        "description": (
            "Deterministically attach curated covalent warheads to nominated scaffold atoms and "
            "validate RDKit valence/SMARTS. This is not a learned generator or reaction predictor."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "scaffold_smiles": {"type": "string"},
                "attachment_atom_indices": {"type": "array", "items": {"type": "integer"}},
                "protected_atom_indices": {"type": "array", "items": {"type": "integer"}},
                "allowed_warheads": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "allowed_linkers": {
                    "type": "array",
                    "items": {
                        "type": "string",
                        "enum": ["direct", "methylene", "ethylene"],
                    },
                    "minItems": 1,
                },
                "max_candidates": {"type": "integer", "minimum": 1, "maximum": 2000, "default": 100},
            },
            "required": ["request_id", "scaffold_smiles", "allowed_warheads"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_plan_ligand_first",
        "description": (
            "Build the documented Ligand-first Covalentization route: parent noncovalent pose, "
            "scaffold modification map, attachment/vector/linker/warhead planning, controlled "
            "generation, CarsiDock-Cov geometry screening, and candidate-pool handoff. Never "
            "launches generation or docking."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_path": {"type": "string"},
                "require_inputs": {"type": "boolean", "default": True},
            },
            "required": ["request_path"],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_evaluate_ligand_first_candidate",
        "description": (
            "Independently and read-only recompute Ligand-first chemistry and pose-ensemble gates "
            "from immutable project artifacts. Returns ADVANCE or HOLD and never trusts the saved "
            "Candidate Passport as its source of truth."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "project_id": {"type": "string"},
                "campaign_path": {"type": "string"},
                "candidate_json_path": {"type": "string"},
                "candidate_sdf_path": {"type": "string"},
                "geometry_json_path": {"type": "string"},
                "raw_poses_path": {"type": "string"},
                "no_dummy_poses_path": {"type": "string"},
                "expected_carsidock_run_id": {"type": "string"},
            },
            "required": [
                "project_id",
                "campaign_path",
                "candidate_json_path",
                "candidate_sdf_path",
                "geometry_json_path",
                "raw_poses_path",
                "no_dummy_poses_path",
                "expected_carsidock_run_id",
            ],
            "additionalProperties": False,
        },
        "annotations": READ_ONLY,
    },
]


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        'covalent_ligand_first_intake': lambda: ligand_first_intake_tool(
            root, arguments["query"]
        ),
        'covalent_ligand_first_resolve_names': lambda: ligand_first_resolve_names_tool(
            root, arguments["gene"], arguments["taxon"], arguments["cys"], arguments["ligand"],
            action=arguments.get("action", "plan")),
        'covalent_ligand_first_prepare_inputs': lambda: ligand_first_prepare_inputs_tool(
            root, arguments["identity_directory"], arguments["discovery_directory"], arguments["entry"],
            arguments["noncovalent_parent_assertion"], action=arguments.get("action", "plan"),
            **{key: arguments.get(key) for key in ("label_chain", "ligand_chain", "ligand_residue", "model", "altloc")}),
        'covalent_ligand_first_discover_structures': lambda: ligand_first_discover_structures_tool(
            root, arguments["uniprot"], arguments["component"],
            limit=arguments.get("limit", 20), action=arguments.get("action", "plan")),
        'covalent_ligand_first_audit_structure_site': lambda: ligand_first_audit_structure_site_tool(
            root, arguments["pdb_path"], arguments["mapping_path"], arguments["component"],
            ligand_chain=arguments.get("ligand_chain"), ligand_residue=arguments.get("ligand_residue"),
            model=arguments.get("model"), altloc=arguments.get("altloc")),
        'covalent_ligand_first_chain': lambda: ligand_first_chain_tool(
            root,
            arguments["config_path"],
            execution_mode=arguments.get("execution_mode", "plan"),
            submit=arguments.get("submit", False),
            human_approved=arguments.get("human_approved", False),
            approval_note=arguments.get("approval_note"),
        ),
        'covalent_ligand_first_campaign': lambda: ligand_first_campaign_tool(
            root, arguments["config_path"], action=arguments.get("action", "plan"),
            max_runtime_minutes=arguments.get("max_runtime_minutes", 240),
            human_approved=arguments.get("human_approved", False),
            approval_note=arguments.get("approval_note"),
            approved_plan_sha256=arguments.get("approved_plan_sha256"),
            controller_id=arguments.get("controller_id")),
        'covalent_plan_attachments': lambda: plan_attachments_tool(root, arguments),
        'covalent_enumerate_warheads': lambda: enumerate_covalent_candidates_tool(arguments),
        'covalent_plan_ligand_first': lambda: plan_ligand_first_tool(
            root,
            arguments["request_path"],
            require_inputs=arguments.get("require_inputs", True),
        ),
        'covalent_evaluate_ligand_first_candidate': lambda: (
            evaluate_ligand_first_candidate_tool(root, arguments)
        ),
    }
