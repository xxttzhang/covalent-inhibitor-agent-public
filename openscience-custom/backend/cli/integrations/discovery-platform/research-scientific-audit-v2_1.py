"""Independent local audit of a proposed 1M17 preparation; never relaxes it."""
from __future__ import annotations

import os
for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "MKL_NUM_THREADS"):
    os.environ[key] = "1"

import argparse
import hashlib
import json
from pathlib import Path
import resource
import signal
import time

import numpy as np
from rdkit import Chem, rdBase
from rdkit.Chem import rdDistGeom
from scipy.spatial import cKDTree

WORKSPACE = Path(__file__).resolve().parents[5]
RESIDUES = dict(zip("ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL".split(), "ARNDCQEGHILKMFPSTWYV"))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def parse_atoms(path):
    result = []
    for line in Path(path).read_text().splitlines():
        if line.startswith("ATOM  "):
            result.append({"serial": int(line[6:11]), "key": (line[21], int(line[22:26]), line[17:20].strip(), line[12:16].strip()),
                           "element": line[76:78].strip(), "xyz": [float(line[i:i + 8]) for i in (30, 38, 46)], "altloc": line[16].strip(),
                           "occupancy": float(line[54:60]), "bfactor": float(line[60:66])})
    return result


REQUIRED_CHECKS = ["unique_atom_identity", "finite_coordinates", "atom_provenance_and_original_coordinates", "full_deposited_sequence_and_heavy_atom_completeness", "polymer_connectivity", "reactive_site_identity_and_prereaction_hydrogen", "standard_amino_acid_stereochemistry", "intramolecular_distance_bonds", "intramolecular_distance_angles", "intramolecular_distance_internal", "aromatic_planarity"]


def sequence_reference(sequence):
    reference = Chem.MolFromSequence("".join(RESIDUES[name] for name in sequence))
    if reference is None:
        raise ValueError("Cannot construct standard sequence reference")
    # RDKit 2026.09.1 peptide template puts CD1 on CG2. wwPDB ILE CCD puts
    # CD1 on CG1. Correct template labels, never prepared coordinates/topology.
    for atom in reference.GetAtoms():
        info = atom.GetPDBResidueInfo()
        if info.GetResidueName().strip() == "ILE" and info.GetName().strip() in {"CG1", "CG2"}:
            info.SetName(" CG2" if info.GetName().strip() == "CG1" else " CG1")
    return reference


def run(directory, policy_path, audit_policy_path):
    start, cpu = time.monotonic(), time.process_time()
    policy = json.loads(policy_path.read_text())
    audit_policy = json.loads(audit_policy_path.read_text())
    assert audit_policy["schema"] == "drug-design-prepared-receptor-audit-policy-v2"
    assert audit_policy["method_revision"] == "2.1"
    assert sha(__file__) == audit_policy["auditor"]["sha256"]
    assert sha(policy_path) == audit_policy["preparation_policy"]["sha256"]
    assert audit_policy["required_checks"] == REQUIRED_CHECKS
    assert audit_policy["geometry"] == policy["geometry"]
    assert {"rdkit": rdBase.rdkitVersion, "numpy": np.__version__} == audit_policy["packages"]
    prior = directory / "independent-audit-v2.json"
    assert sha(prior) == audit_policy["initial_audit"]["sha256"]
    prior_cpu = json.loads(prior.read_text())["runtime"]["process_cpu_seconds"]
    assert prior_cpu == audit_policy["initial_audit_cpu_seconds"]
    limit = audit_policy["audit_cpu_seconds_limit"]
    resource.setrlimit(resource.RLIMIT_CPU, (limit - 10, limit))
    signal.alarm(limit)
    source = WORKSPACE / policy["source"]["path"]
    original = parse_atoms(source)
    assert sha(source) == policy["source"]["sha256"]
    assert policy["geometry"] == json.loads((WORKSPACE / policy["v1_manifest"]["path"]).read_text())["geometry"]
    assert sha(WORKSPACE / policy["v1_manifest"]["path"]) == policy["v1_manifest"]["sha256"]
    prepared = directory / "1M17-prepared-model-v2.pdb"
    atoms = parse_atoms(prepared)
    mapping = json.loads((directory / "atom-mapping.json").read_text())
    topology = json.loads((directory / "topology.json").read_text())
    receipt = json.loads((directory / "preparation-receipt.json").read_text())
    assert mapping["source_sha256"] == sha(source) and mapping["prepared_sha256"] == sha(prepared) and mapping["policy_sha256"] == sha(policy_path)
    assert receipt["policy_sha256"] == sha(policy_path) and receipt["status"] == "prepared_pending_independent_audit"
    for filename, digest in receipt["artifacts"].items():
        assert sha(directory / filename) == digest
    checks = []
    def check(name, passed, detail, **measurements):
        checks.append({"id": name, "status": "pass" if passed else "fail", "detail": detail, "measurements": measurements})
    by_key = {atom["key"]: atom for atom in atoms}
    map_by_key = {tuple(atom["identity"]): atom for atom in mapping["atoms"]}
    check("unique_atom_identity", len(by_key) == len(atoms) == len(map_by_key) == len(topology["atoms"]), "PDB, atom map and topology have a unique complete denominator")
    check("finite_coordinates", all(np.isfinite(atom["xyz"]).all() and not atom["altloc"] for atom in atoms), "Finite single-conformer coordinates")
    originals = {atom["key"]: atom for atom in original if atom["occupancy"] > 0 and atom["altloc"] in ("", "A")}
    source_zeros = {atom["key"] for atom in original if atom["occupancy"] == 0}
    violations = []
    for atom in atoms:
        mapped = map_by_key[atom["key"]]
        retained = mapped["coordinate_origin"] == "retained_observed_coordinate"
        old = originals.get(atom["key"])
        if atom["xyz"] != mapped["output_xyz"] or atom["occupancy"] != mapped["output_occupancy"]:
            violations.append([atom["serial"], "mapping_mismatch"])
        if retained:
            if not old or old["serial"] != mapped["original_serial"] or atom["occupancy"] != old["occupancy"] or atom["bfactor"] != old["bfactor"] or np.max(np.abs(np.array(atom["xyz"]) - np.array(old["xyz"]))) > policy["geometry"]["anchor_coordinate_tolerance_angstrom"]:
                violations.append([atom["serial"], "retained_source_changed"])
        elif atom["occupancy"] != 0 or atom["bfactor"] != 0:
            violations.append([atom["serial"], "modeled_atom_misrepresented_as_observed"])
    missing_retained = sorted(set(originals) - set(by_key))
    missing_rebuilt = sorted(source_zeros - set(by_key))
    check("atom_provenance_and_original_coordinates", not violations and not missing_retained and not missing_rebuilt, "Observed coordinates preserved; every zero-occupancy atom rebuilt and every modeled atom marked unobserved", violations=violations, missing_retained=missing_retained, missing_rebuilt=missing_rebuilt)
    sequence = [word for line in source.read_text().splitlines() if line.startswith("SEQRES") and line[11] == "A" for word in line[19:70].split()]
    reference = sequence_reference(sequence)
    expected = []
    for atom in reference.GetAtoms():
        info = atom.GetPDBResidueInfo()
        expected.append(("A", info.GetResidueNumber() + 665, info.GetResidueName().strip(), info.GetName().strip()))
    heavy = {key for key, atom in by_key.items() if atom["element"] != "H"}
    check("full_deposited_sequence_and_heavy_atom_completeness", set(expected) == heavy and len(sequence) == 333, "All 333 SEQRES residues including original cloning segment and unresolved termini/loop; exact standard amino-acid heavy atoms", expected_atoms=len(expected), actual_atoms=len(heavy), missing=sorted(set(expected) - heavy), unexpected=sorted(heavy - set(expected)))
    if set(expected) != heavy:
        return finish(directory, policy_path, policy, prepared, checks, start, cpu, receipt, audit_policy_path, audit_policy, {})
    index = {key: position for position, key in enumerate(expected)}
    topology_keys = [(atom["chain"], atom["number"], atom["residue"], atom["name"]) for atom in topology["atoms"]]
    actual_bonds = {tuple(sorted((index[topology_keys[first]], index[topology_keys[second]]))) for first, second in topology["bonds"] if topology_keys[first] in index and topology_keys[second] in index}
    expected_bonds = {tuple(sorted((bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()))) for bond in reference.GetBonds()}
    check("polymer_connectivity", actual_bonds == expected_bonds, "Exact heavy-atom peptide/sidechain bond graph against full standard sequence; includes all rebuilt loop junctions", missing_bonds=sorted(expected_bonds - actual_bonds), unexpected_bonds=sorted(actual_bonds - expected_bonds))
    target = ("A", 773, "CYS", "SG")
    sg_index = topology_keys.index(target)
    sg_neighbors = [topology["atoms"][second if first == sg_index else first] for first, second in topology["bonds"] if first == sg_index or second == sg_index]
    check("reactive_site_identity_and_prereaction_hydrogen", target in originals and {atom["name"] for atom in sg_neighbors} == {"CB", "HG"}, "Unmoved original CYS773 SG with explicitly neutral pre-reaction thiol; not a claimed reactive-state population", neighbors=sg_neighbors, pH_assumption=policy["hydrogens"]["pH_assumption"])
    xyz = np.array([by_key[key]["xyz"] for key in expected])
    conformer = Chem.Conformer(reference.GetNumAtoms())
    conformer.Set3D(True)
    for position, point in enumerate(xyz):
        conformer.SetAtomPosition(position, point)
    reference.AddConformer(conformer)
    Chem.AssignStereochemistry(reference, cleanIt=True, force=True)
    stereochemistry = {atom.GetIdx(): atom.GetProp("_CIPCode") for atom in reference.GetAtoms() if atom.HasProp("_CIPCode")}
    actual = Chem.Mol(reference)
    Chem.AssignStereochemistryFrom3D(actual, replaceExistingTags=True)
    Chem.AssignStereochemistry(actual, cleanIt=True, force=True)
    inverted = [{"atom": expected[position], "expected": code, "actual": actual.GetAtomWithIdx(position).GetProp("_CIPCode") if actual.GetAtomWithIdx(position).HasProp("_CIPCode") else None} for position, code in stereochemistry.items() if not actual.GetAtomWithIdx(position).HasProp("_CIPCode") or actual.GetAtomWithIdx(position).GetProp("_CIPCode") != code]
    check("standard_amino_acid_stereochemistry", not inverted, "Coordinate-derived stereochemistry against the standard L-amino-acid sequence template", checked=len(stereochemistry), violations=inverted)
    geometry = policy["geometry"]
    adjacency = {atom.GetIdx(): [neighbor.GetIdx() for neighbor in atom.GetNeighbors()] for atom in reference.GetAtoms()}
    bounds = rdDistGeom.GetMoleculeBoundsMatrix(reference, set15bounds=True, scaleVDW=True, doTriangleSmoothing=True, useMacrocycle14config=False)
    graph_distances = Chem.GetDistanceMatrix(reference)
    diagnostics = {"legacy_self_vdw_ratio": {"role": "diagnostic_only_not_acceptance", "reason": "The v1 ratio is intermolecular. Protein 1-4/1-5 contacts require chemical-graph bounds, not an intermolecular cutoff."}}
    for name, graph_mask, tolerance in (("bonds", lambda d: d == 1, geometry["bond_relative_tolerance"]), ("angles", lambda d: d == 2, geometry["angle_relative_tolerance"]), ("internal", lambda d: d > 2, geometry["internal_clash_lower_tolerance"])):
        failures, count, tested, modeled_count = [], 0, 0, 0
        for first in range(len(expected) - 1):
            seconds = np.arange(first + 1, len(expected))
            seconds = seconds[graph_mask(graph_distances[first, seconds])]
            distances = np.linalg.norm(xyz[seconds] - xyz[first], axis=1)
            lower, upper = bounds[seconds, first], bounds[first, seconds]
            invalid = ~np.isfinite(lower) | ~np.isfinite(upper) | ~np.isfinite(distances) | (lower <= 0) | (distances < lower * (1 - tolerance))
            if name != "internal":
                invalid |= distances > upper * (1 + tolerance)
            tested += len(seconds)
            for offset in np.flatnonzero(invalid):
                second = int(seconds[offset])
                modeled = any(map_by_key[expected[position]]["coordinate_origin"] != "retained_observed_coordinate" for position in (first, second))
                count += 1
                modeled_count += modeled
                if len(failures) < 100:
                    failures.append({"atoms": [expected[first], expected[second]], "distance_angstrom": float(distances[offset]), "bounds": [float(lower[offset]), float(upper[offset])], "involves_modeled_atom": modeled})
        check("intramolecular_distance_" + name, count == 0, "Full chemical-graph RDKit bounds with unchanged v1 flags and tolerances; no protein-ligand ratio applied to intramolecular pairs", tested=tested, violation_count=count, modeled_pair_violations=modeled_count, retained_observed_pair_violations=count-modeled_count, first_100_violations=failures)
    flatness = []
    for ring in Chem.GetSymmSSSR(reference):
        if len(ring) in (5, 6) and all(reference.GetAtomWithIdx(position).GetIsAromatic() for position in ring):
            points = xyz[list(ring)]
            centered = points - points.mean(axis=0)
            deviation = float(np.abs(centered @ np.linalg.svd(centered)[2][-1]).max())
            if deviation > geometry["aromatic_plane_tolerance_angstrom"]:
                flatness.append({"atoms": [expected[position] for position in ring], "deviation_angstrom": deviation})
    check("aromatic_planarity", not flatness, "Unchanged v1 aromatic best-fit plane tolerance", violations=flatness)
    table = Chem.GetPeriodicTable()
    radii = [table.GetRvdw(atom.GetAtomicNum()) for atom in reference.GetAtoms()]
    clashes = []
    for first, second in sorted(cKDTree(xyz).query_pairs(2 * max(radii) * geometry["protein_vdw_ratio_minimum"])):
        if second in adjacency[first] or set(adjacency[first]).intersection(adjacency[second]):
            continue
        distance = float(np.linalg.norm(xyz[first] - xyz[second]))
        ratio = distance / (radii[first] + radii[second])
        if ratio < geometry["protein_vdw_ratio_minimum"]:
            clashes.append({"atoms": [expected[first], expected[second]], "distance_angstrom": distance, "vdw_ratio": ratio,
                            "involves_modeled_atom": any(map_by_key[expected[position]]["coordinate_origin"] != "retained_observed_coordinate" for position in (first, second))})
    diagnostics["legacy_self_vdw_ratio"].update(violation_count=len(clashes), first_100_violations=clashes[:100], modeled_pair_violations=sum(item["involves_modeled_atom"] for item in clashes))
    return finish(directory, policy_path, policy, prepared, checks, start, cpu, receipt, audit_policy_path, audit_policy, diagnostics)


def finish(directory, policy_path, policy, prepared, checks, start, cpu, receipt, audit_policy_path, audit_policy, diagnostics):
    signal.alarm(0)
    elapsed, used = time.monotonic() - start, time.process_time() - cpu
    return {"schema": "drug-design-prepared-receptor-audit-v2", "method_revision": "2.1", "audit_policy_sha256": sha(audit_policy_path), "status": "pass" if {item["id"] for item in checks} == set(REQUIRED_CHECKS) and all(item["status"] == "pass" for item in checks) else "fail",
            "purpose": "computational_preparation_geometry_not_experimental_validation", "policy_sha256": sha(policy_path), "prepared_sha256": sha(prepared),
            "source_sha256": policy["source"]["sha256"], "mapping_sha256": sha(directory / "atom-mapping.json"), "evaluator_sha256": sha(__file__),
            "frozen_v1_geometry": policy["geometry"], "thresholds_changed_from_v1": False, "historical_scores_used_to_select_conformer": False,
            "required_checks": REQUIRED_CHECKS, "checks": checks, "diagnostics": diagnostics, "runtime": {"rdkit": rdBase.rdkitVersion, "numpy": np.__version__, "elapsed_wall_seconds": elapsed, "process_cpu_seconds": used,
                                           "preparation_plus_audit_cpu_core_hours": (receipt["process_cpu_seconds"] + audit_policy["initial_audit_cpu_seconds"] + used) / 3600, "new_gpu_compute": False},
            "limitations": ["Missing loop, termini and zero-occupancy atoms were modeled, not experimentally resolved.", "A single deterministic altloc choice and pH 7.0 neutral-thiol assumption do not resolve ensemble/protonation uncertainty.", "Full chemical-graph bounds, chirality and plane checks do not validate loop accuracy or conformational populations; no MolProbity/Ramachandran reference distribution, energy ensemble, solvent dynamics or biological assay was used.", "This v2 evidence cannot be admitted by the frozen v1 contract; a new scientific manifest and explicit host admission are required."]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--directory", type=Path, required=True)
    parser.add_argument("--audit-policy", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.directory, args.policy, args.audit_policy)
    output = args.directory / "independent-audit-v2.1.json"
    with output.open("x") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(output), "sha256": sha(output), "status": result["status"], "checks_failed": [item["id"] for item in result["checks"] if item["status"] == "fail"], "runtime": result["runtime"]}), flush=True)
