"""Standalone hypothesis MCP, deployable without replacing the running route MCP."""
import contextlib
import json
from pathlib import Path
import sys

import os
ROOT = Path(os.environ.get("COVSCREEN_PROJECT_ROOT", str(Path(__file__).resolve().parents[1]))).resolve()
SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE / "src"))
from covscreen.target_hypothesis import Hypothesis, hypothesis_tool

from covscreen.mcp.upstream import TOOLS as UPSTREAM_TOOLS, handlers as upstream_handlers
from covscreen.mcp.ligand_workflow import TOOLS as WORKFLOW_TOOLS, handlers as workflow_handlers
TOOLS = UPSTREAM_TOOLS + WORKFLOW_TOOLS


def handlers(root, arguments):
    return {**upstream_handlers(root, arguments), **workflow_handlers(root, arguments)}
TOOL = TOOLS[0]
SCHEMA = TOOL["inputSchema"]



def main():
    for line in sys.stdin:
        message = json.loads(line)
        if "id" not in message:
            continue
        method = message.get("method")
        if method == "initialize":
            result = {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                "capabilities": {"tools": {}}, "serverInfo": {"name": "target-hypothesis", "version": "1.1.0"}}
        elif method == "tools/list":
            result = {"tools": TOOLS}
        elif method == "resources/list":
            result = {"resources": []}
        elif method == "prompts/list":
            result = {"prompts": []}
        elif method == "tools/call":
            try:
                params = message["params"]
                selected = next((item for item in TOOLS if item["name"] == params["name"]), None)
                if selected is None:
                    raise ValueError("Unknown tool")
                arguments = params.get("arguments", {})
                if not isinstance(arguments, dict) or set(arguments) - selected["inputSchema"]["properties"].keys():
                    raise ValueError("Unexpected hypothesis arguments")
                with contextlib.redirect_stdout(sys.stderr):
                    value = handlers(ROOT, arguments)[params["name"]]()
                result = {"content": [{"type": "text", "text": json.dumps(value, ensure_ascii=False, indent=2)}],
                    "isError": value.get("state") in {"FAILED", "INTERRUPTED"}}
            except Exception as error:
                result = {"content": [{"type": "text", "text": str(error)}], "isError": True}
        elif method == "ping":
            result = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": message["id"],
                "error": {"code": -32601, "message": "Unknown method"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
