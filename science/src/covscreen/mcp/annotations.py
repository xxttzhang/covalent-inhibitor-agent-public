"""MCP behavior hints; authorization is enforced by the dispatcher, not these hints."""

READ_ONLY = {"readOnlyHint": True, "destructiveHint": False, "idempotentHint": True}
IDEMPOTENT_WRITE = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": True}
EXECUTION = {"readOnlyHint": False, "destructiveHint": False, "idempotentHint": False}
CANCELLATION = {"readOnlyHint": False, "destructiveHint": True, "idempotentHint": False}
