#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
from pathlib import Path

from covscreen.ligand_first_inputs import load_campaign
from covscreen.ligand_first_site_discovery import discover_covalentizer_sites


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Discover deterministic Covalentizer-style branch-replacement sites."
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-sites", type=int, default=8)
    args = parser.parse_args()

    campaign_path = args.campaign.resolve()
    campaign = load_campaign(campaign_path)
    result = discover_covalentizer_sites(
        campaign, campaign_path.parent, max_sites=args.max_sites
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(result.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n"
    )
    print(
        json.dumps(
            {
                "status": result.status,
                "advancing_sites": result.advancing_count,
                "output": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
