#!/usr/bin/env python3
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import json
from pathlib import Path

import pandas as pd
from rdkit import Chem, DataStructs
from rdkit.Chem import AllChem, Crippen, Descriptors, Lipinski, QED, rdDepictor
from rdkit.Chem.FilterCatalog import FilterCatalog, FilterCatalogParams

from covscreen.ligand_first_inputs import (
    audit_ligand_first_inputs,
    build_parent_pose_mol,
    load_campaign,
)
from covscreen.ligand_first_site_discovery import (
    discover_covalentizer_sites,
    exit_vector_specs,
)
from covscreen.rdkit_covalentizer import covalentize_parent
from covscreen.ligand_candidate_table import candidate_table_row


FORMAL_WARHEADS = [
    "acrylamide",
    "chloroacetamide",
    "vinyl_sulfone",
    "propiolamide",
]


def _pains_catalog() -> FilterCatalog:
    parameters = FilterCatalogParams()
    parameters.AddCatalog(FilterCatalogParams.FilterCatalogs.PAINS)
    return FilterCatalog(parameters)


def _medchem_evidence(
    molecule: Chem.Mol,
    parent: Chem.Mol,
    pains: FilterCatalog,
) -> dict[str, object]:
    """Compute transparent, inexpensive triage evidence; none predicts activity."""

    mol = Chem.Mol(molecule)
    parent_mol = Chem.Mol(parent)
    for atom in mol.GetAtoms():
        atom.SetAtomMapNum(0)
    for atom in parent_mol.GetAtoms():
        atom.SetAtomMapNum(0)
    mol_fp = AllChem.GetMorganFingerprintAsBitVect(mol, 2, nBits=2048)
    parent_fp = AllChem.GetMorganFingerprintAsBitVect(parent_mol, 2, nBits=2048)
    logp = float(Crippen.MolLogP(mol))
    mw = float(Descriptors.MolWt(mol))
    aromatic_rings = int(Lipinski.NumAromaticRings(mol))
    pains_matches = sorted({match.GetDescription() for match in pains.GetMatches(mol)})
    return {
        "property_fit_score": float(QED.qed(mol)),
        "molecular_weight": mw,
        "clogp": logp,
        "tpsa": float(Descriptors.TPSA(mol)),
        "rotatable_bonds": int(Lipinski.NumRotatableBonds(mol)),
        "reference_similarity": float(DataStructs.TanimotoSimilarity(mol_fp, parent_fp)),
        "pains_alert": ";".join(pains_matches),
        # This flag is deliberately named and documented as a heuristic. It is
        # not an aggregation assay or a trained predictor.
        "aggregation_risk": bool(logp > 4.5 and aromatic_rings >= 3),
        "aggregation_risk_method": "heuristic_clogp_gt_4.5_and_aromatic_rings_ge_3",
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate traceable, chemistry-gated candidates for a real Ligand-first campaign."
    )
    parser.add_argument("--campaign", type=Path, required=True)
    parser.add_argument("--candidate-json", type=Path, required=True)
    parser.add_argument("--candidate-sdf", type=Path, required=True)
    parser.add_argument("--ligand-table", type=Path, required=True)
    parser.add_argument("--failure-json", type=Path, required=True)
    parser.add_argument(
        "--auto-discover-sites",
        action="store_true",
        help=(
            "Replace the campaign's manually declared exit vectors with deterministic "
            "Covalentizer/RDKit branch-replacement sites."
        ),
    )
    parser.add_argument(
        "--site-discovery-json",
        type=Path,
        help="Required with --auto-discover-sites; writes the ranked site audit.",
    )
    parser.add_argument("--max-sites", type=int, default=8)
    parser.add_argument("--max-branch-heavy-atoms", type=int, default=12)
    parser.add_argument("--max-distance-angstrom", type=float, default=12.0)
    parser.add_argument("--max-vector-angle-degrees", type=float, default=100.0)
    args = parser.parse_args()

    if args.auto_discover_sites and args.site_discovery_json is None:
        parser.error("--site-discovery-json is required with --auto-discover-sites")

    campaign_path = args.campaign.resolve()
    campaign = load_campaign(campaign_path)
    audit, _ = audit_ligand_first_inputs(campaign, campaign_path.parent)
    if audit.status != "PASS":
        raise SystemExit(f"campaign input audit failed: {audit.errors}")
    parent = build_parent_pose_mol(campaign, campaign_path.parent)
    pains = _pains_catalog()

    site_discovery = None
    if args.auto_discover_sites:
        site_discovery = discover_covalentizer_sites(
            campaign,
            campaign_path.parent,
            max_sites=args.max_sites,
            max_branch_heavy_atoms=args.max_branch_heavy_atoms,
            max_distance_angstrom=args.max_distance_angstrom,
            max_vector_angle_degrees=args.max_vector_angle_degrees,
        )
        args.site_discovery_json.parent.mkdir(parents=True, exist_ok=True)
        args.site_discovery_json.write_text(
            json.dumps(site_discovery.model_dump(mode="json"), ensure_ascii=False, indent=2)
            + "\n"
        )
        if site_discovery.status != "PASS":
            raise SystemExit("automatic site discovery found no advancing exit vector")
        campaign = campaign.model_copy(
            update={
                "exit_vectors": exit_vector_specs(site_discovery),
                "allowed_warheads": FORMAL_WARHEADS,
            }
        )

    candidates: list[dict] = []
    accepted_molecules: list[Chem.Mol] = []
    ligand_rows: list[dict] = []
    failures: list[dict] = []
    for exit_vector in sorted(campaign.exit_vectors, key=lambda item: item.priority):
        if not exit_vector.advance:
            failures.append(
                {
                    "exit_vector_id": exit_vector.exit_vector_id,
                    "stage": "exit-vector-gate",
                    "failure_type": "PROTECTED_NON_ADVANCING_EXIT",
                    "failure_reason": exit_vector.reference_mapping,
                }
            )
            continue
        for transformation in exit_vector.allowed_transformations:
            try:
                candidate, product = covalentize_parent(
                    parent, campaign, exit_vector.exit_vector_id, transformation
                )
            except Exception as exc:
                failures.append(
                    {
                        "exit_vector_id": exit_vector.exit_vector_id,
                        "transformation_name": transformation,
                        "stage": "rdkit-covalentization",
                        "failure_type": type(exc).__name__,
                        "failure_reason": str(exc),
                    }
                )
                continue
            record = candidate.model_dump(mode="json")
            candidates.append(record)
            if candidate.quality_gate.status != "PASS":
                failures.append(
                    {
                        "candidate_id": candidate.candidate_id,
                        "stage": "chemistry-quality-gate",
                        "failure_type": "CHEMISTRY_GATE_FAIL",
                        "failure_reason": "; ".join(candidate.quality_gate.failure_reasons),
                    }
                )
                continue
            output_mol = Chem.Mol(product)
            output_mol.RemoveAllConformers()
            rdDepictor.Compute2DCoords(output_mol)
            output_mol.SetProp(
                "quality_gate_json",
                json.dumps(candidate.quality_gate.model_dump(mode="json"), ensure_ascii=False),
            )
            output_mol.SetProp("canonical_smiles", candidate.canonical_smiles)
            input_sdf_index = len(accepted_molecules)
            output_mol.SetProp("_Name", candidate.candidate_id)
            output_mol.SetProp("COMPOUND_ID", candidate.candidate_id)
            output_mol.SetProp("INPUT_SDF_INDEX", str(input_sdf_index))
            output_mol.SetProp("PARENT_ID", campaign.parent_pose.ligand_component_id)
            accepted_molecules.append(output_mol)
            ligand_rows.append(
                {
                    **candidate_table_row(candidate, campaign, input_sdf_index),
                    **_medchem_evidence(output_mol, parent, pains),
                }
            )

    args.candidate_json.parent.mkdir(parents=True, exist_ok=True)
    args.candidate_sdf.parent.mkdir(parents=True, exist_ok=True)
    args.ligand_table.parent.mkdir(parents=True, exist_ok=True)
    args.failure_json.parent.mkdir(parents=True, exist_ok=True)
    args.candidate_json.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "campaign_id": campaign.campaign_id,
                "site_discovery_mode": (
                    "covalentizer-rdkit-rule-v1" if site_discovery is not None else "campaign-declared"
                ),
                "site_count": len(campaign.exit_vectors),
                "advancing_site_count": sum(item.advance for item in campaign.exit_vectors),
                "generated_count": len(candidates),
                "chemistry_pass_count": len(accepted_molecules),
                "candidates": candidates,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    args.failure_json.write_text(
        json.dumps(
            {
                "schema_version": "1.0",
                "campaign_id": campaign.campaign_id,
                "site_discovery_mode": (
                    "covalentizer-rdkit-rule-v1" if site_discovery is not None else "campaign-declared"
                ),
                "failure_count": len(failures),
                "failures": failures,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    writer = Chem.SDWriter(str(args.candidate_sdf))
    try:
        for molecule in accepted_molecules:
            writer.write(molecule)
    finally:
        writer.close()
    pd.DataFrame(ligand_rows).to_parquet(args.ligand_table, index=False)
    pd.DataFrame(ligand_rows).to_csv(args.ligand_table.with_suffix(".csv"), index=False)
    print(
        json.dumps(
            {
                "campaign_id": campaign.campaign_id,
                "site_discovery_mode": (
                    "covalentizer-rdkit-rule-v1" if site_discovery is not None else "campaign-declared"
                ),
                "sites": len(campaign.exit_vectors),
                "advancing_sites": sum(item.advance for item in campaign.exit_vectors),
                "generated": len(candidates),
                "chemistry_pass": len(accepted_molecules),
                "failures_recorded": len(failures),
            },
            ensure_ascii=False,
        )
    )
    return 0 if accepted_molecules else 1


if __name__ == "__main__":
    raise SystemExit(main())
