"""Bounded local evaluation of frozen EGFR C797 inputs and Library-first poses.

No docking, generation, optimization, external program, network or GPU execution.
The reported outcome covers only the checks in the frozen manifest, not activity.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from pathlib import Path

import numpy as np
import yaml
from rdkit import Chem, rdBase
from rdkit.Chem import Descriptors, Lipinski, rdDistGeom, rdMolDescriptors

VERSION = "egfr-library-scientific-evaluator-v1"
WORKSPACE = Path(__file__).resolve().parents[5]
DEFAULT_MANIFEST = Path(__file__).with_name("research-scientific-manifest.json")


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def csv_rows(path):
    with Path(path).open(newline="") as handle:
        return list(csv.DictReader(handle))


def check(name, state, detail, **measurements):
    if state not in {"pass", "fail", "unknown"}:
        raise ValueError("Invalid scientific check state")
    return {"id": name, "status": state, "detail": detail, "measurements": measurements}


def combined(checks):
    states = {item["status"] for item in checks}
    return "fail" if "fail" in states else "unknown" if "unknown" in states or not states else "pass"


def load_manifest(filename=DEFAULT_MANIFEST):
    value = json.loads(Path(filename).read_text())
    if value["schema"] != "drug-design-scientific-manifest-v1" or value["evaluator"]["version"] != VERSION:
        raise ValueError("Unknown scientific manifest/evaluator version")
    if value["evaluator"]["sha256"] != sha(__file__):
        raise ValueError("Scientific evaluator differs from frozen manifest")
    actual = {"rdkit": rdBase.rdkitVersion, "numpy": np.__version__, "pyyaml": yaml.__version__}
    if value["evaluator"]["packages"] != actual:
        raise ValueError(f"Scientific evaluator package versions changed: {actual}")
    return value


def receptor_audit(pdb, manifest):
    raw = Path(pdb).read_text("ascii")
    lines = raw.splitlines()
    binding = manifest["target"]
    checks = [check("receptor_bytes", "pass" if sha(pdb) == binding["receptor_sha256"] else "fail", "Exact frozen receptor bytes", sha256=sha(pdb))]
    headers = [line[62:66].strip() for line in lines if line.startswith("HEADER")]
    dbrefs = [line for line in lines if line.startswith("DBREF ") and line[12] == "A"]
    matches = [line for line in dbrefs if line[7:11] == "1M17" and line[26:32].strip() == "UNP" and line[33:41].strip() == "P00533"
               and (int(line[14:18]), int(line[20:24]), int(line[55:60]), int(line[62:67])) == (671, 998, 695, 1022)
               and not any(line[index].strip() for index in (18, 24, 60, 67))]
    checks.append(check("dbref_mapping", "pass" if headers == ["1M17"] and len(matches) == len(dbrefs) == 1 else "fail",
                        "PDB DBREF cross-reference supports author 773 + 24 = canonical 797; not a fresh SIFTS alignment", records=dbrefs))
    differences = [line for line in lines if line.startswith("SEQADV")]
    expected = [(666, "GLY"), (667, "SER"), (668, "HIS"), (669, "MET"), (670, "ALA")]
    observed = [(int(line[18:22]), line[12:15]) for line in differences if line[16] == "A" and "CLONING ARTIFACT" in line]
    sequence = [word for line in lines if line.startswith("SEQRES") and line[11] == "A" for word in line[19:70].split()]
    checks.append(check("construct_record", "pass" if observed == expected and len(differences) == 5 and len(sequence) == 333
                        and "ORGANISM_TAXID: 9606" in raw and "ENGINEERED: YES" in raw else "fail",
                        "Frozen deposited construct: 333 SEQRES residues including five cloning artifacts; no disease-mutation extrapolation", seqres_length=len(sequence), seqadv=differences))
    if any(line.startswith("MODEL ") for line in lines):
        checks.append(check("single_coordinate_model", "fail", "This evaluator accepts only the frozen single-model receptor"))
    atoms = []
    for line in lines:
        if line.startswith("ATOM  "):
            xyz = [float(line[start:start + 8]) for start in (30, 38, 46)]
            occupancy = float(line[54:60])
            atoms.append({"serial": int(line[6:11]), "name": line[12:16].strip(), "altloc": line[16:17].strip(),
                          "residue": line[17:20].strip(), "chain": line[21], "number": int(line[22:26]), "insertion": line[26].strip(),
                          "xyz": xyz, "occupancy": occupancy, "element": line[76:78].strip()})
    selected = [atom for atom in atoms if atom["chain"] == "A" and atom["number"] == 773]
    complete = {atom["name"] for atom in selected} == {"N", "CA", "C", "O", "CB", "SG"} and len(selected) == 6
    valid = complete and all(atom["residue"] == "CYS" and not atom["altloc"] and not atom["insertion"]
                             and 0 < atom["occupancy"] <= 1 and all(math.isfinite(n) for n in atom["xyz"]) for atom in selected)
    sulfur = [atom for atom in selected if atom["name"] == "SG" and atom["element"] == "S"]
    checks.append(check("unique_reactive_atom", "pass" if valid and len(sulfur) == 1 else "fail", "Unique complete A:CYS773 with occupied finite SG coordinates", atoms=selected))
    ambiguous = [atom for atom in atoms if atom["altloc"] or not all(math.isfinite(n) for n in atom["xyz"]) or not 0 < atom["occupancy"] <= 1]
    checks.append(check("protein_coordinates", "pass" if atoms and not ambiguous else "fail", "No alternate/zero-occupancy/nonfinite protein atoms in this fixed evaluator", total_atoms=len(atoms), unresolved_atoms=ambiguous))
    return {"schema": "drug-design-receptor-audit-v1", "evaluator": VERSION, "evaluator_sha256": sha(__file__),
            "structure_sha256": sha(pdb), "entry_id": "1M17", "uniprot": "P00533", "uniprot_residue": 797,
            "auth_asym_id": "A", "author_residue_id": "773", "reactive_atom": "SG", "taxon": 9606,
            "mapping_method": "frozen_PDB_DBREF_and_SEQADV_plus_coordinate_audit", "mapping_status": combined([item for item in checks if item["id"] in {"receptor_bytes", "dbref_mapping", "construct_record", "unique_reactive_atom"}]), "status": combined(checks), "checks": checks,
            "construct_scope": "Deposited 1M17 EGFR kinase construct only; no mutant efficacy or clinical indication inference",
            "new_compute": False}, atoms


def canonical(mol, stereo=True):
    copied = Chem.Mol(mol)
    for atom in copied.GetAtoms():
        atom.SetAtomMapNum(0)
    return Chem.MolToSmiles(copied, canonical=True, isomericSmiles=stereo)


def candidate_chemistry(row, sdf, config):
    mol = Chem.MolFromSmiles(row["standardized_smiles"])
    if mol is None or sdf is None:
        return [check("molecular_identity", "fail", "SMILES or input SDF did not parse")]
    identity = canonical(mol) == canonical(sdf) and Chem.MolToInchiKey(mol) == row["parent_inchikey"]
    identity = identity and row["compound_id"] == "covindb-" + row["parent_inchikey"].lower()
    chemistry = [check("molecular_identity", "pass" if identity else "fail", "Independent SMILES/SDF/InChIKey/compound identity check")]
    values = {"mw": Descriptors.MolWt(mol), "logp": Descriptors.MolLogP(mol), "tpsa": rdMolDescriptors.CalcTPSA(mol),
              "rotors": Lipinski.NumRotatableBonds(mol), "formal_charge": Chem.GetFormalCharge(mol)}
    limits = config["lead_like"]
    eligible = (limits["min_mw"] <= values["mw"] <= limits["max_mw"] and limits["min_logp"] <= values["logp"] <= limits["max_logp"]
                and values["tpsa"] <= limits["max_tpsa"] and values["rotors"] <= limits["max_rotors"] and abs(values["formal_charge"]) <= limits["max_abs_formal_charge"])
    chemistry.append(check("registered_lead_like", "pass" if eligible and all(math.isfinite(v) for v in values.values()) else "fail", "Unchanged CovInDB recipe ranges independently recomputed", **values))
    matches = mol.GetSubstructMatches(Chem.MolFromSmarts(config["warhead_smarts"]), uniquify=True)
    electrophiles = {match[0] for match in matches}
    chemistry.append(check("declared_warhead", "pass" if len(electrophiles) == 1 and int(row["electrophile_atom_index"]) in electrophiles
                           and row["warhead_class"] == "acrylamide" else "fail", "Declared acrylamide Michael electrophile is unique", matches=len(matches), electrophile_indices=sorted(electrophiles)))
    chemistry.append(check("molecule_sanity", "pass" if len(Chem.GetMolFrags(mol)) == 1 and not any(a.GetNumRadicalElectrons() for a in mol.GetAtoms()) else "fail", "Sanitized connected parent without radical electrons"))
    return chemistry


def recover_parent(pose, reference):
    # A CarsiDock Michael product has a terminal sulfur placeholder attached at beta-C.
    # No graph remapping is accepted unless removing that sulfur and restoring the
    # beta/alpha double bond reproduces the entire original molecular graph.
    query = Chem.MolFromSmarts("[S;D1]-[C]-[C]-[C](=[O])[N]")
    matches = []
    for match in pose.GetSubstructMatches(query):
        sulfur, beta, alpha = match[:3]
        product = Chem.RWMol(pose)
        product.RemoveAtom(sulfur)
        remap = lambda index: index - int(index > sulfur)
        bond = product.GetBondBetweenAtoms(remap(beta), remap(alpha))
        bond.SetBondType(Chem.BondType.DOUBLE)
        restored = product.GetMol()
        try:
            Chem.SanitizeMol(restored)
        except ValueError:
            continue
        if canonical(restored, False) == canonical(reference, False):
            matches.append((sulfur, beta, alpha, restored))
    return matches


def geometry_checks(pose, row, atoms, policy):
    reference = Chem.MolFromSmiles(row["standardized_smiles"])
    if pose is None or reference is None or pose.GetNumConformers() != 1:
        return [check("pose_geometry", "unknown", "Missing parseable single-conformer pose or reference")]
    try:
        Chem.SanitizeMol(pose)
    except ValueError as error:
        return [check("pose_sanity", "fail", str(error))]
    if not pose.GetConformer().Is3D() or not np.isfinite(pose.GetConformer().GetPositions()).all():
        return [check("pose_3d", "fail", "Pose must have finite, explicitly 3D coordinates")]
    recovered = recover_parent(pose, reference)
    if len(recovered) != 1:
        return [check("covalent_product_identity", "unknown", "A unique declared Michael product/parent mapping was not established", mappings=len(recovered))]
    sulfur, beta, alpha, restored = recovered[0]
    checks = [check("covalent_product_identity", "pass", "Unique exact parent graph recovered from the Michael adduct", sulfur_index=sulfur, beta_index=beta, alpha_index=alpha)]
    # Michael addition consumes the electrophilic double bond's E/Z designation.
    # Retain every other originally specified stereochemical constraint.
    parent_match = reference.GetSubstructMatch(restored, useChirality=False)
    if not parent_match:
        return checks + [check("retained_stereochemistry", "unknown", "Restored parent atom mapping unavailable")]
    parent_beta = parent_match[beta - int(beta > sulfur)]
    parent_alpha = parent_match[alpha - int(alpha > sulfur)]
    reference.GetBondBetweenAtoms(parent_beta, parent_alpha).SetStereo(Chem.BondStereo.STEREONONE)
    Chem.AssignStereochemistryFrom3D(restored, replaceExistingTags=True)
    stereo = restored.HasSubstructMatch(reference, useChirality=True)
    checks.append(check("retained_stereochemistry", "pass" if stereo else "fail", "Retained original stereochemistry checked against coordinates; consumed warhead E/Z and newly formed adduct stereocentres have no predeclared configuration"))
    site = {atom["name"]: atom for atom in atoms if atom["chain"] == "A" and atom["number"] == 773}
    if not all(name in site for name in ("SG", "CB", "CA")):
        return checks + [check("covalent_site", "unknown", "Audited target sidechain atoms unavailable")]
    positions = pose.GetConformer().GetPositions()
    displacement = float(np.max(np.abs(positions[sulfur] - np.array(site["SG"]["xyz"]))))
    checks.append(check("covalent_anchor", "pass" if displacement <= policy["anchor_coordinate_tolerance_angstrom"] else "fail", "Sulfur placeholder must coincide with frozen receptor SG within coordinate serialization tolerance", maximum_axis_displacement_angstrom=displacement))
    # Replace the placeholder's implicit H with the actual receptor CB/CA fragment
    # so the C-S-CB cross-boundary angle is checked as covalent geometry, not clash.
    augmented = Chem.RWMol(pose)
    cb = augmented.AddAtom(Chem.Atom("C"))
    ca = augmented.AddAtom(Chem.Atom("C"))
    augmented.AddBond(sulfur, cb, Chem.BondType.SINGLE)
    augmented.AddBond(cb, ca, Chem.BondType.SINGLE)
    mol = augmented.GetMol()
    try:
        Chem.SanitizeMol(mol)
        conformer = mol.GetConformer()
        conformer.SetAtomPosition(cb, site["CB"]["xyz"])
        conformer.SetAtomPosition(ca, site["CA"]["xyz"])
        bounds = rdDistGeom.GetMoleculeBoundsMatrix(mol, set15bounds=True, scaleVDW=True, doTriangleSmoothing=True, useMacrocycle14config=False)
    except (ValueError, RuntimeError) as error:
        return checks + [check("distance_geometry", "unknown", "RDKit could not establish distance bounds: " + str(error))]
    xyz = mol.GetConformer().GetPositions()
    topology = Chem.GetDistanceMatrix(mol)
    violations = {"bond_lengths": [], "bond_angles": [], "internal_clashes": []}
    tested = {name: 0 for name in violations}
    for first in range(mol.GetNumAtoms()):
        if mol.GetAtomWithIdx(first).GetAtomicNum() == 1:
            continue
        for second in range(first + 1, mol.GetNumAtoms()):
            if mol.GetAtomWithIdx(second).GetAtomicNum() == 1:
                continue
            distance = float(np.linalg.norm(xyz[first] - xyz[second]))
            lower, upper = float(bounds[second, first]), float(bounds[first, second])
            if not all(math.isfinite(n) for n in (lower, upper, distance)) or lower <= 0 or upper < lower:
                return checks + [check("distance_geometry", "unknown", "Invalid molecular distance bounds")]
            kind = "bond_lengths" if topology[first, second] == 1 else "bond_angles" if topology[first, second] == 2 else "internal_clashes"
            tolerance = policy["bond_relative_tolerance"] if kind == "bond_lengths" else policy["angle_relative_tolerance"] if kind == "bond_angles" else policy["internal_clash_lower_tolerance"]
            tested[kind] += 1
            if distance < lower * (1 - tolerance) or (kind != "internal_clashes" and distance > upper * (1 + tolerance)):
                violations[kind].append({"atoms": [first, second], "distance": distance, "bounds": [lower, upper]})
    for kind in violations:
        checks.append(check(kind, "pass" if not violations[kind] else "fail", "PoseBusters-style relative RDKit bounds; no conformer relaxation", tested=tested[kind], violations=violations[kind]))
    rings = [list(ring) for ring in Chem.GetSymmSSSR(pose) if len(ring) in (5, 6) and all(pose.GetAtomWithIdx(i).GetIsAromatic() for i in ring)]
    flatness = []
    for ring in rings:
        centered = positions[ring] - positions[ring].mean(axis=0)
        normal = np.linalg.svd(centered)[2][-1]
        flatness.append(float(np.abs(centered @ normal).max()))
    checks.append(check("aromatic_planarity", "pass" if all(value <= policy["aromatic_plane_tolerance_angstrom"] for value in flatness) else "fail", "Heavy-atom aromatic 5/6 rings checked against a best-fit plane", deviations_angstrom=flatness))
    clashes = []
    exclusions = []
    table = Chem.GetPeriodicTable()
    for index, atom in enumerate(pose.GetAtoms()):
        if atom.GetAtomicNum() == 1 or index == sulfur:
            continue
        for protein in atoms:
            if protein["element"] in {"H", "D"}:
                continue
            is_site = protein["chain"] == "A" and protein["number"] == 773
            bond_steps = topology[index, sulfur] if is_site and protein["name"] == "SG" else topology[index, cb] if is_site and protein["name"] == "CB" else topology[index, ca] if is_site and protein["name"] == "CA" else math.inf
            if bond_steps <= 2:
                exclusions.append([index, protein["serial"], int(bond_steps)])
                continue
            radii = table.GetRvdw(atom.GetAtomicNum()) + table.GetRvdw(table.GetAtomicNumber(protein["element"]))
            distance = float(np.linalg.norm(positions[index] - np.array(protein["xyz"])))
            if not math.isfinite(radii) or radii <= 0:
                return checks + [check("protein_clashes", "unknown", "Missing van der Waals radius")]
            if distance < policy["protein_vdw_ratio_minimum"] * radii:
                clashes.append({"ligand_atom": index, "protein_serial": protein["serial"], "distance_angstrom": distance, "vdw_ratio": distance / radii})
    checks.append(check("protein_clashes", "pass" if not clashes else "fail", "Protein heavy atoms; explicitly covalent 1-2/1-3 pairs excluded, waters/reference ligand excluded", clashes=clashes, covalent_exclusions=exclusions))
    return checks


def evaluate(directory, pdb, manifest, manifest_path=DEFAULT_MANIFEST):
    root = Path(directory)
    audit, atoms = receptor_audit(pdb, manifest)
    config_path = WORKSPACE / manifest["recipe"]["path"]
    if sha(config_path) != manifest["recipe"]["sha256"]:
        raise ValueError("Frozen scientific recipe changed")
    config = yaml.safe_load(config_path.read_text())
    paths = {key: root / filename for key, filename in manifest["artifacts"].items()}
    missing = [key for key, filename in paths.items() if not filename.is_file()]
    if missing:
        return {"schema": "drug-design-scientific-evaluation-v1", "status": "unknown", "outcome": "insufficient_evidence", "missing": missing, "receptor": audit, "candidates": []}
    rows = csv_rows(paths["candidates_csv"])
    docked = csv_rows(paths["docking_csv"])
    scored = csv_rows(paths["scoring_csv"])
    inputs = list(Chem.SDMolSupplier(str(paths["candidates_sdf"]), removeHs=False)) if paths["candidates_sdf"].stat().st_size else []
    poses = list(Chem.SDMolSupplier(str(paths["poses_sdf"]), removeHs=False, sanitize=False)) if paths["poses_sdf"].stat().st_size else []
    ids = [row["compound_id"] for row in rows]
    if len(rows) > manifest["scope"]["max_candidates"] or len(ids) != len(set(ids)) or len(inputs) != len(rows):
        raise ValueError("Candidate scope, SDF denominator or identity duplication error")
    for table in (docked, scored):
        found = [row["compound_id"] for row in table]
        if sorted(found) != sorted(ids) or len(found) != len(set(found)):
            raise ValueError("Engine output identity denominator mismatch")
    if sorted(int(row["input_sdf_index"]) for row in rows) != list(range(len(rows))):
        raise ValueError("Input SDF indices must form the complete unique denominator")
    results = []
    for row in rows:
        index = int(row["input_sdf_index"])
        if index < 0 or index >= len(inputs) or inputs[index] is None:
            raise ValueError("Invalid input SDF index")
        sdf = inputs[index]
        if not sdf.HasProp("COMPOUND_ID") or sdf.GetProp("COMPOUND_ID") != row["compound_id"] or not sdf.HasProp("INPUT_SDF_INDEX") or sdf.GetProp("INPUT_SDF_INDEX") != str(index):
            raise ValueError("Input SDF compound ID mismatch")
        checks = candidate_chemistry(row, sdf, config)
        dock = next(item for item in docked if item["compound_id"] == row["compound_id"])
        score = next(item for item in scored if item["compound_id"] == row["compound_id"])
        if any(item.get("standardized_smiles") != row["standardized_smiles"] or item.get("input_sdf_index") != row["input_sdf_index"] for item in (dock, score)):
            raise ValueError("Engine output parent identity or input index mismatch")
        successful = dock["engine_status"] == score["engine_status"] == "SUCCESS"
        numeric = all(item.get("raw_score", "").strip() and math.isfinite(float(item["raw_score"])) for item in (dock, score))
        checks.append(check("finite_engine_scores", "pass" if successful and numeric else "unknown", "Both engine outputs need a finite score; no score threshold is interpreted as affinity"))
        if successful:
            pose_index = int(dock["selected_pose_record_index"])
            if pose_index < 0 or pose_index >= len(poses) or poses[pose_index] is None:
                checks.append(check("pose_identity", "fail", "Selected pose record missing"))
            elif poses[pose_index].GetProp("_Name") != dock["pose_variant_id"]:
                checks.append(check("pose_identity", "fail", "Selected pose variant identity differs from score table"))
            elif audit["status"] != "pass":
                checks.append(check("pose_geometry", "unknown", "Receptor audit failed; geometric conclusion withheld"))
            else:
                checks.extend(geometry_checks(poses[pose_index], row, atoms, manifest["geometry"]))
        else:
            checks.append(check("pose_geometry", "unknown", "Computation did not produce a successful docked and scored candidate"))
        results.append({"compound_id": row["compound_id"], "input_sdf_index": index, "status": combined(checks), "checks": checks})
    counts = {state: sum(item["status"] == state for item in results) for state in ("pass", "fail", "unknown")}
    outcome = "insufficient_evidence" if audit["status"] != "pass" or counts["unknown"] else "objective_met" if counts["pass"] >= manifest["scope"]["minimum_accepted_candidates"] else "no_viable_candidate"
    return {"schema": "drug-design-scientific-evaluation-v1", "evaluator": VERSION, "evaluator_sha256": sha(__file__),
            "manifest_sha256": sha(manifest_path), "origin": "local_artifact_evaluation", "new_remote_compute": False,
            "status": "unknown" if outcome == "insufficient_evidence" else "pass" if outcome == "objective_met" else "fail", "outcome": outcome,
            "receptor": audit, "candidate_count": len(results), "counts": counts, "candidates": results,
            "artifacts": {key: {"path": str(filename.resolve()), "sha256": sha(filename)} for key, filename in paths.items()},
            "accepted_candidate_ids": [item["compound_id"] for item in results if item["status"] == "pass"],
            "conclusion_scope": "computational-shortlist-only",
            "limitations": manifest["limitations"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--pdb", type=Path)
    parser.add_argument("--artifacts", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = load_manifest(args.manifest)
    pdb = args.pdb or WORKSPACE / manifest["target"]["receptor_path"]
    result = evaluate(args.artifacts, pdb, manifest, args.manifest) if args.artifacts else receptor_audit(pdb, manifest)[0]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x") as handle:
        json.dump(result, handle, indent=2, allow_nan=False)
        handle.write("\n")
    print(json.dumps({"output": str(args.output), "sha256": sha(args.output), "status": result["status"], "outcome": result.get("outcome"), "counts": result.get("counts")}, allow_nan=False))


if __name__ == "__main__":
    main()
