"""Bounded Ligand-first design preparation from sealed parent evidence.

No arbitrary file paths, historical campaigns, GPU jobs or scientific overrides.
Graph generation is a distinct state from pose/experimental validation.
"""
from __future__ import annotations

import json
import csv
from pathlib import Path
import re
import shutil
import os
import urllib.request
from uuid import uuid4

from rdkit import Chem, rdBase

from .ligand_parent_evidence import require_latest, validate_comparison, version_lock, verified_target_gene
from .target_hypothesis import digest, folder, read as read_evidence
from .native_case import prepare_native_case
from .ligand_first_inputs import load_campaign, build_parent_pose_mol, read_pdb_atoms
from .ligand_first_site_discovery import discover_covalentizer_sites, exit_vector_specs
from .rdkit_covalentizer import RULES, covalentize_parent, evaluate_chemistry_quality


POLICY = {"version": "minimal-design-v2", "max_sites": 2, "max_candidates": 6,
          "warheads": ["acrylamide"], "max_branch_heavy_atoms": 12,
          "max_distance_angstrom": 12.0, "max_vector_angle_degrees": 100.0,
          "protection": "all_parent_ring_atoms_and_NO_contacts_within_3.5A"}

FULL_PARENT_POLICY = {key: value for key, value in POLICY.items() if key != 'max_branch_heavy_atoms'}
FULL_PARENT_POLICY.update(version='full-parent-addition-v1',
    strategy='upstream-covalentizer-full-parent-v1',
    geometry_method='parent-pose RDKit inferred C-H directions; all directions reported')


def upstream_assets() -> dict:
    configured = os.environ.get('COVALENTIZER', '')
    if not configured:
        raise ValueError('Full-parent design requires the registered COVALENTIZER asset directory')
    repository = Path(configured).resolve()
    files = [repository / name for name in
             ('Code/CovUtils.py', 'Code/numbered_reaction.re', 'Scripts/reform_elec.re')]
    if any(not path.is_file() for path in files):
        raise ValueError('Registered Covalentizer assets are incomplete')
    return {'repository': str(repository),
            'files': {str(path.relative_to(repository)): digest(path) for path in files}}


def write(path: Path, data: dict):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def design_folder(root: Path, design_id: str) -> Path:
    if not re.fullmatch(r"design-[a-f0-9]{24}", design_id):
        raise ValueError("Invalid design ID")
    return root / "results/ligand-designs" / design_id


def seal(out: Path):
    write(out / "manifest.json", {"files": {str(p.relative_to(out)): digest(p)
        for p in out.rglob("*") if p.is_file() and p.name != "manifest.json"}})


def verify(out: Path):
    manifest = json.loads((out / "manifest.json").read_text())
    for name, expected in manifest["files"].items():
        path = (out / name).resolve()
        if not path.is_relative_to(out.resolve()) or digest(path) != expected:
            raise ValueError("Design artifact changed: " + name)


def protection(parent, campaign, directory: Path) -> dict:
    """Conservative measured contact heuristic, not H-bond/pharmacophore inference."""
    core = [a.GetProp("pdb_atom_name") for a in parent.GetAtoms() if a.IsInRing()]
    atoms = [a for a in read_pdb_atoms(directory / campaign.parent_pose.structure_path)
             if a.record == "ATOM" and a.chain_id == campaign.parent_pose.protein_chain_id
             and a.element in {"N", "O"}]
    contacts = []
    for atom in parent.GetAtoms():
        if atom.GetSymbol() not in {"N", "O"}:
            continue
        xyz = parent.GetConformer().GetAtomPosition(atom.GetIdx())
        for protein in atoms:
            distance = sum((xyz[i] - protein.xyz[i]) ** 2 for i in range(3)) ** 0.5
            if distance <= 3.5:
                contacts.append({"ligand_atom": atom.GetProp("pdb_atom_name"),
                    "protein_chain": protein.chain_id, "protein_residue": protein.residue_number,
                    "protein_residue_name": protein.residue_name,
                    "protein_atom": protein.atom_name, "distance_angstrom": distance})
    from .ligand_first_inputs import ProtectedInteractionSpec
    geometric_contacts = [ProtectedInteractionSpec(
        interaction_id=f"measured-NO-contact-{index + 1}", ligand_atom_name=row["ligand_atom"],
        ligand_role="geometric N/O contact proxy; not an assigned hydrogen bond",
        protein_chain_id=row["protein_chain"], protein_residue_name=row["protein_residue_name"],
        protein_author_residue_number=row["protein_residue"], protein_uniprot_residue_number=None,
        protein_atom_name=row["protein_atom"], maximum_distance_angstrom=3.5
    ).model_dump(mode="json") for index, row in enumerate(contacts)]
    return {"scaffold_core_atom_names": core, "protected_interactions": geometric_contacts,
        "protected_pharmacophore_atom_names": sorted({x["ligand_atom"] for x in contacts}),
        "measured_contacts": contacts, "method": POLICY["protection"],
        "limitations": ["N/O proximity does not establish hydrogen bonding or energetic importance.",
            "All parent rings are protected conservatively; no global optimality claim.",
            "Candidate interaction retention requires independent 3D evaluation."]}


def prepare(root: Path, evidence_id: str, proposal_id: str, proposal_sha256: str) -> dict:
    if not re.fullmatch(r"proposal-[a-f0-9]{12}", proposal_id):
        raise ValueError("Use an exact finalized proposal ID")
    base = folder(root, evidence_id)
    with version_lock(base):
        require_latest(root, evidence_id)
        evidence = read_evidence(root, evidence_id)["evidence"]
        source = base / proposal_id / "hypothesis.json"
        if digest(source) != proposal_sha256:
            raise ValueError("Proposal hash mismatch")
        proposal = json.loads(source.read_text())
        hypothesis = proposal["hypothesis"]
        if (proposal.get("evidence_id") != evidence_id
            or proposal.get("evidence_manifest_sha256") != digest(base / "manifest.json")
            or hypothesis.get("route") != "ligand-first"):
            raise ValueError("Proposal/evidence provenance mismatch")
        review = validate_comparison(evidence, hypothesis.get("parent_comparison"), hypothesis.get("parent_ligand"))
        if review["state"] != "PARENT_COMPARISON_REVIEW_READY":
            return {"state": review["state"], "blockers": review["blockers"]}
        if hypothesis.get("warhead_class") != POLICY["warheads"]:
            return {"state": "UNSUPPORTED_DESIGN_SCOPE", "blockers": [
                POLICY["version"] + " supports only explicitly proposed acrylamide; do not silently change the proposal"]}
        selected = next(x for x in hypothesis["parent_comparison"]["candidates"]
                        if x["molecule_id"] == review["selected_molecule_id"])
        geometry_id = selected["geometry_id"]
        if not re.fullmatch(r"geometry-[a-f0-9]{16}", geometry_id):
            raise ValueError("Invalid geometry record ID")
        acquisition = base / geometry_id
        # All acquisition bytes must be covered by the evidence seal before any copy.
        files = json.loads((base / "manifest.json").read_text())["files"]
        if any(str(p.relative_to(base)) not in files for p in acquisition.rglob("*") if p.is_file()):
            raise ValueError("Unsealed acquisition input")
        from .ligand_parent_pair import review_acquisition
        pairing = review_acquisition(acquisition)
        if pairing['status'] != 'ELIGIBLE_FOR_ENUMERATION_REVIEW':
            return {'state': 'NEEDS_PARENT_PAIR_REVIEW', 'parent_cys_assessment': pairing,
                'blockers': pairing['reasons'], 'compute_submitted': False,
                'next_action': 'Reconsider the parent or supplement missing structural evidence; do not repeat prepare unchanged.'}
        assets = upstream_assets()
        design_id = "design-" + uuid4().hex[:24]
        destination = design_folder(root, design_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        staging = destination.with_name("." + destination.name + ".preparing")
        staging.mkdir()
        try:
            case = staging / "case"
            accession = evidence["identity"]["uniprot_id"]
            if not re.fullmatch(r"[A-Z0-9]{6,10}", accession):
                raise ValueError("Invalid target accession")
            with urllib.request.urlopen("https://rest.uniprot.org/uniprotkb/" + accession + ".json", timeout=40) as response:
                identity_bytes = response.read(8_000_001)
            if len(identity_bytes) > 8_000_000:
                raise ValueError("UniProt response exceeds bounded size")
            identity = json.loads(identity_bytes)
            if identity.get("sequence", {}).get("value") != evidence["identity"].get("sequence"):
                raise ValueError("UniProt sequence changed since parent evidence collection")
            (staging / "uniprot.json").write_bytes(identity_bytes)
            preparation = prepare_native_case(acquisition, case, campaign_id=design_id,
                gene=verified_target_gene(evidence), taxon=evidence["taxon"], ligand_name=hypothesis["parent_ligand"],
                noncovalent_parent_assertion="Bound to the revalidated sealed comparison in proposal.json",
                identity_record=staging / "uniprot.json")
            campaign = load_campaign(case / "campaign.json")
            parent = build_parent_pose_mol(campaign, case)
            protected = protection(parent, campaign, case)
            if not protected["scaffold_core_atom_names"]:
                raise ValueError("This bounded design policy requires a ring scaffold")
            campaign = campaign.model_copy(update={"planning_mode": "upstream-native", "allowed_warheads": POLICY["warheads"],
                "protection": campaign.protection.model_copy(update={key: protected[key] for key in
                    ("scaffold_core_atom_names", "protected_pharmacophore_atom_names")})})
            # Validate nested contact records, preserving author numbering when no
            # residue-level UniProt mapping is available. Never infer an offset.
            campaign_data = campaign.model_dump(mode="json")
            campaign_data["protection"]["protected_interactions"] = protected["protected_interactions"]
            campaign = type(campaign).model_validate(campaign_data)
            (case / "campaign.json").write_text(campaign.model_dump_json(indent=2))
            preparation.update(campaign_path="campaign.json", campaign_sha256=digest(case / "campaign.json"),
                noncovalent_parent_evidence={"kind": "revalidated_sealed_parent_comparison",
                    "evidence_id": evidence_id, "proposal_id": proposal_id, "proposal_sha256": proposal_sha256})
            write(case / "native_case_preparation.json", preparation)
            shutil.copyfile(source, staging / "proposal.json")
            write(staging / "protection.json", protected)
            write(staging / "parent-cys-assessment.json", pairing)
            write(staging / "design.json", {"design_id": design_id, "state": "DESIGN_INPUTS_READY",
                "evidence_id": evidence_id, "evidence_manifest_sha256": digest(base / "manifest.json"),
                "proposal_id": proposal_id, "proposal_sha256": proposal_sha256,
                "selected_molecule_id": review["selected_molecule_id"], "geometry_id": geometry_id,
                "policy": FULL_PARENT_POLICY, "upstream_assets": assets, "rdkit_version": rdBase.rdkitVersion,
                "compute_submitted": False, "derivatization_feasibility": "unknown"})
            seal(staging)
            staging.rename(destination)
        except Exception:
            # Retain failed preparation for diagnosis; never expose it as a committed design.
            write(staging / "failure.json", {"state": "PREPARATION_FAILED", "design_id": design_id})
            raise
    return inspect(root, design_id)


def inspect(root: Path, design_id: str) -> dict:
    directory = design_folder(root, design_id)
    verify(directory)
    design = json.loads((directory / "design.json").read_text())
    result = {**design, "next_call": {"tool": "covalent_ligand_design", "arguments": {
        "action": "generate", "design_id": design_id}}, "artifacts": {"design": str(directory / "design.json")}}
    generated = directory / "generation"
    if generated.exists():
        verify(generated)
        result.update(json.loads((generated / "summary.json").read_text()))
        result["artifacts"].update({"report": str(generated / "report.md"),
            "candidates": str(generated / "candidates.sdf"), "review": str(generated / "review.json"),
            "failures": str(generated / "failures.json")})
    if (directory / "pose-plan").exists():
        verify(directory / "pose-plan")
        result.update(json.loads((directory / "pose-plan/summary.json").read_text()))
        result["artifacts"]["pose_plan"] = str(directory / "pose-plan/job-request.json")
    if (directory / "delivery").exists():
        verify(directory / "delivery")
        result["delivery"] = json.loads((directory / "delivery/disposition.json").read_text())
        result["artifacts"]["delivery_report"] = str(directory / "delivery/report.md")
        result["artifacts"]["disposition"] = str(directory / "delivery/disposition.json")
        result["next_call"] = None
        result["reporting_instruction"] = "Use workflow evidence export_design_report to save the official delivery_report and disposition to Results; remote paths are not session files. Quote allowed_conclusion; do not infer ring identity, incompatibility or a completed computational loop from rule failures."
    return result


def generate(root: Path, design_id: str) -> dict:
    directory = design_folder(root, design_id)
    with version_lock(directory, ".generation.lock"):
        verify(directory)
        design = json.loads((directory / "design.json").read_text())
        require_latest(root, design["evidence_id"])
        current = read_evidence(root, design["evidence_id"])
        if current["state"] != "EVIDENCE_READY" or digest(folder(root, design["evidence_id"]) / "manifest.json") != design["evidence_manifest_sha256"]:
            raise ValueError("Design source evidence changed")
        if (directory / "generation").exists():
            return inspect(root, design_id)
        policy = design.get('policy')
        if policy not in (POLICY, FULL_PARENT_POLICY):
            raise ValueError("Prepared design policy differs from this generator; prepare a new design, never silently migrate it")
        out = directory / (".generation-" + uuid4().hex[:12])
        out.mkdir()
        campaign = load_campaign(directory / "case/campaign.json")
        parent = build_parent_pose_mol(campaign, directory / "case")
        if policy == FULL_PARENT_POLICY:
            from .upstream_handoff import generate_full_parent_candidates
            from .ligand_first_site_discovery import _target_xyz
            assets = design.get('upstream_assets')
            if not assets or assets != upstream_assets():
                raise ValueError('Registered Covalentizer assets differ from the prepared design')
            candidates, molecules, audit = generate_full_parent_candidates(
                parent, campaign, _target_xyz(campaign, directory / 'case'), out / 'upstream',
                upstream_repo=Path(assets['repository']), policy=policy)
            records = [candidate.model_dump(mode='json') for candidate in candidates]
            failures = [{'stage': 'attachment', **row} for row in audit['inventory']
                        if row['disposition'] in {'mapping_or_chemistry_rejected', 'geometry_rejected'}]
            with Chem.SDWriter(str(out / 'candidates.sdf')) as writer:
                for molecule in molecules:
                    writer.write(molecule)
            write(out / 'upstream-review.json', audit)
            selected_anchors = set(audit['selection']['budgeted_parent_atom_indices'])
            site_rows = {}
            for row in audit['geometry_reviews']:
                geometry = row['geometry']
                index = geometry['attachment_parent_atom_index']
                site_rows[index] = {'exit_vector_id': f'upstream-add-C{index}',
                    'operation': 'add_at_parent_carbon_hydrogen',
                    'anchor_atom_index': index,
                    'anchor_atom_name': parent.GetAtomWithIdx(index).GetProp('pdb_atom_name'),
                    'removed_parent_atom_indices': [],
                    'advance': geometry['prefilter_passed'] and index in selected_anchors,
                    'rejection_reasons': geometry['rejection_reasons'],
                    'geometry': geometry}
            write(out / 'sites.json', {'strategy': policy['strategy'],
                'candidates': [site_rows[index] for index in sorted(site_rows)],
                'selection': audit['selection']})
            (out / 'campaign.json').write_text(campaign.model_dump_json(indent=2))
        else:
            sites = discover_covalentizer_sites(campaign, directory / "case", **{key: POLICY[key] for key in
                ("max_sites", "max_branch_heavy_atoms", "max_distance_angstrom", "max_vector_angle_degrees")})
            write(out / "sites.json", sites.model_dump(mode="json"))
            campaign = campaign.model_copy(update={"exit_vectors": exit_vector_specs(sites)})
            (out / "campaign.json").write_text(campaign.model_dump_json(indent=2))
            records, failures = [], []
            with Chem.SDWriter(str(out / "candidates.sdf")) as writer:
                for site in exit_vector_specs(sites):
                    if not site.advance:
                        failures.append({"stage": "attachment", "site_id": site.exit_vector_id, "reason": site.reference_mapping})
                        continue
                    for transformation in site.allowed_transformations:
                        if RULES[transformation].warhead_name not in POLICY["warheads"]:
                            continue
                        try:
                            record, product = covalentize_parent(parent, campaign, site.exit_vector_id, transformation_name=transformation)
                            if record.quality_gate.status != "PASS":
                                failures.append({"stage": "chemistry", "candidate_id": record.candidate_id,
                                                 "reasons": record.quality_gate.failure_reasons})
                                continue
                            # Combined graph coordinates are not generated 3D poses.
                            product.RemoveAllConformers()
                            writer.write(product)
                            records.append(record.model_dump(mode="json"))
                        except ValueError as error:
                            failures.append({"stage": "generation", "site_id": site.exit_vector_id,
                                             "transformation": transformation, "reason": str(error)})
        if len(records) > policy["max_candidates"]:
            raise ValueError("Candidate budget exceeded")
        write(out / "candidates.json", {"candidates": records})
        # Independent re-read and chemistry recomputation; never trust producer PASS alone.
        molecules = list(Chem.SDMolSupplier(str(out / "candidates.sdf"), removeHs=False)) if records else []
        reviews = []
        if len(molecules) != len(records):
            raise ValueError("Saved candidate count mismatch")
        for record, mol in zip(records, molecules):
            if mol is None:
                raise ValueError("Saved candidate cannot be parsed")
            gate = evaluate_chemistry_quality(mol, parent, campaign)
            identity = Chem.Mol(mol)
            for atom in identity.GetAtoms():
                atom.SetAtomMapNum(0)
            matched = (Chem.MolToSmiles(identity) == record["canonical_smiles"]
                and mol.HasProp("candidate_id") and mol.GetProp("candidate_id") == record["candidate_id"])
            reviews.append({"candidate_id": record["candidate_id"], "identity_matches": matched,
                            "chemistry": gate.model_dump(mode="json"), "pose_assessment": "NOT_RUN"})
        graph_passed = bool(reviews) and all(x["identity_matches"] and x["chemistry"]["status"] == "PASS" for x in reviews)
        execution_errors = any(item["stage"] == "generation" for item in failures)
        state = ("DESIGN_REVIEW_FAILED" if execution_errors else "NEEDS_POSE_EVALUATION" if graph_passed
                 else "NO_GRAPH_CANDIDATES" if not reviews else "DESIGN_REVIEW_FAILED")
        write(out / "review.json", {"state": state, "candidates": reviews,
            "independence": "Re-read saved SDF; shared audited chemistry implementation, no independent predictive model."})
        write(out / "failures.json", {"failures": failures})
        summary = {"state": state, "candidate_count": len(records), "failure_count": len(failures),
            "graph_review_passed": graph_passed, "derivatization_feasibility": "unknown",
            "compute_submitted": False, "next_call": (None if state == "NEEDS_POSE_EVALUATION"
                else {"tool": "covalent_ligand_design", "arguments": {"action": "deliver", "design_id": design_id}}),
            "next_gate": (
                "Continue with prepare_docking_input in the registered Ligand workflow: the verified complete batch selects direct CovDock-LO for 1–6 admitted candidates or CovDock-VS then LO for larger batches. Independent receptor preparation and native execution approval remain required. Graph generation is not pose acceptance or MD admission."
                if state == "NEEDS_POSE_EVALUATION" else
                "Deliver this branch with its rejection or review-failure evidence; do not register a pose job. Reconsider the parent or design strategy through the workflow before a new attempt."),
            "limitations": ["No 3D candidate poses, reaction-angle, clash, strain or interaction-retention evaluation has run.",
                "No synthesis, affinity, covalent kinetics, selectivity or experimental validation.",
                "This is a bounded design package, not completed computational acceptance."]}
        write(out / "summary.json", summary)
        (out / "report.md").write_text("# Ligand-first bounded design\n\n" + json.dumps({
            "design_id": design_id, "evidence_id": design["evidence_id"], "policy": policy, **summary}, ensure_ascii=False, indent=2))
        seal(out)
        out.rename(directory / "generation")
    return inspect(root, design_id)


def plan_pose(root: Path, design_id: str) -> dict:
    """Export a bounded, identity-bound input package, without executing a job."""
    from .docking_policy import require_engine_enabled
    require_engine_enabled("carsidock-cov")
    from .jobs import JobSubmissionRequest
    from .ligand_first_geometry import provisional_geometry_policy
    from .rdkit_covalentizer import WARHEAD_ATOM_MAPS
    directory = design_folder(root, design_id)
    with version_lock(directory, ".pose-plan.lock"):
        current = inspect(root, design_id)
        require_latest(root, current["evidence_id"])
        read_evidence(root, current["evidence_id"])
        if (directory / "pose-plan").exists():
            return current
        if current["state"] != "NEEDS_POSE_EVALUATION":
            raise ValueError("A verified nonempty graph generation is required")
        generated = directory / "generation"
        records = json.loads((generated / "candidates.json").read_text())["candidates"]
        molecules = list(Chem.SDMolSupplier(str(generated / "candidates.sdf"), removeHs=False))
        if len(records) != len(molecules) or len({r["candidate_id"] for r in records}) != len(records):
            raise ValueError("Candidate identity/count mismatch")
        campaign = load_campaign(generated / "campaign.json")
        destination = directory / "pose-plan"
        out = directory / (".pose-plan-" + uuid4().hex[:12])
        out.mkdir()
        selected = records[:3]  # Deterministic pilot order, never a scientific ranking.
        rows = []
        reaction_map = WARHEAD_ATOM_MAPS["reaction_beta_carbon"]
        with Chem.SDWriter(str(out / "ligands.sdf")) as writer:
            for index, (record, molecule) in enumerate(zip(selected, molecules)):
                if (molecule is None or not molecule.HasProp("candidate_id")
                        or molecule.GetProp("candidate_id") != record["candidate_id"]):
                    raise ValueError("Saved candidate identity mismatch")
                if sum(a.GetAtomMapNum() == reaction_map for a in molecule.GetAtoms()) != 1:
                    raise ValueError("Missing or ambiguous electrophilic atom map")
                molecule.SetIntProp("INPUT_SDF_INDEX", index)
                molecule.SetProp("compound_id", record["candidate_id"])
                molecule.SetProp("COMPOUND_ID", record["candidate_id"])
                molecule.SetProp("_Name", record["candidate_id"])
                writer.write(molecule)
                rows.append({"input_sdf_index": index, "compound_id": record["candidate_id"],
                    "reaction_type": "Michael Addition", "electrophilic_atom_map": reaction_map})
        with (out / "ligands.csv").open("w", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(rows)
        relative = lambda path: str(path.relative_to(root))
        parameters = {"pdb": relative(directory / "case" / campaign.parent_pose.structure_path),
            "reference_ligand": relative(directory / "case/parent_pose.sdf"),
            "ligand_sdf": relative(destination / "ligands.sdf"),
            "ligand_table": relative(destination / "ligands.csv"),
            "covres": f"{campaign.parent_pose.protein_chain_id}:{campaign.target.nucleophile.residue_name}:{campaign.parent_pose.target_author_residue_number}",
            "reaction_type_column": "reaction_type", "num_conformer": 3, "seed": 20260725,
            "dispatch_policy": "ligand-smoke-v1", "dispatch_batch": "ligand-smoke-" + design_id,
            "input_row_count": len(rows)}
        request = JobSubmissionRequest(request_id=design_id + "-pose-v1", project_id=design_id,
            route="ligand-first", engine="carsidock-cov", parameters=parameters,
            resource_budget={"max_runtime_minutes": 30, "gpu_count": 1, "cpu_count": 20,
                "memory_gb": 200, "max_candidates": len(rows)}, execution_mode="plan")
        write(out / "job-request.json", request.model_dump(mode="json"))
        write(out / "geometry-policy.json", provisional_geometry_policy().model_dump(mode="json"))
        write(out / "input-binding.json", {"design_id": design_id,
            "design_manifest_sha256": digest(directory / "manifest.json"),
            "generation_manifest_sha256": digest(generated / "manifest.json"),
            "source_inputs": {parameters[k]: digest(root / parameters[k]) for k in ("pdb", "reference_ligand")},
            "selected_candidate_ids": [row["candidate_id"] for row in selected],
            "deferred_candidate_ids": [row["candidate_id"] for row in records[3:]],
            "selection": "first three in deterministic enumeration order; not ranked by predicted potency"})
        scientific_blockers = ([] if campaign.protection.protected_interactions else
            ["No protected contacts/interactions are defined; retention cannot be assessed."])
        write(out / "summary.json", {"state": "POSE_INPUTS_BLOCKED" if scientific_blockers else "POSE_PLAN_READY",
            "compute_submitted": False, "next_call": None, "pose_candidate_count": len(rows),
            "deferred_candidate_count": max(0, len(records) - 3), "scientific_blockers": scientific_blockers,
            "runtime_registration": "NOT_RUN", "approval": "NOT_REQUESTED",
            "next_gate": "Register this exact input package in the configured runtime, then use native approval before execute.",
            "limitations": ["Export and schema validation do not prove runtime availability or pose quality.",
                "Rule-derived protected contacts are N/O distance proxies, not validated pharmacophoric interactions."]})
        seal(out)
        out.rename(destination)
    return inspect(root, design_id)


def design_tool(root: Path, action: str, **arguments) -> dict:
    if action == "prepare":
        return prepare(root, **arguments)
    if action == "read":
        return inspect(root, **arguments)
    if action == "generate":
        return generate(root, **arguments)
    if action == "plan_pose":
        return plan_pose(root, **arguments)
    if action == "deliver":
        return deliver(root, **arguments)
    raise ValueError("Unknown design action")


def deliver(root: Path, design_id: str) -> dict:
    """Publish machine-derived disposition; do not turn a graph stop into infeasibility."""
    directory = design_folder(root, design_id)
    with version_lock(directory, ".delivery.lock"):
        current = inspect(root, design_id)
        require_latest(root, current["evidence_id"])
        read_evidence(root, current["evidence_id"])
        if (directory / "delivery").exists():
            return current
        generated = directory / "generation"
        if not generated.exists():
            raise ValueError("Run the bounded generation before delivery")
        failures = json.loads((generated / "failures.json").read_text())["failures"]
        campaign = load_campaign(generated / "campaign.json")
        parent = build_parent_pose_mol(campaign, directory / "case")
        sites = json.loads((generated / "sites.json").read_text())
        detail = []
        protected_ring = set(campaign.protection.scaffold_core_atom_names)
        protected_contact = set(campaign.protection.protected_pharmacophore_atom_names)
        for site in sites["candidates"]:
            protected = []
            for index in site["removed_parent_atom_indices"]:
                atom = parent.GetAtomWithIdx(index)
                name = atom.GetProp("pdb_atom_name")
                reasons = (["受保护环骨架"] if name in protected_ring else []) + (
                    ["母体结构中 3.5 Å 内的 N/O 接触保护"] if name in protected_contact else [])
                if reasons:
                    protected.append({"index": index, "name": name, "element": atom.GetSymbol(),
                        "is_ring": atom.IsInRing(), "protection_reasons": reasons})
            detail.append({"site_id": site["exit_vector_id"], "advance": site["advance"],
                "rejection_reasons": site["rejection_reasons"], "protected_removed_atoms": protected,
                **({'operation': site['operation'], 'geometry': site['geometry']}
                   if 'geometry' in site else {})})
        negative = (current["state"] == "NO_GRAPH_CANDIDATES" and not any(
            item["stage"] == "generation" for item in failures))
        disposition = {"design_id": design_id, "evidence_id": current["evidence_id"],
            "source_state": current["state"], "outcome": "BOUNDED_NEGATIVE_BRANCH" if negative else "INCOMPLETE",
            "full_computational_loop_accepted": False,
            "selected_parent": campaign.parent_pose.ligand_name,
            "scope": ("本次所选母体、已封存结构、固定保护规则和完整母体新增连接策略；C–H 方向为推算值，尚未评估候选真实姿态。"
                if current['policy'] == FULL_PARENT_POLICY else
                "本次所选母体、已封存结构、固定保护规则和分支替换策略；不涵盖其他母体或其他修饰策略。"),
            "design_policy": current['policy'],
            **({'candidate_selection': sites['selection']} if 'selection' in sites else {}),
            "candidate_count": current["candidate_count"], "site_details": detail,
            "failure_ledger": failures, "pose_evaluation": "NOT_RUN", "compute_submitted": False,
            "allowed_conclusion": ("本轮固定规则未产生可推进的化学图候选，保留淘汰证据。"
                if negative else "本轮尚未完成真实三维评估与计算验收。"),
            "unsupported_conclusions": ["不能据此断言母体与目标残基或弹头几何不兼容。",
                "不能据此断言整个靶点不可行、所有母体均失败或已有有效共价抑制剂。",
                "规则筛选结果不证明程序不存在缺陷。"],
            "unassessed": ["其他母体的设计分支", "候选真实三维姿态", "亲和力与共价反应动力学",
                "选择性", "合成可行性", "实验验证"],
            "binding": {"design_manifest_sha256": digest(directory / "manifest.json"),
                "generation_manifest_sha256": digest(generated / "manifest.json")}}
        out = directory / (".delivery-" + uuid4().hex[:12])
        out.mkdir()
        write(out / "disposition.json", disposition)
        lines = ["# Ligand-first 本轮设计交付", "", disposition["allowed_conclusion"], "",
            "范围：" + disposition["scope"], "", f"所选母体：{disposition['selected_parent']}。候选数：{current['candidate_count']}。",
            "", "**真实三维计算尚未运行；完整计算设计闭环尚未验收通过。**", "",
            "## 位点判定", "", "```json", json.dumps(detail, ensure_ascii=False, indent=2), "```", "",
            "## 结论边界", "", *["- " + item for item in disposition["unsupported_conclusions"]], "",
            "## 未评估项", "", *["- " + item for item in disposition["unassessed"]], ""]
        (out / "report.md").write_text("\n".join(lines))
        seal(out)
        out.rename(directory / "delivery")
    return inspect(root, design_id)
