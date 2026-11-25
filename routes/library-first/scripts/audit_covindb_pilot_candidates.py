#!/usr/bin/env python3
"""Rank label-free CovInDB target-site candidates for a lock-box pilot."""

from __future__ import annotations

import _bootstrap  # resolve this release, independent of cwd

import argparse
import csv
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from rdkit import Chem
from rdkit.Chem.Scaffolds import MurckoScaffold


ACRYLAMIDE = Chem.MolFromSmarts("C=CC(=O)N")


@dataclass
class CompoundEvidence:
    smiles: str
    best_micromolar: float | None = None


def concentration_micromolar(value: str, unit: str, relation: str) -> float | None:
    if ">" in relation:
        return None
    try:
        number = float(re.sub(r"[^0-9.eE+-]", "", value))
    except ValueError:
        return None
    normalized = (
        unit.strip()
        .lower()
        .replace("μ", "u")
        .replace("µ", "u")
        .replace("&micro;", "u")
    )
    factors = {
        "pm": 1e-6,
        "nm": 1e-3,
        "um": 1.0,
        "mm": 1e3,
        "m": 1e6,
    }
    factor = factors.get(normalized)
    return None if factor is None else number * factor


def scaffold_smiles(mol: Chem.Mol) -> str:
    scaffold = MurckoScaffold.GetScaffoldForMol(mol)
    return Chem.MolToSmiles(scaffold, canonical=True) if scaffold.GetNumAtoms() else ""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--threshold-um", type=float, default=10.0)
    parser.add_argument("--minimum-potent", type=int, default=20)
    args = parser.parse_args()

    groups: dict[tuple[str, str, str, str], dict[str, CompoundEvidence]] = defaultdict(dict)
    invalid_smiles = 0
    with args.input.open(encoding="utf-8-sig", errors="replace", newline="") as handle:
        for row in csv.DictReader(handle):
            site = row.get("Site", "").strip().upper()
            if not re.fullmatch(r"CYS-\d+", site):
                continue
            reaction = f'{row.get("Reaction_type", "")} {row.get("War_head", "")}'.upper()
            if "MICHAEL" not in reaction:
                continue
            taxonomy = row.get("Target_Taxonomy", "").strip().lower()
            if taxonomy not in {"human", "homo sapiens"} and "homo sapiens" not in taxonomy:
                continue
            smiles = row.get("SMILES", "").strip()
            mol = Chem.MolFromSmiles(smiles)
            if mol is None:
                invalid_smiles += 1
                continue
            if ACRYLAMIDE is None or not mol.HasSubstructMatch(ACRYLAMIDE):
                continue
            identity = row.get("InChI_Key", "").strip() or Chem.MolToSmiles(mol, canonical=True)
            key = (
                row.get("Target_Gene", "").strip(),
                row.get("Target_Name", "").strip(),
                row.get("Target", "").strip(),
                site,
            )
            evidence = groups[key].setdefault(identity, CompoundEvidence(smiles=smiles))
            potency = concentration_micromolar(
                row.get("Value", ""), row.get("Unit", ""), row.get("Relation", "")
            )
            if potency is not None and (
                evidence.best_micromolar is None or potency < evidence.best_micromolar
            ):
                evidence.best_micromolar = potency

    ranked = []
    for key, compounds in groups.items():
        potent = [x for x in compounds.values() if x.best_micromolar is not None and x.best_micromolar <= args.threshold_um]
        scaffolds = {
            scaffold_smiles(mol)
            for item in potent
            if (mol := Chem.MolFromSmiles(item.smiles)) is not None
        }
        scaffolds.discard("")
        ranked.append((len(potent), len(scaffolds), len(compounds), key))

    print("potent\tscaffolds\tacrylamides\tgene\ttarget\tuniprot\tsite")
    for potent_count, scaffold_count, total_count, key in sorted(
        ranked, key=lambda item: (-item[0], -item[1], item[3])
    ):
        if potent_count >= args.minimum_potent:
            print(
                f"{potent_count}\t{scaffold_count}\t{total_count}\t"
                + "\t".join(key)
            )
    print(f"invalid_smiles={invalid_smiles}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
