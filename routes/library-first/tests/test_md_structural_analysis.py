import hashlib
import json
import struct

import numpy as np

from covscreen_library_first.md_structural_analysis import analyze_three_replicas


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def record(data):
    return struct.pack("<i", len(data)) + data + struct.pack("<i", len(data))


def write_dcd(path, xyz):
    header = b"CORD" + struct.pack("<20i", 1, 1, 1, 1, *([0] * 16))
    title = struct.pack("<i", 0)
    body = record(header) + record(title) + record(struct.pack("<i", len(xyz)))
    for axis in range(3):
        body += record(np.asarray(xyz[:, axis], dtype="<f4").tobytes())
    path.write_bytes(body)


def test_three_replicas_are_analyzed_together(tmp_path):
    rng = np.random.default_rng(7)
    reference_a = rng.normal(size=(23, 3))
    reference_a[:3] = [[0, 0, 0], [1, 0, 0], [0, 1, 0]]
    reference_a[3] = [1.8, 0, 0]
    reference = tmp_path / "reference.npy"
    np.save(reference, reference_a / 10.0, allow_pickle=False)
    mapping = []
    for i in range(23):
        mapping.append({"index_0based": i, "atomic_number": 6,
            "atom_name": "C" if i < 3 else "CA", "residue_number": 900 if i < 3 else i - 2})
    mapping_path = tmp_path / "atom-mapping.json"
    mapping_path.write_text(json.dumps(mapping))
    parameter = {"system_id": "candidate_001", "total_atoms": 23, "solute_atoms": 23,
        "atom_mapping_sha256": sha(mapping_path),
        "ligand_heavy_atom_indices_1based": [1, 2, 3],
        "covalent_bond": {"atom_indices_1based": [1, 4]}}
    parameter_path = tmp_path / "parameterization-audit.json"
    parameter_path.write_text(json.dumps(parameter))
    parents = []
    for replica in (1, 2, 3):
        folder = tmp_path / f"r{replica}"; folder.mkdir()
        write_dcd(folder / "trajectory.dcd", reference_a + replica * 0.01)
        production = {"schema": "openmm-production-replica-artifact-v1",
            "status": "PRODUCTION_REPLICA_COMPLETE_TRAJECTORY_AUDIT_PENDING",
            "system_id": "candidate_001", "replica": replica,
            "parents": {"equilibration_manifest_sha256": "a" * 64},
            "protocol": {"trajectory_interval_ps": 10.0},
            "artifacts": {"trajectory.dcd": sha(folder / "trajectory.dcd")}}
        manifest = folder / "production.json"; manifest.write_text(json.dumps(production))
        audit = {"status": "TRAJECTORY_INTEGRITY_VERIFIED_ANALYSIS_PENDING",
            "parents": {"production_manifest_sha256": sha(manifest)}}
        audit_path = folder / "trajectory-audit.json"; audit_path.write_text(json.dumps(audit))
        parents.append({"production_directory": str(folder),
            "production_manifest_sha256": sha(manifest), "trajectory_audit": str(audit_path),
            "trajectory_audit_sha256": sha(audit_path)})
    result = analyze_three_replicas(parameter_path, sha(parameter_path), mapping_path,
        sha(mapping_path), reference, sha(reference), parents, tmp_path / "output")
    assert result["replicas"] == [1, 2, 3]
    assert len(result["replica_summaries"]) == 3
    assert result["descriptive_only"] is True
    assert result["screening_decision_made"] is False
