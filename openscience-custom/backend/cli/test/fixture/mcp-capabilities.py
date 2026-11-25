"""Blocking stdio transport used to verify native host launch interoperability."""
import json
import sys

for line in sys.stdin:
    message = json.loads(line)
    if "id" not in message:
        continue
    method = message["method"]
    results = {
        "initialize": {"protocolVersion": message.get("params", {}).get("protocolVersion", "2024-11-05"),
                       "capabilities": {"tools": {}, "resources": {}, "prompts": {}},
                       "serverInfo": {"name": "python-capability-test", "version": "1.0.0"}},
        "tools/list": {"tools": [{"name": "echo", "description": "Echo a value", "inputSchema": {"type": "object"}}]},
        "resources/list": {"resources": [{"name": "guide", "uri": "memory://guide", "description": "Connector guide", "mimeType": "text/plain"}]},
        "prompts/list": {"prompts": [{"name": "review", "description": "Review a result"}]},
        "ping": {},
    }
    print(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": results[method]}), flush=True)
