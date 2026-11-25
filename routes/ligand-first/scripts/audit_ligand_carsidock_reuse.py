"""Read-only compatibility audit before adopting retained grouped CarsiDock results.

Current-byte equality cannot prove an unrecorded historical input hash. That
limitation is explicit; this audit does not register or mark a new run complete.
"""

import _bootstrap  # canonical route and shared runtime, independent of cwd
import argparse
import json
from pathlib import Path

import pandas as pd
from covscreen.jobs import sha256_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retained-dir", required=True, type=Path)
    parser.add_argument("--original-candidates", required=True, type=Path)
    parser.add_argument("--registered-candidates", required=True, type=Path)
    parser.add_argument("--pdb", required=True, type=Path)
    parser.add_argument("--reference-ligand", required=True, type=Path)
    parser.add_argument("--checkpoint", required=True, type=Path)
    parser.add_argument("--covres", required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    old = pd.read_parquet(args.original_candidates)
    new = pd.read_parquet(args.registered_candidates)
    columns = ["input_sdf_index", "compound_id", "standardized_smiles",
               "warhead_class", "carsidock_reaction_type", "electrophilic_atom_map"]
    identical = old[columns].sort_values("input_sdf_index").reset_index(drop=True).equals(
        new[columns].sort_values("input_sdf_index").reset_index(drop=True))
    checks = {"candidate_identity_and_reaction_equal": identical,
              "identity_unique": bool(new.input_sdf_index.is_unique and len(new))}
    evidence = []
    for reaction in sorted(set(new.carsidock_reaction_type)):
        slug = "_".join(reaction.lower().replace("/", " ").split())
        prefix = args.retained_dir / f"carsidock_{slug}"
        path = Path(f"{prefix}.run_manifest.json")
        manifest = json.loads(path.read_text())
        command = manifest["command"]
        def value(flag):
            if command.count(flag) != 1:
                raise ValueError(f"missing_or_ambiguous_flag:{flag}")
            return command[command.index(flag) + 1]
        expected = {"-covres": args.covres, "-rectype": reaction,
                    "--seed": str(args.seed), "--num_conformer": "1"}
        group = {"upstream_success": manifest.get("status") == "SUCCESS" and manifest.get("return_code") == 0,
                 "parameters_equal": all(value(k) == v for k, v in expected.items()),
                 "electrophile_guard": manifest.get("enforce_electrophile_map") is True,
                 "checkpoint_matches_recorded_hash": sha256_file(args.checkpoint) == manifest.get("availability", {}).get("checkpoint_sha256")}
        for flag, current in [("-p", args.pdb), ("-rl", args.reference_ligand)]:
            group[f"current_bytes_equal:{flag}"] = sha256_file(Path(value(flag))) == sha256_file(current)
        outputs = [Path(f"{prefix}.sdf"), Path(f"{prefix}_nodumat.sdf"), Path(f"{prefix}_withpckt.pdb")]
        group["retained_outputs_nonempty"] = all(p.is_file() and p.stat().st_size > 0 for p in outputs)
        checks[reaction] = all(group.values())
        evidence.append({"reaction": reaction, "checks": group,
                         "manifest_sha256": sha256_file(path),
                         "output_hashes": {str(p): sha256_file(p) for p in outputs if p.is_file()}})
    payload = {"status": "CURRENT_COMPATIBILITY_PASS" if all(checks.values()) else "FAIL",
               "checks": checks, "evidence": evidence,
               "historical_input_hashes_verified": False,
               "limitation": "Retained manifests record checkpoint SHA256 but not frozen receptor/ligand input hashes. Current file equality is not historical immutability proof.",
               "registered_run_created": False}
    print(json.dumps(payload, indent=2))
    return 0 if all(checks.values()) else 2


if __name__ == "__main__":
    raise SystemExit(main())
