#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import hashlib
import json
import os
import urllib.parse
import urllib.request
from pathlib import Path

from covscreen.ligand_first_inputs import (
    audit_ligand_first_inputs,
    load_campaign,
    write_parent_pose_sdf,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Verify a real Ligand-first campaign and reconstruct its experimental parent pose."
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--parent-pose-output", type=Path, required=True)
    parser.add_argument(
        "--restore-missing",
        action="store_true",
        help="Restore only missing frozen RCSB assets, verifying the declared SHA-256.",
    )
    args = parser.parse_args()

    campaign_path = args.campaign.resolve()
    campaign = load_campaign(campaign_path)
    restored: list[dict[str, str]] = []
    if args.restore_missing:
        for asset in campaign.source_assets:
            destination = campaign_path.parent / asset.path
            if destination.is_file():
                continue
            parsed = urllib.parse.urlparse(asset.source_url)
            if parsed.scheme != "https" or parsed.hostname != "files.rcsb.org":
                raise SystemExit(
                    f"refusing non-RCSB or non-HTTPS source for {asset.path}: {asset.source_url}"
                )
            destination.parent.mkdir(parents=True, exist_ok=True)
            temporary = destination.with_name(
                f".{destination.name}.{os.getpid()}.download"
            )
            try:
                with urllib.request.urlopen(asset.source_url, timeout=60) as response:
                    content = response.read()
                digest = hashlib.sha256(content).hexdigest()
                if digest != asset.sha256:
                    raise ValueError(
                        f"download checksum mismatch for {asset.path}: {digest} != {asset.sha256}"
                    )
                temporary.write_bytes(content)
                temporary.replace(destination)
            finally:
                if temporary.exists():
                    temporary.unlink()
            restored.append(
                {
                    "path": asset.path,
                    "source_url": asset.source_url,
                    "sha256": asset.sha256,
                }
            )
    audit, parent_pose = audit_ligand_first_inputs(campaign, campaign_path.parent)
    args.audit_output.parent.mkdir(parents=True, exist_ok=True)
    payload = audit.model_dump(mode="json")
    payload["restored_assets"] = restored
    payload["restoration_policy"] = "missing-only; HTTPS files.rcsb.org; SHA-256 required"
    args.audit_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    if parent_pose is not None:
        write_parent_pose_sdf(parent_pose, args.parent_pose_output)
    print(json.dumps(audit.model_dump(mode="json"), ensure_ascii=False, indent=2))
    return 0 if audit.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
