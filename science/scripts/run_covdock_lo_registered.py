"""Run an explicitly identified core or AF3 subset through CovDock-LO gating."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rdkit import Chem

from covscreen.covdock_gate import (ALGORITHM_VERSION as LEGACY_GATE_ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION,
    gate_covdock_candidates, merge_pose_consistency)
from covscreen.jobs import sha256_file, utc_now
from covscreen.refinement_input import verify_refinement_input
from covscreen.refinement_output import freeze_refinement_outputs
from covscreen.covdock_receptor import (attachment_site, inspect_receptor,
    verify_reused_inputs, read_effective_receptor)

from parse_covdock_scores import parse_inspection


ALGORITHM_VERSION = "covdock-lo-registered-v1.2"
DEFAULT_SCORE_PROPERTIES = (
    "r_cdock_cdock_affinity",
    "r_i_docking_score",
    "r_i_sample_docking_score",
    "r_i_sample_glide_gscore",
)
IDENTITY_RENAMES = {
    "raw_score": "af3_mpae",
    "engine_status": "af3_status",
    "failure_reason": "af3_failure_reason",
    "engine": "af3_engine",
    "score_direction": "af3_score_direction",
}


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(path)


def _read_table(path: Path) -> pd.DataFrame:
    if path.suffix.lower() == ".parquet":
        return pd.read_parquet(path)
    if path.suffix.lower() == ".csv":
        return pd.read_csv(path)
    raise ValueError("candidate table must be CSV or Parquet")


def _stable_title(row: object) -> str:
    compound = re.sub(r"[^A-Za-z0-9._-]+", "_", str(row.compound_id)).strip("_")
    compound = compound[:60] or "compound"
    protomer = re.sub(
        r"[^A-Za-z0-9._-]+", "_", str(getattr(row, "protomer_id", "0"))
    ).strip("_")
    return f"{compound}__p{protomer or '0'}__i{int(row.input_sdf_index)}"


def prepare_identity_and_sdf(
    candidate_table: Path,
    identity_output: Path,
    sdf_output: Path,
    input_score_source: str = "af3",
) -> pd.DataFrame:
    if input_score_source not in {"af3", "core"}:
        raise ValueError("CovDock input score source must be af3 or core")
    frame = _read_table(candidate_table).copy()
    required = {"input_sdf_index", "compound_id", "standardized_smiles"}
    missing = sorted(required - set(frame))
    if missing:
        raise ValueError(f"Candidate table is missing columns: {missing}")
    frame["input_sdf_index"] = pd.to_numeric(
        frame["input_sdf_index"], errors="raise"
    ).astype("int64")
    if frame["input_sdf_index"].duplicated().any():
        raise ValueError("Candidate table has duplicate input_sdf_index values")
    frame["compound_id"] = frame["compound_id"].astype(str)
    if "protomer_id" not in frame:
        frame["protomer_id"] = "0"
    if "parent_id" not in frame:
        frame["parent_id"] = frame["compound_id"]
    if input_score_source == "core" and any(str(column).startswith("af3_") for column in frame):
        raise ValueError("Core-only input cannot claim AF3 evidence")
    if input_score_source == "af3" and "af3_mpae_rank" not in frame:
        score = pd.to_numeric(
            frame["raw_score"]
            if "raw_score" in frame
            else pd.Series(np.nan, index=frame.index),
            errors="coerce",
        )
        frame["af3_mpae_rank"] = score.rank(method="first", na_option="bottom").astype(int)
    renames = IDENTITY_RENAMES if input_score_source == "af3" else {
        name: "core_input_" + name for name in IDENTITY_RENAMES
    }
    if any(old in frame and new in frame for old, new in renames.items()):
        raise ValueError("Ambiguous input score provenance columns")
    frame = frame.rename(columns={old: new for old, new in renames.items() if old in frame})
    frame["covdock_input_score_source"] = input_score_source
    identity_output.parent.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(identity_output, index=False)
    frame.to_csv(identity_output.with_suffix(".csv"), index=False)

    writer = Chem.SDWriter(str(sdf_output))
    written = 0
    try:
        for row in frame.itertuples(index=False):
            molecule = Chem.MolFromSmiles(str(row.standardized_smiles))
            if molecule is None:
                raise ValueError(
                    f"invalid standardized_smiles for input_sdf_index={row.input_sdf_index}"
                )
            molecule.SetProp("_Name", _stable_title(row))
            molecule.SetIntProp("INPUT_SDF_INDEX", int(row.input_sdf_index))
            molecule.SetProp("COMPOUND_ID", str(row.compound_id))
            molecule.SetProp("PROTOMER_ID", str(row.protomer_id))
            molecule.SetProp("INPUT_SCORE_SOURCE", input_score_source)
            if input_score_source == "af3":
                molecule.SetIntProp("AF3_MPAE_RANK", int(row.af3_mpae_rank))
            af3_mpae = pd.to_numeric(
                getattr(row, "af3_mpae", np.nan), errors="coerce"
            )
            if np.isfinite(af3_mpae):
                molecule.SetDoubleProp("AF3_MPAE", float(af3_mpae))
            writer.write(molecule)
            written += 1
    finally:
        writer.close()
    if written != len(frame):
        raise RuntimeError(f"identity/SDF count mismatch: table={len(frame)}, sdf={written}")
    return frame


def _run(command: list[str], *, env: dict[str, str] | None = None) -> None:
    process = subprocess.run(command, env=env, check=False)
    if process.returncode != 0:
        raise RuntimeError(
            f"registered subprocess failed with return code {process.returncode}: {command[1]}"
        )


def _successful_ligprep(manifest: Path, output_mae: Path, input_sdf: Path) -> bool:
    if not manifest.is_file() or not output_mae.is_file() or output_mae.stat().st_size == 0:
        return False
    payload = json.loads(manifest.read_text())
    if payload.get("status") != "SUCCESS":
        return False
    if (payload.get("input_sdf_sha256") != sha256_file(input_sdf)
            or payload.get("output_mae_sha256") != sha256_file(output_mae)):
        raise ValueError("LigPrep retained input/output differs or lacks frozen provenance")
    return True


def _successful_covdock(manifest: Path) -> list[Path]:
    if not manifest.is_file():
        return []
    payload = json.loads(manifest.read_text())
    if payload.get("status") != "SUCCESS" or payload.get("return_code") != 0:
        return []
    parsed = payload.get("parsed_outputs", {})
    pose_files = [Path(value) for value in parsed.get("pose_files", [])]
    return pose_files if pose_files and all(path.is_file() and path.stat().st_size > 0 for path in pose_files) else []


def _choose_score_property(inspections: list[Path], explicit: str | None) -> str:
    available: set[str] = set()
    for path in inspections:
        payload = json.loads(path.read_text())
        available.update(payload.get("candidate_score_properties", []))
        for row in payload.get("rows", []):
            available.update(key for key, value in row.items() if value not in (None, ""))
    if explicit:
        if explicit not in available:
            raise ValueError(f"requested CovDock score property is absent: {explicit}")
        return explicit
    for candidate in DEFAULT_SCORE_PROPERTIES:
        if candidate in available:
            return candidate
    raise ValueError(
        "no supported CovDock score property found; available candidates="
        + json.dumps(sorted(name for name in available if "score" in name.lower()))
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run resumable LigPrep, CovDock-LO, pose inspection, and A/B/C gating."
    )
    parser.add_argument("--candidate-table", type=Path, required=True)
    parser.add_argument("--expected-input-count", type=int)
    parser.add_argument("--expected-input-sha256")
    parser.add_argument("--expected-identity-sha256")
    parser.add_argument("--input-score-source", choices=("af3", "core"),
                        help="Required for exact-input contracts; core input must not be labelled as AF3 evidence")
    parser.add_argument("--receptor", type=Path, required=True)
    parser.add_argument("--expected-receptor-sha256")
    parser.add_argument("--expected-grid-sha256")
    parser.add_argument("--attachment-residue", required=True, help="CHAIN:author_residue")
    parser.add_argument("--nucleophilic-atom", default="SG")
    parser.add_argument("--reaction-type", default="Michael Addition")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--schrodinger-root", type=Path, required=True)
    parser.add_argument("--license-file", type=Path, required=True)
    parser.add_argument("--runtime-lib-dir", type=Path)
    parser.add_argument("--prepare-receptor", action="store_true")
    parser.add_argument("--grid-file", type=Path)
    parser.add_argument("--njobs", type=int, default=1)
    parser.add_argument("--host")
    parser.add_argument(
        "--jobname-prefix",
        default="library_first",
        help=(
            "Bounded identifier prepended to Schr\u00f6dinger Job Control names. "
            "Parallel candidate shards must use distinct prefixes."
        ),
    )
    parser.add_argument("--min-radius", type=float)
    parser.add_argument("--score-property")
    parser.add_argument(
        "--pose-consistency-table",
        type=Path,
        help="Optional precomputed identity-audited local AF3/CovDock RMSD table.",
    )
    parser.add_argument(
        "--reuse-artifact-dir",
        type=Path,
        help="Reuse validated LigPrep/CovDock artifacts from an earlier registered run.",
    )
    parser.add_argument("--final-n", type=int, default=30)
    parser.add_argument("--gate-algorithm-version", choices=(LEGACY_GATE_ALGORITHM_VERSION, STRICT_ALGORITHM_VERSION, LIGAND_ALGORITHM_VERSION),
                        default=LEGACY_GATE_ALGORITHM_VERSION, help="Frozen evaluation policy; provisional v3 must be explicitly selected.")
    parser.add_argument("--max-local-strain", type=float, default=10.0)
    parser.add_argument("--hard-max-strain", type=float, default=20.0)
    parser.add_argument("--min-covalent-bond-length", type=float, default=1.5)
    parser.add_argument("--max-covalent-bond-length", type=float, default=2.2)
    parser.add_argument("--max-pose-rmsd", type=float, default=3.0)
    parser.add_argument("--severe-clash-distance", type=float, default=1.2)
    parser.add_argument("--min-success-rate", type=float, default=0.80)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    input_contract = verify_refinement_input(args.candidate_table, args.expected_input_count,
                                            args.expected_input_sha256, args.expected_identity_sha256)
    if input_contract is not None and args.input_score_source is None:
        parser.error("Exact-input CovDock requires --input-score-source")
    input_score_source = args.input_score_source or "af3"  # Historical standalone behavior only.
    prefix = "core_subset" if input_score_source == "core" else "af3_top"

    attachment_chain, attachment_number = attachment_site(args.attachment_residue)
    receptor_check = inspect_receptor(args.receptor, args.attachment_residue,
        args.nucleophilic_atom, expected_sha256=args.expected_receptor_sha256)
    candidate_sha = sha256_file(args.candidate_table)
    grid_sha = sha256_file(args.grid_file) if args.grid_file else None
    if args.expected_grid_sha256 is not None and args.expected_grid_sha256 != grid_sha:
        raise ValueError("CovDock grid differs from the frozen plan")
    if args.njobs < 1:
        parser.error("--njobs must be positive")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}", args.jobname_prefix):
        parser.error("--jobname-prefix must be a safe 1-48 character identifier")

    output_dir = args.output_dir.resolve()
    if args.reuse_artifact_dir and args.reuse_artifact_dir.resolve() == output_dir:
        parser.error("reuse source must differ from the new output directory")
    output_dir.mkdir(parents=True, exist_ok=True)
    identity_table = output_dir / f"{prefix}_identity.parquet"
    ligand_sdf = output_dir / f"{prefix}_for_ligprep.sdf"
    identities = prepare_identity_and_sdf(
        args.candidate_table.resolve(), identity_table, ligand_sdf, input_score_source
    )
    if input_contract is not None:
        verify_refinement_input(args.candidate_table, args.expected_input_count,
                                args.expected_input_sha256, args.expected_identity_sha256)
        verify_refinement_input(identity_table, args.expected_input_count,
                                sha256_file(identity_table), args.expected_identity_sha256)

    reuse_dir = args.reuse_artifact_dir.resolve() if args.reuse_artifact_dir else None
    covdock_job = f"{args.jobname_prefix}_{prefix}_covdock_lo"
    if reuse_dir:
        source_sdf = reuse_dir / f"{prefix}_for_ligprep.sdf"
        if not source_sdf.is_file() or sha256_file(source_sdf) != sha256_file(ligand_sdf):
            raise ValueError("Retained CovDock candidate SDF differs from requested batch")
        source_manifest = reuse_dir / "covdock_lo" / f"{covdock_job}.run_manifest.json"
        if not _successful_covdock(source_manifest):
            raise ValueError("Retained CovDock batch lacks complete successful outputs; reparse will not launch docking")
        source = json.loads(source_manifest.read_text())
        verify_reused_inputs(source, receptor_check['receptor_sha256'],
            sha256_file(reuse_dir / 'ligprep' / f'{prefix}_ligprep.mae'),
            args.attachment_residue, args.reaction_type, 'leadopt',
            prepare_receptor=args.prepare_receptor, grid_sha256=grid_sha)
    ligprep_dir = (reuse_dir / "ligprep") if reuse_dir else (output_dir / "ligprep")
    ligand_mae = ligprep_dir / f"{prefix}_ligprep.mae"
    ligprep_job = f"{args.jobname_prefix}_{prefix}_ligprep"
    ligprep_manifest = ligprep_dir / f"{ligprep_job}.run_manifest.json"
    ligprep_resumed = (args.resume or reuse_dir is not None) and _successful_ligprep(
        ligprep_manifest, ligand_mae, ligand_sdf)
    if reuse_dir and not ligprep_resumed:
        raise ValueError("Retained LigPrep output unavailable; reparse will not launch LigPrep")
    if not ligprep_resumed:
        ligprep_command = [
            sys.executable,
            str(Path(__file__).with_name("run_ligprep_job.py")),
            "--schrodinger-root", str(args.schrodinger_root.resolve()),
            "--license-file", str(args.license_file.resolve()),
            "--input-sdf", str(ligand_sdf),
            "--output-mae", str(ligand_mae),
            "--output-dir", str(ligprep_dir),
            "--jobname", ligprep_job,
        ]
        if args.runtime_lib_dir:
            ligprep_command.extend(
                ["--runtime-lib-dir", str(args.runtime_lib_dir.resolve())]
            )
        _run(ligprep_command)

    covdock_dir = (reuse_dir / "covdock_lo") if reuse_dir else (output_dir / "covdock_lo")
    covdock_manifest = covdock_dir / f"{covdock_job}.run_manifest.json"
    ligand_sha = sha256_file(ligand_mae)
    if (args.resume or reuse_dir) and covdock_manifest.is_file():
        verify_reused_inputs(json.loads(covdock_manifest.read_text()), receptor_check['receptor_sha256'],
            ligand_sha, args.attachment_residue, args.reaction_type, 'leadopt',
            prepare_receptor=args.prepare_receptor, grid_sha256=grid_sha)
    pose_files = _successful_covdock(covdock_manifest) if args.resume or reuse_dir else []
    covdock_resumed = bool(pose_files)
    if not pose_files:
        env = os.environ.copy()
        # This child is our project helper, not a vendor executable. It needs
        # the registered Python runtime, project source and dependency paths.
        # run_covdock_job strips these again at the vendor subprocess boundary.
        for key in ("LD_PRELOAD", "LD_AUDIT"):
            env.pop(key, None)
        env["SCHRODINGER"] = str(args.schrodinger_root.resolve())
        env["SCHROD_LICENSE_FILE"] = str(args.license_file.resolve())
        covdock_command = [
            sys.executable,
            str(Path(__file__).with_name("run_covdock_job.py")),
            "--schrodinger-root", str(args.schrodinger_root.resolve()),
            "--receptor", str(args.receptor.resolve()),
            "--ligand", str(ligand_mae),
            "--attachment-residue", args.attachment_residue,
            "--nucleophilic-atom", args.nucleophilic_atom,
            "--expected-receptor-sha256", receptor_check['receptor_sha256'],
            "--expected-ligand-sha256", ligand_sha,
            "--reaction-type", args.reaction_type,
            "--mode", "leadopt",
            "--jobname", covdock_job,
            "--output-dir", str(covdock_dir),
            "--njobs", str(args.njobs),
            "--covdock-flag", "affinity",
            "--covdock-flag", "strain",
        ]
        if args.prepare_receptor:
            covdock_command.append("--prepare-receptor")
        if args.grid_file:
            covdock_command.extend(["--grid-file", str(args.grid_file.resolve())])
            covdock_command.extend(["--expected-grid-sha256", grid_sha])
        if args.host:
            covdock_command.extend(["--host", args.host])
        if args.min_radius is not None:
            covdock_command.extend(["--min-radius", str(args.min_radius)])
        if args.resume and covdock_manifest.is_file():
            covdock_command.append("--restart")
        _run(covdock_command, env=env)
        pose_files = _successful_covdock(covdock_manifest)
    if not pose_files:
        raise RuntimeError("CovDock-LO completed without a validated pose file")
    verify_reused_inputs(json.loads(covdock_manifest.read_text()), receptor_check['receptor_sha256'],
        ligand_sha, args.attachment_residue, args.reaction_type, 'leadopt',
        prepare_receptor=args.prepare_receptor, grid_sha256=grid_sha)
    inspect_receptor(args.receptor, args.attachment_residue, args.nucleophilic_atom,
        expected_sha256=receptor_check['receptor_sha256'])
    if sha256_file(args.candidate_table) != candidate_sha:
        raise ValueError("CovDock candidate table changed during execution")
    effective_receptor_check = read_effective_receptor(covdock_manifest, args.receptor,
        args.attachment_residue, args.nucleophilic_atom)

    inspection_dir = output_dir / "inspection"
    inspection_dir.mkdir(parents=True, exist_ok=True)
    inspection_paths: list[Path] = []
    schrodinger_env = os.environ.copy()
    schrodinger_env["SCHRODINGER"] = str(args.schrodinger_root.resolve())
    for key in ("LD_LIBRARY_PATH", "LD_PRELOAD", "LD_AUDIT", "PYTHONHOME", "PYTHONPATH"):
        schrodinger_env.pop(key, None)
    for index, pose_file in enumerate(pose_files):
        prefix = inspection_dir / f"pose_{index:03d}_audit"
        command = [
            str(args.schrodinger_root.resolve() / "run"),
            "python3",
            str(Path(__file__).with_name("inspect_covdock_output.py")),
            "--input", str(pose_file.resolve()),
            "--output-prefix", str(prefix),
            "--attachment-chain", attachment_chain,
            "--attachment-residue", str(attachment_number),
            "--nucleophilic-atom", args.nucleophilic_atom,
            "--severe-clash-distance", str(args.severe_clash_distance),
        ]
        _run(command, env=schrodinger_env)
        inspection_paths.append(prefix.with_suffix(".json"))

    score_property = _choose_score_property(inspection_paths, args.score_property)
    parsed = parse_inspection(
        inspection_paths,
        identity_table,
        score_property,
        "lower_is_better",
        engine="schrodinger-covdock-lo",
    )
    if len(parsed) != len(identities):
        raise RuntimeError(
            f"CovDock identity loss: input={len(identities)}, parsed={len(parsed)}"
        )
    if args.pose_consistency_table is not None:
        pose_consistency = _read_table(args.pose_consistency_table.resolve())
        parsed = merge_pose_consistency(parsed, pose_consistency)
    score_table = output_dir / "covdock_lo_all_scores.parquet"
    parsed.to_parquet(score_table, index=False)
    parsed.to_csv(score_table.with_suffix(".csv"), index=False)
    gate_manifest = gate_covdock_candidates(
        parsed,
        output_dir,
        final_n=args.final_n,
        max_local_strain=args.max_local_strain,
        hard_max_strain=args.hard_max_strain,
        min_covalent_bond_length=args.min_covalent_bond_length,
        max_covalent_bond_length=args.max_covalent_bond_length,
        max_pose_rmsd=args.max_pose_rmsd,
        min_success_rate=args.min_success_rate,
        algorithm_version=args.gate_algorithm_version,
    )
    output_files = [
        identity_table,
        identity_table.with_suffix(".csv"),
        ligand_sdf,
        score_table,
        score_table.with_suffix(".csv"),
        output_dir / "stage5_gate_manifest.json",
        output_dir / "stage5_gated_candidates.parquet",
        output_dir / "final_candidates.parquet",
        output_dir / "stage5_failures.parquet",
    ]
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": ALGORITHM_VERSION,
        "status": gate_manifest["status"],
        "generated_at": utc_now(),
        "input_candidate_table": str(args.candidate_table.resolve()),
        "input_candidate_table_sha256": sha256_file(args.candidate_table.resolve()),
        "refinement_input_contract": input_contract,
        "input_score_source": input_score_source,
        "input_receptor": str(args.receptor.resolve()),
        "input_receptor_sha256": sha256_file(args.receptor.resolve()),
        "receptor_input_check": receptor_check,
        "effective_receptor_input_check": effective_receptor_check,
        "pose_consistency_table": (
            str(args.pose_consistency_table.resolve())
            if args.pose_consistency_table is not None
            else None
        ),
        "pose_consistency_table_sha256": (
            sha256_file(args.pose_consistency_table.resolve())
            if args.pose_consistency_table is not None
            else None
        ),
        "input_count": len(identities),
        "attachment_residue": args.attachment_residue,
        "nucleophilic_atom": args.nucleophilic_atom,
        "reaction_type": args.reaction_type,
        "jobname_prefix": args.jobname_prefix,
        "score_property": score_property,
        "score_direction": "lower_is_better",
        "resume": {
            "requested": args.resume,
            "ligprep_reused": ligprep_resumed,
            "covdock_reused": covdock_resumed,
            "source_artifact_dir": str(reuse_dir) if reuse_dir else None,
        },
        "license": {
            "configured": args.license_file.is_file(),
            "file_sha256": sha256_file(args.license_file) if args.license_file.is_file() else None,
            "content_redacted": True,
        },
        "pose_files": [str(path) for path in pose_files],
        "inspection_files": [str(path) for path in inspection_paths],
        "gate_quality": gate_manifest["quality"],
        "gate_quality_gates": gate_manifest["quality_gates"],
        "outputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)}
            for path in output_files
            if path.is_file()
        },
    }
    _atomic_json(output_dir / "covdock_lo_registered_manifest.json", manifest)
    if input_contract is not None:
        manifest["refinement_artifacts"] = freeze_refinement_outputs(output_dir, "covdock", input_contract,
            expected_gate_version=args.gate_algorithm_version)
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    return 0 if manifest["status"] == "completed" else 4


if __name__ == "__main__":
    raise SystemExit(main())
