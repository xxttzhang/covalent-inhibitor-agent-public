"""Legacy tool contracts and thin adapters; no scientific execution logic is duplicated."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from .annotations import READ_ONLY
from covscreen.ligand_reuse import read_minimal_reuse


TOOLS = [
    {
        "name": "covalent_ligand_first_reuse_result",
        "description": "Read hash-verified historical EGFR C797 erlotinib two-candidate results. Does not submit calculations or prove online acceptance.",
        "inputSchema": {"type": "object", "properties": {
            "target": {"type": "string"}, "site": {"type": "string"},
            "parent": {"type": "string"}},
            "required": ["target", "site", "parent"], "additionalProperties": False},
        "annotations": READ_ONLY,
    },
]


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    return {
        'covalent_ligand_first_reuse_result': lambda: read_minimal_reuse(
            root, target=arguments["target"], site=arguments["site"], parent=arguments["parent"]),
    }
