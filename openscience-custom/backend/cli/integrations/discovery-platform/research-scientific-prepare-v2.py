"""Isolated, CPU-only PDBFixer preparation of the deposited 1M17 construct.

All modeled coordinates are provenance-labelled and have PDB occupancy zero.
This writes a proposed prepared structure, never a scientific acceptance result.
"""
from __future__ import annotations

import os
for key in ("OPENMM_CPU_THREADS", "OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
    os.environ[key] = "1"

import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import random
import resource
import signal
import time
import traceback

import numpy as np
import openmm as mm
from openmm import app, unit
from pdbfixer import PDBFixer
import pdbfixer

WORKSPACE = Path(__file__).resolve().parents[5]
VERSION = "egfr-1m17-preparation-v2"


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write_json(path, value):
    with Path(path).open("x") as handle:
        json.dump(value, handle, indent=2, allow_nan=False)
        handle.write("\n")


def read_atoms(path):
    return [{"serial": int(line[6:11]), "name": line[12:16].strip(), "altloc": line[16], "residue": line[17:20], "chain": line[21],
             "number": int(line[22:26]), "insertion": line[26], "xyz": [float(line[i:i + 8]) for i in (30, 38, 46)],
             "occupancy": float(line[54:60]), "bfactor": float(line[60:66]), "element": line[76:78].strip(), "line": line}
            for line in Path(path).read_text().splitlines() if line.startswith(("ATOM  ", "HETATM"))]


def identity(atom):
    return (atom["chain"], atom["number"], atom["residue"].strip(), atom["name"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    policy = json.loads(args.policy.read_text())
    assert policy["schema"] == "drug-design-receptor-preparation-policy-v2"
    assert policy["preparation_script_sha256"] == sha(__file__)
    assert policy["runtime"]["openmm_full_version"] == mm.version.full_version
    assert policy["runtime"]["pdbfixer_version"] == importlib.metadata.version("pdbfixer")
    assert policy["runtime"]["pdbfixer_core_sha256"] == sha(Path(pdbfixer.__file__).with_name("pdbfixer.py"))
    assert policy["compute"]["worker_cpu_seconds_limit"] + policy["compute"]["audit_cpu_seconds_limit"] <= 7200
    original = WORKSPACE / policy["source"]["path"]
    assert sha(original) == policy["source"]["sha256"]
    args.output_dir.mkdir(parents=True, exist_ok=False)
    source_atoms = read_atoms(original)
    source_lines = original.read_text().splitlines()
    sequence = [word for line in source_lines if line.startswith("SEQRES") and line[11] == "A" for word in line[19:70].split()]
    assert len(sequence) == 333
    start, cpu = time.monotonic(), time.process_time()
    ledger = {"schema": "drug-design-receptor-preparation-receipt-v2", "version": VERSION, "status": "running", "scientific_acceptance": "not_evaluated",
              "source_sha256": sha(original), "policy_sha256": sha(args.policy), "script_sha256": sha(__file__), "platform": "CPU", "threads": 1,
              "new_gpu_compute": False, "pdbfixer_version": importlib.metadata.version("pdbfixer"), "openmm_full_version": mm.version.full_version,
              "modeled_coordinates_are_experimental": False, "stages": [], "artifacts": {}}
    limit = policy["compute"]["worker_cpu_seconds_limit"]
    resource.setrlimit(resource.RLIMIT_CPU, (limit - 10, limit))
    def deadline(signum, frame):
        raise TimeoutError("Frozen local CPU/wall preparation limit reached")
    signal.signal(signal.SIGXCPU, deadline)
    signal.signal(signal.SIGALRM, deadline)
    signal.alarm(limit)
    def progress(stage, **detail):
        item = {"stage": stage, "wall_seconds": time.monotonic() - start, "process_cpu_seconds": time.process_time() - cpu, **detail}
        ledger["stages"].append(item)
        (args.output_dir / "progress.json").write_text(json.dumps(ledger, indent=2, allow_nan=False) + "\n")
        print(json.dumps(item, allow_nan=False), flush=True)
    try:
        seed = policy["random_seed"]
        random.seed(seed)
        np.random.seed(seed)
        platform = mm.Platform.getPlatformByName("CPU")
        platform.setPropertyDefaultValue("Threads", "1")
        retained, removed = [], []
        for atom in source_atoms:
            if not atom["line"].startswith("ATOM  "):
                removed.append({"original": atom, "reason": "explicit_dry_protein_no_crystal_ligand_or_water"})
            elif atom["occupancy"] <= 0:
                removed.append({"original": atom, "reason": "unsupported_zero_occupancy_coordinate_scheduled_for_rebuilding"})
            elif atom["altloc"].strip() and atom["altloc"] != "A":
                removed.append({"original": atom, "reason": "whole_residue_lexicographic_altloc_A_policy"})
            else:
                assert atom["chain"] == "A" and not atom["insertion"].strip()
                retained.append(atom)
        assert sum(item["reason"].startswith("unsupported") for item in removed) == 37
        assert sum(item["reason"].startswith("whole") for item in removed) == 14
        by_id = {identity(atom): atom for atom in retained}
        assert len(by_id) == len(retained)
        prepared_input = args.output_dir / "rebuild-input.pdb"
        headers = [line for line in source_lines if line.startswith(("HEADER", "TITLE ", "COMPND", "SOURCE", "DBREF ", "SEQADV", "SEQRES"))]
        prepared_input.write_text("\n".join(headers + [atom["line"][:16] + " " + atom["line"][17:] for atom in retained] + ["TER", "END"]) + "\n")
        write_json(args.output_dir / "original-atom-decisions.json", {"source_sha256": sha(original), "policy_sha256": sha(args.policy), "removed": removed, "retained_original_serials": [atom["serial"] for atom in retained]})
        progress("input_decisions_frozen", retained_heavy_atoms=len(retained), removed_records=len(removed))
        fixer = PDBFixer(filename=str(prepared_input), platform=platform)
        fixer.findMissingResidues()
        missing_residues = [{"chain_index": key[0], "insertion_index": key[1], "residues": value} for key, value in sorted(fixer.missingResidues.items())]
        assert sum(len(item["residues"]) for item in missing_residues) == 22
        fixer.findNonstandardResidues()
        assert not fixer.nonstandardResidues
        fixer.findMissingAtoms()
        missing_atoms = [{"chain": residue.chain.id, "number": residue.id, "residue": residue.name, "atoms": [atom.name for atom in atoms]} for residue, atoms in fixer.missingAtoms.items()]
        write_json(args.output_dir / "rebuild-manifest.json", {"missing_residues": missing_residues, "missing_atoms": missing_atoms, "sequence": sequence, "seed": seed})
        progress("missing_atoms_identified", missing_residues=missing_residues, missing_heavy_atoms=sum(len(item["atoms"]) for item in missing_atoms))
        fixer.addMissingAtoms(seed=seed)
        residues = list(fixer.topology.residues())
        assert [residue.name for residue in residues] == sequence
        for index, residue in enumerate(residues):
            residue.id = str(666 + index)
            residue.chain.id = "A"
        heavy_positions = fixer.positions.value_in_unit(unit.angstrom)
        maximum_drift = 0.0
        for atom in fixer.topology.atoms():
            key = (atom.residue.chain.id, int(atom.residue.id), atom.residue.name, atom.name)
            if key in by_id:
                maximum_drift = max(maximum_drift, float(np.linalg.norm(np.array(heavy_positions[atom.index]) - np.array(by_id[key]["xyz"]))))
        assert maximum_drift <= policy["geometry"]["anchor_coordinate_tolerance_angstrom"]
        progress("heavy_atoms_rebuilt", residues=len(residues), heavy_atoms=fixer.topology.getNumAtoms(), retained_atom_maximum_drift_angstrom=maximum_drift)
        forcefield = app.ForceField("amber14/protein.ff14SB.xml")
        modeller = app.Modeller(fixer.topology, fixer.positions)
        variants = ["CYS" if residue.id == "773" else None for residue in modeller.topology.residues()]
        selected_variants = modeller.addHydrogens(forcefield, pH=policy["hydrogens"]["pH_assumption"], variants=variants, platform=platform)
        system = forcefield.createSystem(modeller.topology, nonbondedMethod=app.NoCutoff, constraints=None, rigidWater=False)
        assert system.getNumParticles() == modeller.topology.getNumAtoms()
        progress("hydrogens_added_and_forcefield_matched", atoms=system.getNumParticles())
        raw = args.output_dir / "openmm-output.pdb"
        with raw.open("x") as handle:
            app.PDBFile.writeFile(modeller.topology, modeller.positions, handle, keepIds=True)
        final_atoms = read_atoms(raw)
        assert len(final_atoms) == modeller.topology.getNumAtoms()
        original_by_id = {}
        for atom in source_atoms:
            original_by_id.setdefault(identity(atom), []).append(atom)
        changes, final_lines = [], []
        for atom in final_atoms:
            key = identity(atom)
            old = by_id.get(key)
            provenance = "retained_observed_coordinate" if old else "rebuilt_zero_occupancy_atom" if any(value["occupancy"] == 0 for value in original_by_id.get(key, [])) else "added_hydrogen" if atom["element"] == "H" else "modeled_missing_heavy_atom"
            occupancy = old["occupancy"] if old else 0.0
            bfactor = old["bfactor"] if old else 0.0
            final_lines.append(atom["line"][:54] + f"{occupancy:6.2f}{bfactor:6.2f}" + atom["line"][66:])
            changes.append({"output_serial": atom["serial"], "identity": list(key), "element": atom["element"], "coordinate_origin": provenance,
                            "original_serial": old["serial"] if old else None, "replaced_original_serials": [value["serial"] for value in original_by_id.get(key, [])] if not old else [],
                            "original_occupancy": old["occupancy"] if old else None, "output_occupancy": occupancy,
                            "original_xyz": old["xyz"] if old else None, "output_xyz": atom["xyz"],
                            "displacement_angstrom": float(np.linalg.norm(np.array(atom["xyz"]) - np.array(old["xyz"]))) if old else None})
        final = args.output_dir / "1M17-prepared-model-v2.pdb"
        remarks = ["REMARK 900 COMPUTATIONAL PREPARATION OF SOURCE 1M17; NOT A NEW EXPERIMENT", "REMARK 900 MODELED ATOMS HAVE OCCUPANCY 0.00 AND B-FACTOR 0.00", "REMARK 900 SEE ATOM-MAPPING.JSON FOR EXPERIMENTAL VERSUS MODELED PROVENANCE", "REMARK 900 ALTLOC A IS A DETERMINISTIC CHOICE, NOT A PHYSICAL PREFERENCE"]
        final.write_text("\n".join(headers + remarks + final_lines + ["TER", "END"]) + "\n")
        atoms = list(modeller.topology.atoms())
        topology = {"atoms": [{"index": atom.index, "name": atom.name, "element": atom.element.symbol, "chain": atom.residue.chain.id, "residue": atom.residue.name, "number": int(atom.residue.id)} for atom in atoms],
                    "bonds": [[bond[0].index, bond[1].index] for bond in modeller.topology.bonds()], "selected_protonation_variants": selected_variants}
        write_json(args.output_dir / "topology.json", topology)
        write_json(args.output_dir / "atom-mapping.json", {"schema": "drug-design-prepared-receptor-atom-map-v2", "source_sha256": sha(original), "prepared_sha256": sha(final), "policy_sha256": sha(args.policy), "atoms": changes})
        forcefields = [Path(app.__file__).parent / "data/amber14/protein.ff14SB.xml", Path(app.__file__).parent / "data/hydrogens.xml"]
        ledger["forcefield_files"] = [{"path": str(path), "sha256": sha(path)} for path in forcefields]
        ledger["status"] = "prepared_pending_independent_audit"
        ledger["artifacts"] = {path.name: sha(path) for path in args.output_dir.iterdir() if path.is_file() and path.name != "progress.json"}
        progress("prepared_proposal_written", prepared_sha256=sha(final), modeled_atoms=sum(item["coordinate_origin"] != "retained_observed_coordinate" for item in changes))
    except BaseException as error:
        ledger["status"] = "preparation_failed"
        ledger["error"] = {"type": type(error).__name__, "message": str(error), "traceback": traceback.format_exc()}
        raise
    finally:
        signal.alarm(0)
        ledger["elapsed_wall_seconds"] = time.monotonic() - start
        ledger["process_cpu_seconds"] = time.process_time() - cpu
        ledger["cpu_core_hours"] = ledger["process_cpu_seconds"] / 3600
        ledger["peak_rss_bytes"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        ledger["source_unchanged"] = sha(original) == policy["source"]["sha256"]
        write_json(args.output_dir / "preparation-receipt.json", ledger)
        print(json.dumps({key: ledger[key] for key in ("status", "elapsed_wall_seconds", "process_cpu_seconds", "cpu_core_hours", "source_unchanged")}), flush=True)


if __name__ == "__main__":
    main()
