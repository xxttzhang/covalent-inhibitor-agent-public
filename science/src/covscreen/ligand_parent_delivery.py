"""Bounded export of the current workflow's official parent proposal files.

No caller paths, scientific state changes, filesystem grants, or compute.
The proposal is sealed by workflow identity; report hashes attest captured
bytes, not a retrospective claim that legacy reports were sealed at creation.
"""
import base64
import hashlib
import json
from pathlib import Path
import re

LIMIT = 2 * 1024 * 1024


def export_parent_report(root: Path, state: dict) -> dict:
    reference = state.get("proposal") or {}
    evidence = state.get("evidence_id")
    proposal = reference.get("proposal_id", "")
    expected = reference.get("proposal_sha256", "")
    if (not re.fullmatch(r"hyp-[a-f0-9]{24}", evidence or "")
            or reference.get("evidence_id") != evidence
            or not re.fullmatch(r"proposal-[a-f0-9]{12}", proposal)
            or not re.fullmatch(r"[a-f0-9]{64}", expected)):
        raise ValueError("Current workflow has no bound parent proposal")
    folder = root / "results/target-hypotheses" / evidence / proposal
    files = []
    for name, mime in (("hypothesis.json", "application/json"), ("report.md", "text/markdown")):
        path = folder / name
        # Check every component below the operator-owned root. Never follow a
        # proposal/file symlink, even if its target happens to remain inside.
        if any(p.is_symlink() for p in (path, folder, folder.parent, folder.parent.parent, root / "results")):
            raise ValueError("Proposal delivery refuses symlinks")
        with path.open("rb") as stream:
            raw = stream.read(LIMIT + 1)
        if len(raw) > LIMIT:
            raise ValueError("Parent artifact exceeds bounded delivery limit")
        if name == 'report.md' and state.get('parent_pair_review'):
            assessment = state['parent_pair_review']
            supplement = ('\n\n## 当前工作流的配对暂缓记录\n\n'
                '以下为设计准备阶段的补充核验，不改写原假设或原报告。未提交枚举或对接。\n\n'
                + '```json\n' + json.dumps({'workflow_id': state['workflow_id'],
                    'revision': state['revision'], 'assessment': assessment},
                    ensure_ascii=False, indent=2) + '\n```\n')
            raw += supplement.encode('utf-8')
            if len(raw) > LIMIT:
                raise ValueError('Parent report with pairing review exceeds delivery limit')
        digest = hashlib.sha256(raw).hexdigest()
        if name == "hypothesis.json":
            if digest != expected:
                raise ValueError("Proposal hash mismatch")
            value = json.loads(raw)
            if value.get("evidence_id") != evidence or value.get("hypothesis", {}).get("route") != "ligand-first":
                raise ValueError("Proposal provenance mismatch")
        files.append({"name": name, "mime_type": mime, "sha256": digest,
                      "bytes": len(raw), "base64": base64.b64encode(raw).decode("ascii")})
    return {"schema": "ligand-parent-delivery-v1", "workflow_id": state["workflow_id"],
            "evidence_id": evidence, "proposal_id": proposal, "proposal_sha256": expected,
            "files": files, "hash_scope": "captured_bytes; hypothesis also matches workflow seal",
            "compute_submitted": False}
