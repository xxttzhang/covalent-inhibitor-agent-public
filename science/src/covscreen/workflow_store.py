"""Atomic persistence shared by route workflows.

The route owns state schema, identity checks, locks, revision comparison and
scientific transitions. Callers must hold the route's lock while changing state.
This module does not create a second task ledger or interpret scientific status.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4


def save(folder: Path, state: dict):
    temporary = folder / (".state-" + uuid4().hex)
    with temporary.open("x") as stream:
        json.dump(state, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(folder / "state.json")


def load(folder: Path) -> dict:
    return json.loads((folder / "state.json").read_text())
