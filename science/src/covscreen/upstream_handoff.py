"""Prepare upstream Covalentizer products for reaction-routed docking.

This exports 2D ligand inputs, never claimed bound conformations. Sulfonamide
products retain their actual chemistry label instead of being called sulfones.
"""
from pathlib import Path
import json
import math
import hashlib
import subprocess
import sys

import pandas as pd
from rdkit import Chem
from rdkit.Chem import rdDepictor, QED, Descriptors, Crippen

from .jobs import sha256_file
from .upstream_parent_mapping import parent_mapping_evidence


# Reaction atom is the final query atom. Labels correspond to upstream restore
# rules; routing labels are inputs to engines, not proof of engine support.
WARHEADS = {
    "acrylamide": ("[NX3][CX3](=[OX1])[CX3]=[CX3]", "Michael Addition", "Michael Addition"),
    "chloroacetamide": ("[NX3][CX3](=[OX1])[CH2]Cl", "Nucleophilic Substitution", "Nucleophilic Substitution"),
    "vinyl_sulfonamide": ("[NX3][SX4](=[OX1])(=[OX1])[CX3]=[CX3]", "Michael Addition", "Michael Addition"),
    # CarsiDock's triple-bond branch is nitrile-only. Activated alkynes are
    # implemented in its Michael Addition branch (cov_prepare.mol_edit).
    "propiolamide": ("[NX3][CX3](=[OX1])[CX2]#[CX2H1]", "Michael Addition", "Conjugate Addition to Alkyne (carbonyl activated)"),
}


def identify_warhead(mol: Chem.Mol) -> dict:
    matches = []
    for name, (smarts, carsi, covdock) in WARHEADS.items():
        query = Chem.MolFromSmarts(smarts)
        for match in mol.GetSubstructMatches(query):
            electrophile = match[-2] if name == "chloroacetamide" else match[-1]
            matches.append((name, electrophile, carsi, covdock))
    matches = sorted(set(matches))
    if len(matches) != 1:
        raise ValueError(f"expected exactly one supported warhead, observed {len(matches)}")
    name, index, carsi, covdock = matches[0]
    return {"warhead_class": name, "reaction_atom_index": index,
            "carsidock_reaction_type": carsi, "covdock_reaction_type": covdock}


def bind_full_parent_atoms(parent: Chem.Mol, product: Chem.Mol) -> tuple[Chem.Mol, dict]:
    """Bind an unambiguous full-parent graph to the existing atom-map contract.

    This is structural correspondence, not upstream reaction ancestry. No
    generation, geometric ranking, campaign registration or admission occurs.
    Existing caller-supplied atom maps are discarded and reconstructed.
    """
    parent = Chem.RemoveHs(Chem.Mol(parent))
    product = Chem.RemoveHs(Chem.Mol(product))
    for mol in (parent, product):
        for atom in mol.GetAtoms():
            atom.SetAtomMapNum(0)
        Chem.SanitizeMol(mol)
        if (len(Chem.GetMolFrags(mol)) != 1 or any(
                atom.GetAtomicNum() in (0, 14) or atom.GetNumRadicalElectrons()
                for atom in mol.GetAtoms())):
            raise ValueError('Full-parent binding requires a valid single molecular graph')
    if not 0 < parent.GetNumAtoms() < 800:
        raise ValueError('Parent atom count exceeds the reserved atom-map range')
    # Two matches suffice to detect ambiguity; never silently choose one
    # symmetry mapping for downstream scaffold RMSD or interaction retention.
    matches = product.GetSubstructMatches(parent, useChirality=True,
                                         uniquify=False, maxMatches=2)
    if not matches:
        raise ValueError('Full-parent correspondence must be present and unambiguous: '
                         'no chirality-aware full-parent match; retained identity is unverified')
    if len(matches) > 1:
        raise ValueError('Full-parent correspondence must be present and unambiguous: '
                         'multiple graph mappings; atom correspondence is unresolved, '
                         'not evidence of chemical infeasibility')
    mapping = dict(enumerate(matches[0]))
    retained = set(mapping.values())
    for index, target in mapping.items():
        before, after = parent.GetAtomWithIdx(index), product.GetAtomWithIdx(target)
        if (before.GetAtomicNum(), before.GetFormalCharge(), before.GetIsotope(), before.GetIsAromatic()) != (
                after.GetAtomicNum(), after.GetFormalCharge(), after.GetIsotope(), after.GetIsAromatic()):
            raise ValueError('Retained parent atom chemistry changed')
    if sum(b.GetBeginAtomIdx() in retained and b.GetEndAtomIdx() in retained
           for b in product.GetBonds()) != parent.GetNumBonds():
        raise ValueError('Retained parent connectivity changed')
    annotation = identify_warhead(product)
    if annotation['warhead_class'] != 'acrylamide':
        raise ValueError('Bounded full-parent adapter currently requires acrylamide')
    warhead = product.GetSubstructMatches(Chem.MolFromSmarts(WARHEADS['acrylamide'][0]))[0]
    if retained.intersection(warhead):
        raise ValueError('New warhead must not overlap the retained parent graph')
    boundaries = [(bond.GetBeginAtomIdx(), bond.GetEndAtomIdx())
                  for bond in product.GetBonds()
                  if (bond.GetBeginAtomIdx() in retained) != (bond.GetEndAtomIdx() in retained)]
    if len(boundaries) != 1:
        raise ValueError('Full-parent addition requires exactly one attachment bond')
    first, second = boundaries[0]
    anchor, added = (first, second) if first in retained else (second, first)
    bond = product.GetBondBetweenAtoms(anchor, added)
    if product.GetAtomWithIdx(anchor).GetSymbol() != 'C' or bond.GetBondType() != Chem.BondType.SINGLE:
        raise ValueError('Bounded full-parent addition requires a carbon single-bond anchor')
    linker = set(range(product.GetNumAtoms())) - retained - set(warhead)
    if len(linker) > 2:
        raise ValueError('Added linker exceeds the bounded two-carbon contract')
    path = Chem.GetShortestPath(product, anchor, warhead[0])
    if (set(path[1:-1]) != linker or any(
            product.GetAtomWithIdx(index).GetSymbol() != 'C'
            or product.GetAtomWithIdx(index).IsInRing()
            or product.GetAtomWithIdx(index).GetHybridization() != Chem.HybridizationType.SP3
            for index in linker) or any(
            product.GetBondBetweenAtoms(first, second).GetBondType() != Chem.BondType.SINGLE
            for first, second in zip(path, path[1:]))):
        raise ValueError('Added linker must be a linear saturated carbon chain to the warhead nitrogen')
    for index, target in mapping.items():
        product.GetAtomWithIdx(target).SetAtomMapNum(100 + index)
    for number, target in zip((902, 903, 904, 905, 906), warhead):
        product.GetAtomWithIdx(target).SetAtomMapNum(number)
    linker_mapping = dict(zip((907, 908), path[1:-1]))
    for number, target in linker_mapping.items():
        product.GetAtomWithIdx(target).SetAtomMapNum(number)
    product.RemoveAllConformers()
    return product, {
        'method': 'unique chirality-aware full-parent graph correspondence',
        'parent_atom_index_to_product_index': mapping,
        'warhead_atom_map_to_product_index': dict(zip((902, 903, 904, 905, 906), warhead)),
        'attachment_parent_atom_index': next(index for index, target in mapping.items() if target == anchor),
        'linker_name': ('direct', 'methylene', 'ethylene')[len(linker)],
        'linker_heavy_atom_count': len(linker),
        'linker_atom_map_to_product_index': linker_mapping,
        'removed_parent_atom_indices': [],
        'reaction_atom_index': annotation['reaction_atom_index'],
        'mapping_ambiguous': False,
        'reaction_ancestry_verified': False,
        'pose_assessment': 'NOT_RUN',
    }


def upstream_candidate_record(parent, campaign, row: dict, *, manifest_sha256: str,
                              input_sha256: dict):
    """Adapt one manifest row to the existing VS/LO identity contract.

    Caller must verify the manifest and source files before using this adapter.
    Here chemical identity and graph protection are recomputed independently;
    geometry, budget selection and execution admission remain separate gates.
    """
    from .rdkit_covalentizer import (CovalentizedCandidate, UpstreamCovalentizerSource,
                                    evaluate_chemistry_quality)

    def canonical(molecule):
        molecule = Chem.RemoveHs(Chem.Mol(molecule))
        for atom in molecule.GetAtoms():
            atom.SetAtomMapNum(0)
        return Chem.MolToSmiles(molecule)

    product = Chem.MolFromSmiles(row['smiles'])
    source_parent = Chem.MolFromSmiles(row['input_smiles'])
    if product is None or source_parent is None or canonical(source_parent) != canonical(parent):
        raise ValueError('Upstream input must match the full campaign parent identity')
    structure = canonical(product)
    if hashlib.sha256(structure.encode()).hexdigest() != row['structure_sha256']:
        raise ValueError('Upstream structure digest does not match the product')
    bound, mapping = bind_full_parent_atoms(parent, product)
    gate = evaluate_chemistry_quality(bound, parent, campaign)
    if gate.status != 'PASS':
        raise ValueError('Upstream candidate fails campaign chemistry: ' + '; '.join(gate.failure_reasons))
    source = UpstreamCovalentizerSource(
        enumeration_manifest_sha256=manifest_sha256,
        source_product_sha256=row['source_product_sha256'],
        structure_sha256=row['structure_sha256'], input_sha256=input_sha256,
        enumeration_candidate_id=row['candidate_id'], upstream_id=row['upstream_id'],
        input_id=row['input_id'], mapping_method=mapping['method'])
    identity = hashlib.sha256(json.dumps([campaign.campaign_id, manifest_sha256,
        row['candidate_id'], structure], separators=(',', ':')).encode()).hexdigest()[:16]
    site = f"upstream-add-C{mapping['attachment_parent_atom_index']}"
    candidate = CovalentizedCandidate(
        candidate_id=f'{campaign.target.symbol.lower()}-{identity}',
        campaign_id=campaign.campaign_id, parent_ligand_id=campaign.parent_pose.ligand_component_id,
        parent_pose_id=campaign.parent_pose.pdb_id, exit_vector_id=site,
        transformation_name='upstream_full_parent_acrylamide_addition',
        rule_id='upstream-CovUtils-install-and-restore', warhead_name='acrylamide',
        linker_name=mapping['linker_name'], linker_heavy_atom_count=mapping['linker_heavy_atom_count'],
        reaction_class='thiol Michael addition', covalent_mode='irreversible',
        canonical_smiles=structure,
        parent_atom_index_to_product_index=mapping['parent_atom_index_to_product_index'],
        warhead_atom_map_to_product_index={**mapping['warhead_atom_map_to_product_index'],
                                         **mapping['linker_atom_map_to_product_index']},
        removed_parent_atom_indices=[], quality_gate=gate,
        provenance='upstream-covalentizer-full-parent-v1', upstream_source=source)
    for key in ('candidate_id', 'campaign_id', 'parent_ligand_id', 'exit_vector_id',
                'transformation_name', 'warhead_name', 'linker_name', 'provenance'):
        bound.SetProp(key, getattr(candidate, key))
    bound.SetProp('reaction_atom_index', str(gate.reaction_atom_index))
    bound.SetProp('chemistry_gate_status', gate.status)
    bound.SetProp('enumeration_manifest_sha256', manifest_sha256)
    return candidate, bound


def full_parent_attachment_geometry(parent: Chem.Mol, anchor_index: int,
                                    target_xyz, *, max_distance_angstrom: float,
                                    max_vector_angle_degrees: float) -> dict:
    """Audit a C-H addition hypothesis without inventing a leaving heavy atom.

    Parent heavy-atom coordinates are retained. RDKit hydrogen coordinates are
    constructed hypotheses, not experimental observations or product poses.
    All available C-H directions are reported; passing any direction is only
    a permissive attachment prefilter, never a pose or interaction gate.
    """
    if (not math.isfinite(max_distance_angstrom) or max_distance_angstrom <= 0
            or not math.isfinite(max_vector_angle_degrees)
            or not 0 <= max_vector_angle_degrees <= 180):
        raise ValueError('Invalid attachment geometry limits')
    if (len(target_xyz) != 3 or not all(math.isfinite(float(x)) for x in target_xyz)
            or parent.GetNumConformers() != 1 or not parent.GetConformer().Is3D()):
        raise ValueError('Attachment geometry requires finite target and one 3D parent pose')
    if not 0 <= anchor_index < parent.GetNumAtoms():
        raise ValueError('Attachment anchor index is outside the parent')
    if parent.GetAtomWithIdx(anchor_index).GetSymbol() != 'C':
        raise ValueError('Attachment anchor must be carbon')
    coordinates = [tuple(parent.GetConformer().GetAtomPosition(i))
                   for i in range(parent.GetNumAtoms())]
    if not all(math.isfinite(x) for point in coordinates for x in point):
        raise ValueError('Parent pose has nonfinite coordinates')
    # Always reconstruct H coordinates so supplied hydrogens cannot silently
    # change the meaning of the reported inferred-direction evidence.
    if any(atom.GetAtomicNum() == 1 for atom in parent.GetAtoms()):
        raise ValueError('Attachment geometry expects the heavy-atom parent contract')
    hydrogenated = Chem.AddHs(Chem.Mol(parent), addCoords=True)
    origin = coordinates[anchor_index]
    target_vector = [float(target_xyz[i]) - origin[i] for i in range(3)]
    distance = math.sqrt(sum(x*x for x in target_vector))
    if distance <= 1e-8:
        raise ValueError('Target overlaps the attachment anchor')
    angles = []
    for atom in hydrogenated.GetAtomWithIdx(anchor_index).GetNeighbors():
        if atom.GetAtomicNum() != 1:
            continue
        point = hydrogenated.GetConformer().GetAtomPosition(atom.GetIdx())
        vector = [point[i] - origin[i] for i in range(3)]
        length = math.sqrt(sum(x*x for x in vector))
        if not math.isfinite(length) or length <= 1e-8:
            raise ValueError('Cannot construct a finite C-H direction')
        cosine = sum(a*b for a, b in zip(vector, target_vector)) / (length * distance)
        angles.append(math.degrees(math.acos(max(-1.0, min(1.0, cosine)))))
    reasons = []
    if not angles:
        reasons.append('Attachment carbon has no replaceable hydrogen')
    if distance > max_distance_angstrom:
        reasons.append('Anchor-target distance exceeds the bounded limit')
    if angles and min(angles) > max_vector_angle_degrees:
        reasons.append('All inferred C-H directions exceed the bounded angle limit')
    return {
        'method': 'parent-pose RDKit inferred C-H directions; all directions reported',
        'attachment_parent_atom_index': anchor_index,
        'distance_to_target_angstrom': distance,
        'inferred_hydrogen_angles_degrees': sorted(angles),
        'direction_is_experimental': False,
        'multiple_direction_hypotheses': len(angles) > 1,
        'max_distance_angstrom': max_distance_angstrom,
        'max_vector_angle_degrees': max_vector_angle_degrees,
        'prefilter_passed': not reasons,
        'rejection_reasons': reasons,
        'pose_assessment': 'NOT_RUN',
        'interaction_retention': 'NOT_ASSESSED',
    }


def select_bounded_additions(reviewed: list[dict], *, max_sites: int,
                             max_candidates: int) -> dict:
    """Budget graph-qualified additions; preserve every geometry disposition.

    This deterministic ordering is a workload allocation rule, not a binding
    score. Chemistry failures belong in the caller's separate audit inventory.
    """
    if (type(max_sites) is not int or type(max_candidates) is not int
            or max_sites < 1 or max_candidates < 1):
        raise ValueError('Site and candidate budgets must be positive integers')
    ids = [row['candidate_id'] for row in reviewed]
    if any(not isinstance(cid, str) or not cid for cid in ids) or len(set(ids)) != len(ids):
        raise ValueError('Candidate IDs must be nonempty and unique')
    eligible, inventory = [], []
    for row in reviewed:
        geometry = row['geometry']
        angles = geometry['inferred_hydrogen_angles_degrees']
        distance = geometry['distance_to_target_angstrom']
        anchor = geometry['attachment_parent_atom_index']
        if (type(anchor) is not int or anchor < 0 or not math.isfinite(distance) or distance <= 0
                or any(not math.isfinite(angle) or not 0 <= angle <= 180 for angle in angles)):
            raise ValueError('Invalid geometry evidence for bounded selection')
        distance_limit = geometry['max_distance_angstrom']
        angle_limit = geometry['max_vector_angle_degrees']
        if (not math.isfinite(distance_limit) or distance_limit <= 0
                or not math.isfinite(angle_limit) or not 0 <= angle_limit <= 180):
            raise ValueError('Invalid geometry limits for bounded selection')
        passes = bool(angles) and distance <= distance_limit and min(angles) <= angle_limit
        if type(geometry['prefilter_passed']) is not bool or geometry['prefilter_passed'] != passes:
            raise ValueError('Geometry decision disagrees with measured evidence')
        if not geometry['prefilter_passed']:
            inventory.append({'candidate_id': row['candidate_id'], 'disposition': 'geometry_rejected',
                              'reasons': geometry['rejection_reasons']})
            continue
        if not angles or geometry['rejection_reasons']:
            raise ValueError('Inconsistent passing geometry evidence')
        eligible.append(row)
    # Site ordering uses source-parent geometry. Stable IDs break ties only;
    # upstream enumeration order never decides which sites enter the budget.
    site_priority = {}
    for row in eligible:
        geometry = row['geometry']
        anchor = geometry['attachment_parent_atom_index']
        priority = (geometry['distance_to_target_angstrom'],
                    min(geometry['inferred_hydrogen_angles_degrees']), anchor)
        if anchor in site_priority and site_priority[anchor] != priority:
            raise ValueError('Products at the same anchor have inconsistent parent geometry')
        site_priority[anchor] = priority
    sites = sorted(site_priority, key=site_priority.get)[:max_sites]
    ordered = sorted(eligible, key=lambda row: (
        site_priority[row['geometry']['attachment_parent_atom_index']], row['candidate_id']))
    selected = []
    for row in ordered:
        anchor = row['geometry']['attachment_parent_atom_index']
        if anchor not in sites:
            disposition = 'deferred_site_budget'
        elif len(selected) >= max_candidates:
            disposition = 'deferred_candidate_budget'
        else:
            disposition = 'selected'
            selected.append(row['candidate_id'])
        inventory.append({'candidate_id': row['candidate_id'], 'disposition': disposition})
    return {'selected_candidate_ids': selected, 'budgeted_parent_atom_indices': sites,
            'max_sites': max_sites, 'max_candidates': max_candidates,
            'inventory': sorted(inventory, key=lambda row: row['candidate_id']),
            'selection_policy': 'parent anchor distance, inferred angle, anchor index, stable candidate ID',
            'selection_is_binding_score': False, 'pose_assessment': 'NOT_RUN'}


def generate_full_parent_candidates(parent, campaign, target_xyz, output_dir: Path,
                                    *, upstream_repo: Path, policy: dict):
    """Run the installed upstream adapter and verify its bounded handoff.

    Used by native design generation, not a standalone approval or docking
    entry point. The caller owns the uncommitted generation directory.
    """
    from .layout import science_script
    repository = upstream_repo.resolve()
    inputs = [repository / 'Code/CovUtils.py', repository / 'Code/numbered_reaction.re',
              repository / 'Scripts/reform_elec.re']
    if any(not path.is_file() for path in inputs):
        raise ValueError('Registered upstream Covalentizer assets are incomplete')
    output_dir = output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=False)
    source = output_dir / 'full_parent.smi'
    molecule = Chem.RemoveHs(Chem.Mol(parent))
    for atom in molecule.GetAtoms():
        atom.SetAtomMapNum(0)
    source.write_text(Chem.MolToSmiles(molecule) + '\tparent\n')
    inputs.append(source)
    expected = {str(path): sha256_file(path) for path in inputs}
    command = [sys.executable, str(science_script('run_upstream_covalentizer_enumeration.py')),
               '--upstream-repo', str(repository), '--fragment-smiles', str(source),
               '--parent-smiles-file', str(source), '--min-parent-fraction', '1.0',
               '--output-dir', str(output_dir)]
    with (output_dir / 'enumeration.log').open('w') as log:
        subprocess.run(command, check=True, timeout=60, stdout=log, stderr=subprocess.STDOUT)
    return review_full_parent_enumeration(output_dir / 'upstream_enumeration_manifest.json',
        parent, campaign, target_xyz, expected_input_sha256=expected, policy=policy)


def review_full_parent_enumeration(manifest_path: Path, parent, campaign, target_xyz,
                                   *, expected_input_sha256: dict, policy: dict):
    """Verify source files and return bounded, independently reviewed records.

    Expected input hashes must be captured by the caller before enumeration,
    not copied from the manifest being verified. This function submits no job.
    """
    manifest_path = manifest_path.resolve()
    manifest_hash = sha256_file(manifest_path)
    payload = json.loads(manifest_path.read_text())
    if (not expected_input_sha256 or payload.get('input_sha256') != expected_input_sha256
            or payload.get('parent_gate_evaluated') is not True):
        raise ValueError('Enumeration is not bound to the expected parent and source inputs')
    for name, expected in expected_input_sha256.items():
        if sha256_file(Path(name)) != expected:
            raise ValueError('Enumeration source input changed: ' + name)
    rows = payload['candidates']
    identifiers = [row['candidate_id'] for row in rows]
    if len(set(identifiers)) != len(identifiers) or payload['candidate_count'] != len(rows):
        raise ValueError('Enumeration candidate count or identity mismatch')
    files = {}
    for row in rows:
        path = Path(row['source_product_file']).resolve()
        if not path.is_relative_to(manifest_path.parent):
            raise ValueError('Product source is outside the enumeration directory')
        if path not in files:
            files[path] = (sha256_file(path), {tuple(line.split()) for line in path.read_text().splitlines()})
        digest, entries = files[path]
        if digest != row['source_product_sha256'] or (row['smiles'], row['upstream_id']) not in entries:
            raise ValueError('Enumeration product source digest or membership mismatch')
    reviews, inventory, products = [], [], {}
    for row in rows:
        source_id = row['candidate_id']
        try:
            molecule = Chem.MolFromSmiles(row['smiles'])
            if molecule is None:
                raise ValueError('Product cannot be parsed')
            annotation = identify_warhead(molecule)
            if annotation['warhead_class'] not in policy['warheads']:
                inventory.append({'candidate_id': source_id, 'disposition': 'outside_warhead_policy',
                                  'warhead_class': annotation['warhead_class']})
                continue
            record, molecule = upstream_candidate_record(parent, campaign, row,
                manifest_sha256=manifest_hash, input_sha256=expected_input_sha256)
            _, mapping = bind_full_parent_atoms(parent, molecule)
            geometry = full_parent_attachment_geometry(parent,
                mapping['attachment_parent_atom_index'], target_xyz,
                max_distance_angstrom=policy['max_distance_angstrom'],
                max_vector_angle_degrees=policy['max_vector_angle_degrees'])
            reviews.append({'candidate_id': source_id, 'geometry': geometry})
            products[source_id] = (record, molecule)
        except ValueError as error:
            inventory.append({'candidate_id': source_id, 'disposition': 'mapping_or_chemistry_rejected',
                              'reason': str(error)})
    selection = select_bounded_additions(reviews, max_sites=policy['max_sites'],
                                         max_candidates=policy['max_candidates'])
    inventory.extend(selection['inventory'])
    selected = [products[cid] for cid in selection['selected_candidate_ids']]
    if len(inventory) != len(rows):
        raise ValueError('Enumeration disposition inventory is incomplete')
    return [pair[0] for pair in selected], [pair[1] for pair in selected], {
        'source_manifest_sha256': manifest_hash, 'source_candidate_count': len(rows),
        'inventory': sorted(inventory, key=lambda row: row['candidate_id']),
        'geometry_reviews': reviews, 'selection': selection,
        'selected_source_to_candidate_id': {cid: products[cid][0].candidate_id
            for cid in selection['selected_candidate_ids']},
        'pose_assessment': 'NOT_RUN', 'interaction_retention': 'NOT_ASSESSED'}


def export_handoff(manifest_path: Path, out: Path, *, target_id: str,
                    site_id: str, parent_id: str) -> dict:
    payload = json.loads(manifest_path.read_text())
    if not payload.get("parent_gate_evaluated"):
        raise ValueError("parent-gated upstream enumeration is required")
    candidates = payload["candidates"]
    ids = [row["candidate_id"] for row in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError("duplicate candidate IDs")
    rows, molecules, failures = [], [], []
    for candidate in candidates:
        try:
            mol = Chem.MolFromSmiles(candidate["smiles"])
            if mol is None or len(Chem.GetMolFrags(mol)) != 1 or any(
                a.GetAtomicNum() in (0, 14) or a.GetNumRadicalElectrons() for a in mol.GetAtoms()
            ):
                raise ValueError("chemical validity check failed")
            mapping = candidate["parent_mapping"]
            evidence = parent_mapping_evidence(
                mapping["parent_smiles"], candidate["input_smiles"], candidate["smiles"],
                min_parent_fraction=mapping["min_parent_fraction"],
                protected_core_smarts=mapping.get("protected_core_smarts"))
            if evidence["status"] != "PASS":
                raise ValueError("parent mapping check failed: " + str(evidence["failure_reasons"]))
            annotation = identify_warhead(mol)
            for atom in mol.GetAtoms():
                atom.SetAtomMapNum(0)
            canonical = Chem.MolToSmiles(mol)
            mol.GetAtomWithIdx(annotation["reaction_atom_index"]).SetAtomMapNum(906)
            index = len(rows)
            cid = candidate["candidate_id"]
            for key, value in {"_Name": cid, "COMPOUND_ID": cid,
                               "INPUT_SDF_INDEX": str(index), "PARENT_ID": parent_id,
                               "WARHEAD_CLASS": annotation["warhead_class"],
                               "COORDINATE_ROLE": "2D_input_not_binding_pose"}.items():
                mol.SetProp(key, value)
            rdDepictor.Compute2DCoords(mol)
            rows.append({"compound_id": cid, "protomer_id": cid,
                         "parent_id": parent_id, "target_id": target_id,
                         "site_id": site_id, "label": "candidate",
                         "input_sdf_index": index, "standardized_smiles": canonical,
                         "electrophilic_atom_map": 906, **annotation,
                         "chemistry_gate": "PASS", "parent_retained_fraction": evidence["parent_retained_fraction"],
                         "property_fit_score": float(QED.qed(mol)),
                         "molecular_weight": float(Descriptors.MolWt(mol)),
                         "clogp": float(Crippen.MolLogP(mol)),
                         "upstream_input_id": candidate["input_id"],
                         "enumeration_manifest_sha256": sha256_file(manifest_path)})
            molecules.append(mol)
        except (ValueError, KeyError) as exc:
            failures.append({"candidate_id": candidate["candidate_id"], "reason": str(exc)})
    out.mkdir(parents=True, exist_ok=True)
    sdf = out / "ligand_first_candidates.sdf"
    with Chem.SDWriter(str(sdf)) as writer:
        for mol in molecules:
            writer.write(mol)
    table = out / "ligand_first_candidates.csv"
    pd.DataFrame(rows).to_csv(table, index=False)
    result = {"status": "prepared" if rows else "no_candidates",
              "generated_count": len(candidates), "accepted_count": len(rows),
              "failure_count": len(failures), "failures": failures,
              "source_manifest_sha256": sha256_file(manifest_path),
              "outputs": {p.name: {"path": str(p), "sha256": sha256_file(p)} for p in (sdf, table)},
              "limitations": ["Inputs prepared only; docking has not run.",
                              "vinyl_sulfonamide is not vinyl_sulfone.",
                              "Engine recognition of reaction atoms must be validated with actual docking."]}
    (out / "upstream_handoff_manifest.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
