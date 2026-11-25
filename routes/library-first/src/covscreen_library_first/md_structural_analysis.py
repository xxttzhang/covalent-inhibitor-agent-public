"""Three-replica descriptive structural analysis for admitted production trajectories."""
from __future__ import annotations

import csv
import json
import math
import struct
from pathlib import Path

from .md_system_conversion import sha
from .md_system_minimization import _regular
from .md_trajectory_audit import _record


def _dcd_frames(path, atoms):
    import numpy as np
    with Path(path).open("rb") as handle:
        marker = handle.read(4)
        if len(marker) != 4:
            raise ValueError("DCD header is missing")
        little, big = struct.unpack("<i", marker)[0], struct.unpack(">i", marker)[0]
        endian = "<" if little == 84 else ">" if big == 84 else None
        if endian is None:
            raise ValueError("Unsupported DCD endian/header")
        handle.seek(0)
        header = _record(handle, endian)
        if header is None or header[:4] != b"CORD":
            raise ValueError("Invalid DCD coordinate header")
        control = struct.unpack(endian + "20i", header[4:])
        declared, start, stride, total = control[:4]
        _record(handle, endian)
        atom_record = _record(handle, endian)
        if atom_record is None or struct.unpack(endian + "i", atom_record)[0] != atoms:
            raise ValueError("DCD atom count mismatch")
        seen = 0
        while True:
            xraw = _record(handle, endian)
            if xraw is None:
                break
            if len(xraw) in {24, 48, 56}:
                xraw = _record(handle, endian)
            yraw, zraw = _record(handle, endian), _record(handle, endian)
            if any(raw is None or len(raw) != atoms * 4 for raw in (xraw, yraw, zraw)):
                raise ValueError("Malformed DCD coordinate frame")
            dtype = np.dtype(endian + "f4")
            xyz = np.column_stack([np.frombuffer(raw, dtype=dtype) for raw in (xraw, yraw, zraw)]).astype(float)
            if not np.isfinite(xyz).all():
                raise ValueError("DCD contains non-finite coordinates")
            seen += 1
            yield seen, xyz
        if seen != declared or not seen or total != start + stride * (seen - 1):
            raise ValueError("DCD frame/step counts are inconsistent")


def _align(frame, reference, indices):
    import numpy as np
    mobile = frame[indices]
    target = reference[indices]
    mc, tc = mobile.mean(0), target.mean(0)
    u, _, vt = np.linalg.svd((mobile - mc).T @ (target - tc))
    rotation = u @ vt
    if np.linalg.det(rotation) < 0:
        u[:, -1] *= -1
        rotation = u @ vt
    return (frame - mc) @ rotation + tc


def _rmsd(a, b):
    import numpy as np
    return float(np.sqrt(np.mean(np.sum((a - b) ** 2, axis=1))))


def _summary(values):
    import numpy as np
    data = np.asarray(values, dtype=float)
    return {"median": float(np.median(data)), "p95": float(np.percentile(data, 95)),
            "min": float(data.min()), "max": float(data.max())}


def analyze_three_replicas(parameter_audit, parameter_audit_sha256, atom_mapping,
                           atom_mapping_sha256, reference_positions,
                           reference_positions_sha256, replicas, output):
    import numpy as np
    parameter_path = _regular(parameter_audit, parameter_audit_sha256, "Parameter audit")
    mapping_path = _regular(atom_mapping, atom_mapping_sha256, "Atom mapping")
    reference_path = _regular(reference_positions, reference_positions_sha256, "Reference positions")
    parameter = json.loads(parameter_path.read_text())
    mapping = json.loads(mapping_path.read_text())
    reference = np.load(reference_path, allow_pickle=False) * 10.0
    atoms = int(parameter["total_atoms"])
    if (parameter.get("atom_mapping_sha256") != atom_mapping_sha256
            or reference.shape != (atoms, 3) or not isinstance(mapping, list)
            or len(mapping) < parameter["solute_atoms"]):
        raise ValueError("Reference coordinates or atom mapping mismatch")
    ligand = np.asarray(sorted(i - 1 for i in parameter["ligand_heavy_atom_indices_1based"]), dtype=int)
    ligand_set = set(ligand.tolist())
    solute = mapping[:parameter["solute_atoms"]]
    protein_heavy = np.asarray([row["index_0based"] for row in solute
        if row["index_0based"] not in ligand_set and row.get("atomic_number", 0) > 1], dtype=int)
    protein_ca = np.asarray([row["index_0based"] for row in solute
        if row["index_0based"] not in ligand_set and row.get("atom_name") == "CA"], dtype=int)
    if len(ligand) < 3 or len(protein_ca) < 20:
        raise ValueError("Analysis selections are incomplete")
    ligand_ref = reference[ligand]
    pocket_ca = protein_ca[(np.linalg.norm(reference[protein_ca, None, :] - ligand_ref[None, :, :], axis=2).min(1) <= 12.0)]
    if len(pocket_ca) < 3:
        raise ValueError("Pocket CA selection is too small")
    covalent_residue = mapping[parameter["covalent_bond"]["atom_indices_1based"][1] - 1]["residue_number"]
    initial_contacts = {}
    for index in protein_heavy:
        row = mapping[int(index)]
        residue = row["residue_number"]
        if residue == covalent_residue:
            continue
        if np.linalg.norm(reference[index] - ligand_ref, axis=1).min() <= 4.5:
            initial_contacts.setdefault(str(residue), []).append(int(index))
    if len(initial_contacts) < 1:
        raise ValueError("No noncovalent reference contacts were found")
    if not isinstance(replicas, list) or len(replicas) != 3:
        raise ValueError("Exactly three trajectory-audit parents are required")
    loaded = []
    for item in replicas:
        folder = Path(item["production_directory"])
        production_path = _regular(folder / "production.json", item["production_manifest_sha256"], "Production manifest")
        audit_path = _regular(item["trajectory_audit"], item["trajectory_audit_sha256"], "Trajectory audit")
        production, audit = json.loads(production_path.read_text()), json.loads(audit_path.read_text())
        if (audit.get("status") != "TRAJECTORY_INTEGRITY_VERIFIED_ANALYSIS_PENDING"
                or audit.get("parents", {}).get("production_manifest_sha256") != item["production_manifest_sha256"]
                or production.get("status") != "PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING"
                or production.get("system_id") != parameter["system_id"]
                or production.get("artifacts", {}).get("trajectory.dcd") != sha(folder / "trajectory.dcd")):
            raise ValueError("Replica has not passed formal trajectory-audit admission")
        loaded.append((production, audit, folder))
    if {p[0].get("replica") for p in loaded} != {1, 2, 3}:
        raise ValueError("Replicas 1, 2 and 3 are all required")
    equilibrium = {p[0].get("parents", {}).get("equilibration_manifest_sha256") for p in loaded}
    if len(equilibrium) != 1 or None in equilibrium:
        raise ValueError("Replicas do not share one admitted equilibration parent")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Structural-analysis output must be new")
    output.mkdir(parents=True)
    rows, replica_summaries = [], []
    pair = [i - 1 for i in parameter["covalent_bond"]["atom_indices_1based"]]
    for production, audit, folder in sorted(loaded, key=lambda x: x[0]["replica"]):
        metrics = {k: [] for k in ("protein_ca_rmsd_A", "pocket_ca_rmsd_A", "ligand_heavy_rmsd_A",
                                    "ligand_com_displacement_A", "contact_retention", "covalent_distance_A")}
        interval = float(production["protocol"]["trajectory_interval_ps"])
        for frame_number, raw in _dcd_frames(folder / "trajectory.dcd", atoms):
            aligned = _align(raw, reference, protein_ca)
            current_contacts = sum(any(np.linalg.norm(aligned[idx] - aligned[ligand], axis=1).min() <= 4.5
                                       for idx in indices) for indices in initial_contacts.values())
            values = {"protein_ca_rmsd_A": _rmsd(aligned[protein_ca], reference[protein_ca]),
                "pocket_ca_rmsd_A": _rmsd(aligned[pocket_ca], reference[pocket_ca]),
                "ligand_heavy_rmsd_A": _rmsd(aligned[ligand], ligand_ref),
                "ligand_com_displacement_A": float(np.linalg.norm(aligned[ligand].mean(0) - ligand_ref.mean(0))),
                "contact_retention": current_contacts / len(initial_contacts),
                "covalent_distance_A": float(np.linalg.norm(aligned[pair[0]] - aligned[pair[1]]))}
            if not all(math.isfinite(v) for v in values.values()):
                raise ValueError("Non-finite structural metric")
            for key, value in values.items(): metrics[key].append(value)
            rows.append({"replica": production["replica"], "frame": frame_number,
                         "time_ps": frame_number * interval, **values})
        replica_summaries.append({"replica": production["replica"], "frames": len(metrics["protein_ca_rmsd_A"]),
            "metrics": {key: _summary(value) for key, value in metrics.items()}})
    with (output / "frame-metrics.csv").open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0])); writer.writeheader(); writer.writerows(rows)
    result = {"schema": "openmm-three-replica-structural-analysis-v1",
        "status": "THREE_REPLICA_STRUCTURAL_ANALYSIS_COMPLETE_REPORT_PENDING",
        "system_id": parameter["system_id"], "replicas": [1, 2, 3],
        "selections": {"ligand_heavy_atoms": len(ligand), "protein_ca_atoms": len(protein_ca),
            "pocket_ca_atoms": len(pocket_ca), "reference_contact_residues": len(initial_contacts),
            "pocket_definition": "reference ligand heavy atoms within 12 A of protein CA",
            "contact_definition": "reference non-Cys protein residue heavy atoms within 4.5 A of ligand heavy atoms"},
        "replica_summaries": replica_summaries,
        "artifacts": {"frame-metrics.csv": sha(output / "frame-metrics.csv")},
        "descriptive_only": True, "convergence_claimed": False, "affinity_claimed": False,
        "kinetics_claimed": False, "screening_decision_made": False}
    (output / "structural-analysis.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
