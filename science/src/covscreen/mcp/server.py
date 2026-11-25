"""Stdio transport and one authorization boundary for all registered MCP tools."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from rdkit import RDLogger

# Keep benign chemistry warnings out of the stdio transport diagnostics.
# Parse errors remain enabled.
RDLogger.DisableLog("rdApp.warning")

from .registry import SERVER_VERSION, TOOLS, handlers
from .task_guard import guarded_call


def dispatch(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    from covscreen.native_approval import authorize
    if '_workflow_handoff' in arguments:
        from covscreen_library_first.screening_handoff import handle
        return handle(root, name, arguments)
    if '_native_prepare' in arguments:
        raise ValueError('Legacy preparation flag rejected; use the separate read-only approval tool')
    if name == 'covalent_prepare_execution':
        return dispatch_approved(root, name, arguments)
    with authorize(root, name, arguments) as approved:
        return dispatch_approved(root, name, approved)


def dispatch_approved(root: Path, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    registered = handlers(root, arguments)
    if name not in registered:
        raise ValueError(f"unknown tool: {name}")
    return public_output(registered[name]())


def public_output(value):
    """Signing envelopes belong to persisted audit, never model tool output."""
    if isinstance(value, dict):
        return {key: public_output(item) for key, item in value.items()
                if key not in {'native_approval', '_native_approval'}}
    if isinstance(value, list):
        return [public_output(item) for item in value]
    return value


def response(request_id: Any, result: dict[str, Any] | None = None, error: dict[str, Any] | None = None) -> None:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main(root: Path) -> int:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            method = message.get("method")
            request_id = message.get("id")
            if request_id is None:
                continue
            if method == "initialize":
                requested = message.get("params", {}).get("protocolVersion", "2024-11-05")
                response(
                    request_id,
                    {
                        "protocolVersion": requested,
                        "capabilities": {"tools": {"listChanged": False}},
                        "serverInfo": {"name": "covalent-discovery-tools", "version": SERVER_VERSION},
                    },
                )
            elif method == "ping":
                response(request_id, {})
            elif method == "tools/list":
                response(request_id, {"tools": TOOLS})
            elif method == "tools/call":
                params = message.get("params", {})
                try:
                    result = guarded_call(root, params.get("name", ""), params.get("arguments", {}), dispatch)
                    response(
                        request_id,
                        {
                            "content": [
                                {
                                    "type": "text",
                                    "text": json.dumps(result, ensure_ascii=False, indent=2),
                                }
                            ],
                            "structuredContent": result,
                            "isError": result.get("status") == "FAIL",
                        },
                    )
                except Exception as exc:
                    result = {"status": "FAIL", "error": f"{type(exc).__name__}: {exc}"}
                    response(
                        request_id,
                        {
                            "content": [{"type": "text", "text": json.dumps(result)}],
                            "structuredContent": result,
                            "isError": True,
                        },
                    )
            else:
                response(request_id, error={"code": -32601, "message": f"Method not found: {method}"})
        except Exception as exc:
            response(None, error={"code": -32603, "message": f"{type(exc).__name__}: {exc}"})
    return 0
