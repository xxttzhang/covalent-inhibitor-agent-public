"""Read-only measurements for the platform's explicitly approved structural QC.

These are not affinity predictions, topology validation or final decisions.
The released readback controller must verify AF3 confidence/geometry or reopen
CovDock structures with the licensed SDK BEFORE requesting these measurements.
"""
import hashlib
import io
import json
import math
from pathlib import Path

POLICY = {
    "version": "refinement-structural-qc-v1", "scope": "bounded_structural_qc_not_affinity",
    "min_covalent_distance_angstrom": 1.5, "max_covalent_distance_angstrom": 2.2,
    "min_nonbonded_distance_angstrom": 1.2, "covdock_max_strain_kcal_mol": 20,
    "af3_confidence": "report_only_no_affinity_cutoff", "missing_measurements": "reject_candidate",
    "limitation": "full_topology_stereochemistry_and_affinity_not_verified",
}
STRICT_POLICY = {**POLICY, "version": "refinement-structural-qc-v2-provisional",
    "strain_source_field": "r_strain_Local_Strain_Energy",
    "calibration_status": "provisional", "missing_measurements": "withhold_eligibility_not_physical_failure"}


def validate_policy(value):
    # Strict JSON comparison also rejects bool-as-int and unapproved extra fields.
    def canonical(item):
        return json.dumps(item, sort_keys=True, separators=(",", ":"), allow_nan=False)
    if canonical(value) not in (canonical(POLICY), canonical(STRICT_POLICY)):
        raise ValueError("Structural quality policy differs from the frozen implementation")
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def read_measurements(root, evidence, policy):
    from .refinement_output import Files, SPECS, contract
    from .refinement_input import verify_refinement_identities
    from .covdock_gate import STRAIN_COLUMNS
    import pandas as pd

    policy_digest = validate_policy(policy)
    strict = policy == STRICT_POLICY
    engine = evidence["engine"]
    if engine not in SPECS or evidence.get("scientific_quality_verified") is not False:
        raise ValueError("Quality measurements require unaccepted artifact evidence")
    files = Files(root)
    hashes = evidence["artifacts"]
    manifest, table = SPECS[engine]
    def read(name, parse=False):
        if name not in hashes:
            raise ValueError("Quality source is absent from retained artifact witness")
        return files.read(name, hashes[name], parse=parse)
    read(manifest)
    rows = pd.read_parquet(io.BytesIO(read(table, True))).to_dict(orient="records")
    expected = contract(evidence["input_contract"])
    for items in (rows, evidence["candidates"]):
        verify_refinement_identities(items, expected["input_count"], expected["identity_sha256"])
    indexed = {row["input_sdf_index"]: row for row in rows}
    def number(value):
        return float(value) if type(value) in (int, float) and math.isfinite(value) else None
    def boolean(value):
        return value if type(value) is bool else None
    candidates = []
    for candidate in evidence["candidates"]:
        row = indexed[candidate["input_sdf_index"]]
        if row["compound_id"] != candidate["compound_id"] or row["engine_status"] != candidate["engine_status"]:
            raise ValueError("Quality source candidate identity or status differs")
        result = {"input_sdf_index": row["input_sdf_index"], "compound_id": row["compound_id"],
                  "metrics": None, "evidence": []}
        if row["engine_status"] == "FAILED":
            candidates.append(result)
            continue
        if row["engine_status"] != "SUCCESS" or not candidate["structure"]:
            raise ValueError("Quality measurements require actual successful structures")
        structure = candidate["structure"]["path"]
        read(structure)
        result["evidence"] = [manifest, table, structure]
        if engine == "af3":
            job = row["job_name"]
            if Path(structure).name != job + "_model.cif":
                raise ValueError("AF3 quality source differs from structure identity")
            for suffix in ("_summary_confidences.json", "_confidences.json"):
                name = (Path(structure).parent / (job + suffix)).as_posix()
                read(name)
                result["evidence"].append(name)
            if type(row["has_clash"]) not in (int, float, bool) or row["has_clash"] not in (0, 1):
                raise ValueError("AF3 clash observation is missing or invalid")
            result["metrics"] = {"engine": engine, **{key: number(row[key]) for key in ("mpae", "iptm", "ligand_plddt_mean")},
                                 "has_clash": bool(row["has_clash"])}
        else:
            name = files.path(row["covdock_inspection_file"])[1]
            read(name)
            if hashes[name] != row["covdock_inspection_sha256"]:
                raise ValueError("CovDock quality source differs from original inspection witness")
            result["evidence"].append(name)
            result["metrics"] = {"engine": engine,
                **{key: number(row.get(key)) for key in ("target_atom_match_count", "external_target_bond_count",
                    "ligand_heavy_atom_count", "covalent_bond_length_angstrom", "minimum_nonbonded_heavy_atom_distance_angstrom")},
                **{key: boolean(row.get(key)) for key in ("covalent_link_present", "severe_clash")},
                "geometry_inspection_status": row.get("geometry_inspection_status") if isinstance(row.get("geometry_inspection_status"), str) else None,
                "strain_kcal_mol": next((number(row.get(key)) for key in (STRAIN_COLUMNS[:1] if strict else STRAIN_COLUMNS)
                                         if number(row.get(key)) is not None), None)}
            if strict:
                raw = row.get(STRAIN_COLUMNS[0])
                result["metrics"]["strain_provenance"] = {
                    "source_field": STRAIN_COLUMNS[0], "unit": "kcal/mol",
                    "definition": "vendor_local_strain_unmodified",
                    "status": "VALID" if number(raw) is not None else "MISSING" if raw is None or
                        isinstance(raw, float) and math.isnan(raw) else "INVALID_OR_NONFINITE"}
        candidates.append(result)
    for name, checksum in list(files.artifacts.items()):
        files.read(name, checksum)
    return {"version": "refinement-quality-measurements-v1", "policy_digest": policy_digest, "candidates": candidates}


if __name__ == "__main__":
    # Local parity test surface only. Cannot read molecules or invoke an engine.
    print(json.dumps(POLICY, allow_nan=False))
