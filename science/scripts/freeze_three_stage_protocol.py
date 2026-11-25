"""Create an immutable, content-addressed three-stage validation registration."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
from pathlib import Path

import yaml
from Bio.PDB.MMCIF2Dict import MMCIF2Dict


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def source_record(project_root: Path, path: Path) -> dict[str, object]:
    resolved = path if path.is_absolute() else project_root / path
    if not resolved.is_file():
        raise FileNotFoundError(resolved)
    return {
        "path": str(path),
        "bytes": resolved.stat().st_size,
        "sha256": sha256_file(resolved),
    }


def scalar_or_list(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else [str(value)]


def verify_target_mapping(project_root: Path, target: dict[str, object]) -> dict[str, object]:
    config_path = project_root / str(target["config"])
    config = yaml.safe_load(config_path.read_text())
    cif_path = project_root / str(target["pdb_cif"])
    cif = MMCIF2Dict(str(cif_path))
    chains = scalar_or_list(cif["_pdbx_poly_seq_scheme.pdb_strand_id"])
    auth = scalar_or_list(cif["_pdbx_poly_seq_scheme.auth_seq_num"])
    seq_ids = scalar_or_list(cif["_pdbx_poly_seq_scheme.seq_id"])
    monomers = scalar_or_list(cif["_pdbx_poly_seq_scheme.mon_id"])
    hits = [
        (int(sequence_id), monomer)
        for chain, residue, sequence_id, monomer in zip(chains, auth, seq_ids, monomers)
        if chain == str(config["protein_chain"])
        and residue not in {".", "?"}
        and int(float(residue)) == int(config["residue_number"])
    ]
    expected = (int(config["cysteine_sequence_index"]), "CYS")
    if hits != [expected]:
        raise ValueError(f"Residue mapping mismatch for {target['target_id']}: {hits} != {[expected]}")
    return {
        "target_id": target["target_id"],
        "site_id": target["site_id"],
        "protein_chain": config["protein_chain"],
        "auth_residue_number": int(config["residue_number"]),
        "af3_sequence_index": int(config["cysteine_sequence_index"]),
        "residue_name": "CYS",
        "reference_pdb": config["reference_pdb"],
        "reference_ligand": config["reference_ligand"],
        "config_source": source_record(project_root, Path(str(target["config"]))),
        "pdb_source": source_record(project_root, Path(str(target["pdb"]))),
        "cif_source": source_record(project_root, Path(str(target["pdb_cif"]))),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-root", type=Path, default=Path.cwd())
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--panel-config", type=Path, required=True)
    parser.add_argument("--blinded-manifest", type=Path, required=True)
    parser.add_argument("--rtmscore-checkpoint", type=Path, required=True)
    parser.add_argument("--code-file", type=Path, action="append", default=[])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.project_root.resolve()
    if args.output.exists():
        raise FileExistsError(f"Refusing to overwrite frozen protocol: {args.output}")
    protocol = json.loads(args.template.read_text())
    panel = json.loads(args.panel_config.read_text())
    blind = json.loads(args.blinded_manifest.read_text())
    if blind.get("status") != "BLINDED_AND_SEALED" or blind.get("labels_exposed_to_scoring_pipeline") is not False:
        raise ValueError("Blinded panel is not sealed")
    if panel["panel_id"] != blind["panel_id"]:
        raise ValueError("Panel and blinded-manifest identifiers differ")
    if len(panel["targets"]) != 5:
        raise ValueError("Confirmatory panel must contain exactly five targets")
    protocol["registration_utc"] = dt.datetime.now(dt.timezone.utc).isoformat()
    protocol["panel_id"] = panel["panel_id"]
    protocol["targets"] = [verify_target_mapping(root, target) for target in panel["targets"]]
    protocol["frozen_sources"] = {
        "template": source_record(root, args.template),
        "panel_config": source_record(root, args.panel_config),
        "blinded_panel_manifest": source_record(root, args.blinded_manifest),
        "rtmscore_checkpoint": source_record(root, args.rtmscore_checkpoint),
        "code": [source_record(root, path) for path in args.code_file],
    }
    protocol["blinded_answer_key_commitments"] = blind["plaintext_commitments"]
    protocol["sealed_answer_key_files"] = blind["sealed_files"]
    canonical = json.dumps(protocol, sort_keys=True, separators=(",", ":")).encode()
    protocol["freeze_id"] = f"three_stage_blind_v1-{hashlib.sha256(canonical).hexdigest()[:16]}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(protocol, indent=2) + "\n")
    print(json.dumps({"status": "FROZEN", "freeze_id": protocol["freeze_id"], "targets": len(protocol["targets"]), "output": str(args.output)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
