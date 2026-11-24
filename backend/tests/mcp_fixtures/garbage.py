"""Test fixture: initialize succeeds, then tools/list returns bytes that are not JSON-RPC."""
import json
import sys

for line in sys.stdin:
    try:
        message = json.loads(line)
    except ValueError:
        continue
    method, identity = message.get("method"), message.get("id")
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                  "serverInfo": {"name": "garbage", "version": "0"}}
    elif method == "tools/list":
        sys.stdout.write("{ this is not json }\n")
        sys.stdout.flush()
        continue
    else:
        continue
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": identity, "result": result}) + "\n")
    sys.stdout.flush()
