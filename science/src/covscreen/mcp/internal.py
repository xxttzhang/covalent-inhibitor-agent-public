"""Internal tool contracts and thin adapters; no scientific execution logic is duplicated."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .annotations import READ_ONLY, EXECUTION
from covscreen.tool_api import (
    submit_job_tool,
)


TOOLS = [
    {
        "name": "covalent_prepare_execution",
        "description": "Read-only native approval protocol: compile an exact execution plan without submitting it. Does not approve or execute anything.",
        "inputSchema": {"type": "object", "properties": {
            "tool": {"type": "string"}, "arguments": {"type": "object"}},
            "required": ["tool", "arguments"], "additionalProperties": False},
        "annotations": READ_ONLY,
    },
    {
        "name": "covalent_submit_job",
        "description": (
            "Compile or submit a registered receptor-preparation/adduct-parameterization/OpenMM-system-preparation/CovalentInDB/RDKit/CarsiDock/RTMScore/recall-pool/AlphaFold3/CovDock-LO/Pareto/PocketXMol/REINVENT4/DiffLinker job. Execute mode requires "
            "human_approved=true, an approval note, and an explicit resource budget. Arbitrary commands are not accepted."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "project_id": {"type": "string"},
                "route": {"type": "string", "enum": ["library-first", "ligand-first", "de-novo"]},
                "engine": {
                    "type": "string",
                    "enum": [
                        "library-first-receptor-preparation", "library-first-adduct-parameterization", "library-first-openmm-system-preparation", "library-first-openmm-short-qc", "library-first-openmm-equilibration", "library-first-openmm-production", "library-first-trajectory-audit", "library-first-structural-analysis", "library-first-md-report", "library-first-md-candidate-delivery", "library-first-md-adduct-admission", "covindb-prescreen", "rdkit-warhead-enumeration", "ligand-first-structure-intake", "ligand-first-covalentization", "carsidock-cov", "rtmscore",
                        "carsidock-pool-selection",
                        "alphafold3-mpae",
                        "schrodinger-covdock-lo",
                        "ligand-first-evaluation",
                        "ligand-first-pareto",
                        "library-first-pareto",
                        "library-first-evaluation",
                        "pocketxmol", "reinvent4", "difflinker"
                    ],
                },
                "parameters": {"type": "object"},
                "resource_budget": {
                    "type": "object",
                    "properties": {
                        "max_runtime_minutes": {"type": "integer", "minimum": 1, "maximum": 10080},
                        "gpu_count": {"type": "integer", "minimum": 0, "maximum": 8, "default": 0},
                        "cpu_count": {"type": "integer", "minimum": 1, "maximum": 128, "default": 1},
                        "memory_gb": {"type": "number", "exclusiveMinimum": 0, "maximum": 1024, "default": 4},
                        "max_candidates": {"type": "integer", "minimum": 1, "maximum": 1000000, "default": 1},
                    },
                    "required": ["max_runtime_minutes"],
                    "additionalProperties": False,
                },
                "execution_mode": {"type": "string", "enum": ["plan", "execute"], "default": "plan"},
                "human_approved": {"type": "boolean", "default": False},
                "approval_note": {"type": ["string", "null"]},
                "parent_run_id": {"type": ["string", "null"],
                    "description": "Immutable retry parent; validated against the bounded recovery policy."},
                "attempt": {"type": "integer", "minimum": 1, "maximum": 100, "default": 1},
            },
            "required": ["request_id", "project_id", "route", "engine", "parameters", "resource_budget"],
            "additionalProperties": False,
        },
        "annotations": EXECUTION,
    },
]


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        'covalent_prepare_execution': lambda: prepare(root, arguments),
        'covalent_submit_job': lambda: submit_job_tool(root, arguments),
    }


def prepare(root: Path, arguments: dict[str, Any]) -> dict[str, Any]:
    """Host protocol hook; compiling a scope never authorizes or submits it."""
    from covscreen.approval_scope import prepare as compile_scope
    from covscreen.native_approval import needs_approval
    if set(arguments) != {'tool', 'arguments'} or not isinstance(arguments['arguments'], dict):
        raise ValueError('Invalid preparation request')
    target, values = arguments['tool'], arguments['arguments']
    if not needs_approval(target, values) or '_native_approval' in values or '_native_prepare' in values:
        raise ValueError('Only registered execution calls can be prepared')
    return compile_scope(root, target, values)
