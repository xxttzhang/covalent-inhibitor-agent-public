from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from rdkit import Chem
from rdkit.Chem import Crippen, Descriptors, FilterCatalog, rdFMCS

from covscreen.jobs import sha256_file, utc_now
from .ligand_first_inputs import build_parent_pose_mol, load_campaign


WARHEAD_SMARTS = {
    "acrylamide": "[NX3][CX3](=[OX1])[CX3]=[CX3]",
    "chloroacetamide": "[NX3][CX3](=[OX1])[CH2][Cl]",
    "vinyl_sulfone": "[#6][SX4](=[OX1])(=[OX1])[CX3]=[CX3]",
    "vinyl_sulfonamide": "[NX3][SX4](=[OX1])(=[OX1])[CX3]=[CX3]",
    "propiolamide": "[NX3][CX3](=[OX1])[CX2]#[CX2H1]",
}


def _pains_catalog() -> FilterCatalog.FilterCatalog:
    """Build the public RDKit PAINS catalog used for final-ranking evidence."""

    parameters = FilterCatalog.FilterCatalogParams()
    parameters.AddCatalog(FilterCatalog.FilterCatalogParams.FilterCatalogs.PAINS)
    return FilterCatalog.FilterCatalog(parameters)


def _bool(value: object) -> bool | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, (bool, np.bool_)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    return None


def _finite(value: object) -> float | None:
    parsed = pd.to_numeric(value, errors="coerce")
    return float(parsed) if pd.notna(parsed) and np.isfinite(parsed) else None


def _mcs_parent_fraction(parent: Chem.Mol, candidate: Chem.Mol) -> float:
    result = rdFMCS.FindMCS(
        [parent, candidate],
        atomCompare=rdFMCS.AtomCompare.CompareElements,
        bondCompare=rdFMCS.BondCompare.CompareOrder,
        ringMatchesRingOnly=True,
        completeRingsOnly=True,
        timeout=10,
    )
    parent_heavy = sum(atom.GetAtomicNum() > 1 for atom in parent.GetAtoms())
    return result.numAtoms / parent_heavy if parent_heavy else 0.0


def independently_evaluate_ligand_first_batch(
    candidates: pd.DataFrame,
    campaign_path: Path,
    *,
    min_parent_mcs_fraction: float = 0.60,
    min_covalent_bond_length: float = 1.5,
    max_covalent_bond_length: float = 2.2,
    max_local_strain: float = 10.0,
    hard_max_strain: float = 20.0,
    coordinate_evidence: pd.DataFrame | None = None,
) -> pd.DataFrame:
    required = {
        "input_sdf_index",
        "compound_id",
        "standardized_smiles",
        "warhead_class",
    }
    missing = sorted(required - set(candidates))
    if missing:
        raise ValueError(f"Ligand-first evaluation table is missing columns: {missing}")
    frame = candidates.copy()
    frame["input_sdf_index"] = pd.to_numeric(
        frame["input_sdf_index"], errors="raise"
    ).astype("int64")
    if frame.empty or frame["input_sdf_index"].duplicated().any():
        raise ValueError("Ligand-first evaluation requires a non-empty unique identity denominator")
    coordinate_rows = {}
    if coordinate_evidence is not None:
        if not {"input_sdf_index", "compound_id"}.issubset(coordinate_evidence.columns):
            raise ValueError("Coordinate evidence requires compound identity")
        if (not coordinate_evidence["input_sdf_index"].is_unique
                or set(coordinate_evidence["input_sdf_index"]) != set(frame["input_sdf_index"])):
            raise ValueError("Coordinate evidence identity denominator mismatch")
        expected = frame.set_index("input_sdf_index")["compound_id"]
        observed = coordinate_evidence.set_index("input_sdf_index")["compound_id"]
        if expected.isna().any() or not observed.reindex(expected.index).eq(expected).all():
            raise ValueError("Coordinate evidence compound identity mismatch")
        # Evidence must not overwrite chemical structures, scores, or identities.
        evidence_columns = [name for name in coordinate_evidence.columns
                            if name.startswith("coordinate_recheck_")]
        coordinate_rows = coordinate_evidence.set_index("input_sdf_index")[evidence_columns].to_dict("index")
    campaign = load_campaign(campaign_path)
    parent = build_parent_pose_mol(campaign, campaign_path.parent)
    for atom in parent.GetAtoms():
        atom.SetAtomMapNum(0)
    pains_catalog = _pains_catalog()

    rows: list[dict[str, Any]] = []
    for row in frame.to_dict(orient="records"):
        failures: list[str] = []
        warnings: list[str] = []
        coordinates = coordinate_rows.get(row["input_sdf_index"], {})
        geometry_evaluated = coordinates.get("coordinate_recheck_status") == "SUCCESS"
        molecule = Chem.MolFromSmiles(str(row["standardized_smiles"]))
        chemistry_valid = molecule is not None
        mcs_fraction = 0.0
        warhead_matches = 0
        all_warhead_counts: dict[str, int] = {}
        fragment_count = 0
        formal_charge = 0
        radicals = 0
        high_reactivity_alert: bool | None = None
        aggregation_risk: bool | None = None
        pains_alert: str | None = None
        if molecule is None:
            failures.append("invalid_standardized_smiles")
        else:
            fragment_count = len(Chem.GetMolFrags(molecule))
            formal_charge = sum(atom.GetFormalCharge() for atom in molecule.GetAtoms())
            radicals = sum(atom.GetNumRadicalElectrons() for atom in molecule.GetAtoms())
            warhead = str(row["warhead_class"])
            all_warhead_counts = {
                name: len(molecule.GetSubstructMatches(Chem.MolFromSmarts(pattern)))
                for name, pattern in WARHEAD_SMARTS.items()
            }
            # The source table can legitimately omit triage annotations after
            # AF3/CovDock hand-off.  Recompute them here rather than treating
            # missing values as favourable values during the Pareto stage.
            supported_warhead_count = sum(all_warhead_counts.values())
            high_reactivity_alert = supported_warhead_count > 1
            aggregation_risk = (
                float(Crippen.MolLogP(molecule)) > 5.0
                and float(Descriptors.MolWt(molecule)) > 500.0
            )
            pains_match = pains_catalog.GetFirstMatch(molecule)
            pains_alert = pains_match.GetDescription() if pains_match else ""
            smarts = WARHEAD_SMARTS.get(warhead)
            if smarts is None:
                failures.append("unsupported_warhead_class")
            else:
                warhead_matches = all_warhead_counts[warhead]
                if warhead_matches != 1:
                    failures.append("warhead_match_count_not_one")
            if supported_warhead_count != 1:
                failures.append("total_supported_warhead_count_not_one")
            if fragment_count != 1:
                failures.append("multiple_molecular_components")
            if abs(formal_charge) > 2:
                failures.append("formal_charge_outside_gate")
            if radicals:
                failures.append("radical_electrons_present")
            mcs_fraction = _mcs_parent_fraction(parent, molecule)
            if mcs_fraction < min_parent_mcs_fraction:
                failures.append("parent_scaffold_mcs_fraction_below_gate")

        chemistry_valid = molecule is not None and not failures

        # A generic docking score is not evidence of an AF3 interface mPAE.
        af3_mpae = _finite(row.get("af3_mpae"))
        if af3_mpae is None:
            failures.append("af3_mpae_missing")

        covdock_success = _bool(row.get("covdock_success"))
        if covdock_success is False:
            failures.append("covdock_failed")
        elif covdock_success is None:
            warnings.append("covdock_success_not_explicit")
        link = _bool(row.get("covalent_link_verified"))
        clash = _bool(row.get("severe_clash_verified"))
        bond_length = _finite(row.get("covalent_bond_length_angstrom"))
        if coordinates:
            link = _bool(coordinates.get("coordinate_recheck_link"))
            clash = _bool(coordinates.get("coordinate_recheck_clash"))
            bond_length = _finite(coordinates.get("coordinate_recheck_bond_length"))
            if not geometry_evaluated:
                failures.append("coordinate_recheck_failed")
        strain = _finite(row.get("local_strain_energy"))
        if link is False:
            failures.append("specified_covalent_link_missing")
        elif link is None:
            warnings.append("covalent_link_not_verified")
        if clash is True:
            failures.append("severe_nonbonded_clash")
        elif clash is None:
            warnings.append("clash_check_missing")
        if bond_length is None:
            warnings.append("covalent_bond_length_missing")
        elif not min_covalent_bond_length <= bond_length <= max_covalent_bond_length:
            failures.append("covalent_bond_length_outside_gate")
        if strain is None:
            warnings.append("local_strain_missing")
        elif strain > hard_max_strain:
            failures.append("local_strain_above_hard_gate")
        elif strain > max_local_strain:
            warnings.append("local_strain_above_preferred_gate")

        # Geometry/strain above are upstream columns, not independent coordinate
        # measurements. Do not let a fully populated table imply revalidation.
        if not geometry_evaluated:
            warnings.append("independent_coordinate_evaluation_not_performed")
        independent_gate = "FAIL" if failures else "WARN" if warnings else "PASS"
        rows.append(
            {
                **row,
                **coordinates,
                "independent_chemistry_valid": chemistry_valid,
                "independent_fragment_count": fragment_count,
                "independent_formal_charge": formal_charge,
                "independent_radical_electron_count": radicals,
                "independent_warhead_match_count": warhead_matches,
                "independent_total_supported_warhead_count": sum(all_warhead_counts.values()),
                "independent_warhead_counts_json": json.dumps(all_warhead_counts, sort_keys=True),
                "high_reactivity_alert": high_reactivity_alert,
                "aggregation_risk": aggregation_risk,
                "pains_alert": pains_alert,
                "risk_annotation_method": (
                    "supported_warhead_count_gt_1;"
                    "RDKit_MolLogP_gt_5_and_MolWt_gt_500;"
                    "RDKit_PAINS_first_match"
                    if molecule is not None else "unavailable_invalid_smiles"
                ),
                "independent_parent_mcs_fraction": mcs_fraction,
                "independent_geometry_evaluated": geometry_evaluated,
                "physical_evidence_source": (
                    "raw_coordinate_recheck;strain_from_covdock"
                    if geometry_evaluated else "upstream_columns_only"
                ),
                "independent_gate": independent_gate,
                "independent_failure_reasons": ";".join(failures),
                "independent_warning_reasons": ";".join(warnings),
                "physical_validity": (
                    "FAIL" if failures else "WARN" if warnings else "PASS"
                ),
            }
        )
    return pd.DataFrame(rows).sort_values("input_sdf_index", kind="stable")


def write_ligand_first_evaluation(
    evaluated: pd.DataFrame,
    output_dir: Path,
    *,
    input_path: Path,
    campaign_path: Path,
    require_coordinate_recheck: bool = False,
) -> dict[str, Any]:
    original = (pd.read_parquet(input_path) if input_path.suffix.lower() == ".parquet"
                else pd.read_csv(input_path))
    identity_columns = ["input_sdf_index", "compound_id"]
    original_identity = original[identity_columns].sort_values(identity_columns).reset_index(drop=True)
    evaluated_identity = evaluated[identity_columns].sort_values(identity_columns).reset_index(drop=True)
    denominator_preserved = (
        len(original) > 0
        and original["input_sdf_index"].is_unique
        and original_identity.equals(evaluated_identity)
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    parquet = output_dir / "ligand_first_evaluated_candidates.parquet"
    csv = output_dir / "ligand_first_evaluated_candidates.csv"
    report = output_dir / "ligand_first_independent_evaluation.md"
    evaluated.to_parquet(parquet, index=False)
    evaluated.to_csv(csv, index=False)
    counts = evaluated["independent_gate"].value_counts().to_dict()
    report.write_text(
        "# Ligand-first independent evaluation\n\n"
        f"- Input candidates: {len(evaluated)}\n"
        f"- PASS: {int(counts.get('PASS', 0))}\n"
        f"- WARN: {int(counts.get('WARN', 0))}\n"
        f"- FAIL: {int(counts.get('FAIL', 0))}\n"
        "- Scope: independently re-parsed SMILES/warhead/scaffold retention, recomputed "
        "multi-warhead/aggregation/PAINS triage evidence, and "
        "re-applied AF3/CovDock physical thresholds. When supplied, raw-coordinate "
        "rechecks independently execute the existing geometry inspector; this is "
        "not a different scoring model. Strain remains a CovDock-derived value. "
        "See per-row independent_geometry_evaluated and source hashes. This is computational triage, "
        "not experimental proof of potency, selectivity, kinetics, or synthesis.\n"
    )
    gates = {
        "identity_unique": bool(evaluated["input_sdf_index"].is_unique),
        "input_denominator_preserved": bool(denominator_preserved),
        "at_least_one_nonfailed_candidate": bool(
            evaluated["independent_gate"].isin(["PASS", "WARN"]).any()
        ),
    }
    if require_coordinate_recheck:
        # Failed docking candidates stay in the denominator, but every candidate
        # considered for final ranking must have a completed coordinate audit.
        eligible = evaluated["independent_gate"].isin(["PASS", "WARN"])
        checked = evaluated.get(
            "independent_geometry_evaluated", pd.Series(False, index=evaluated.index)
        ).map(_bool).eq(True)
        gates["eligible_candidates_coordinate_rechecked"] = bool(
            eligible.any() and checked.loc[eligible].all()
        )
    manifest = {
        "schema_version": "1.0",
        "algorithm_version": "ligand-first-independent-evaluation-v1.0",
        "status": "completed" if all(gates.values()) else "quality_gate_failed",
        "generated_at": utc_now(),
        "input": {"path": str(input_path), "sha256": sha256_file(input_path)},
        "campaign": {
            "path": str(campaign_path),
            "sha256": sha256_file(campaign_path),
        },
        "counts": {key: int(value) for key, value in counts.items()},
        "coordinate_recheck_required": require_coordinate_recheck,
        "quality_gates": gates,
        "outputs": {
            path.name: {"path": str(path), "sha256": sha256_file(path)}
            for path in (parquet, csv, report)
        },
    }
    manifest_path = output_dir / "ligand_first_evaluation_manifest.json"
    temporary = manifest_path.with_name(f".{manifest_path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    temporary.replace(manifest_path)
    return manifest
