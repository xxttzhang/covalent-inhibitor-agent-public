"""One registry for MCP discovery, dispatch and capability documentation.

Groups describe ownership, not runtime visibility or scientific readiness.
The compatibility server still exposes every registered tool, including the
preparation hook required by the host's native approval protocol.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from . import de_novo, internal, legacy, library_first, library_workflow, ligand_first, ligand_workflow, shared, site_discovery, upstream

SERVER_VERSION = "1.27.23"


_MODULES = (
    ("ligand-workflow", "workflow", ("ligand-first",), ligand_workflow),
    ("library-workflow", "workflow", ("library-first",), library_workflow),
    ("upstream", "upstream", ("library-first", "ligand-first"), upstream),
    ("shared", "shared", ("library-first", "ligand-first", "de-novo"), shared),
    ("site-discovery", "shared", ("library-first", "ligand-first", "de-novo"), site_discovery),
    ("library-first", "route", ("library-first",), library_first),
    ("ligand-first", "route", ("ligand-first", "de-novo"), ligand_first),
    ("de-novo", "route", ("de-novo",), de_novo),
    ("internal", "internal", ("library-first", "ligand-first", "de-novo"), internal),
    ("legacy", "historical", ("ligand-first",), legacy),
)


def _definitions() -> list[dict[str, Any]]:
    definitions = [tool for _, _, _, module in _MODULES for tool in module.TOOLS]
    names = [tool["name"] for tool in definitions]
    if len(names) != len(set(names)):
        raise ValueError("Duplicate MCP tool definition")
    return definitions


TOOLS = _definitions()


def groups() -> list[dict[str, Any]]:
    """Describe maintenance ownership; this is not a route allowlist."""
    return [
        {"group": name, "role": role, "routes": list(routes),
         "module": module.__name__, "tools": [tool["name"] for tool in module.TOOLS]}
        for name, role, routes, module in _MODULES
    ]


def tool_catalog() -> dict[str, Any]:
    """Explicit migration disposition, separate from current tool visibility."""
    from .catalog import catalog
    return catalog(TOOLS, groups())


def handlers(root: Path, arguments: dict[str, Any]) -> dict[str, Callable[[], dict[str, Any]]]:
    registered: dict[str, Callable[[], dict[str, Any]]] = {}
    for name, _, _, module in _MODULES:
        bound = module.handlers(root, arguments)
        if set(bound) != {tool["name"] for tool in module.TOOLS}:
            raise ValueError(f"MCP definitions and handlers differ in {name}")
        if registered.keys() & bound.keys():
            raise ValueError(f"Duplicate MCP tool handler in {name}")
        registered.update(bound)
    return registered
