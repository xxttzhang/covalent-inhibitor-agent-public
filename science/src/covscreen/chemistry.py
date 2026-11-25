from __future__ import annotations

from pathlib import Path

from rdkit import Chem
from rdkit.Chem import AllChem


def mol_from_smiles(smiles: str):
    mol = Chem.MolFromSmiles(str(smiles))
    if mol is None:
        raise ValueError(f"invalid_smiles:{smiles}")
    return mol


def select_warhead_match(
    molecule: Chem.Mol,
    query: Chem.Mol,
    *,
    electrophilic_map_number: int = 1,
) -> tuple[tuple[int, ...], int]:
    """Select one reaction site with a deterministic, label-blind rule.

    The least substituted electrophilic atom is preferred. Canonical RDKit
    atom indices break any remaining tie, so multi-enamide inputs do not turn
    into label-correlated preparation failures.
    """
    matches = molecule.GetSubstructMatches(query)
    if not matches:
        raise ValueError("No warhead match found")
    query_index = next(
        (
            atom.GetIdx()
            for atom in query.GetAtoms()
            if atom.GetAtomMapNum() == electrophilic_map_number
        ),
        None,
    )
    if query_index is None:
        raise ValueError(
            f"Warhead SMARTS has no atom map {electrophilic_map_number}"
        )

    def key(match: tuple[int, ...]) -> tuple[int, tuple[int, ...]]:
        beta = molecule.GetAtomWithIdx(match[query_index])
        heavy_degree = sum(
            neighbor.GetAtomicNum() > 1 for neighbor in beta.GetNeighbors()
        )
        return heavy_degree, tuple(match)

    return min(matches, key=key), len(matches)


def prepare_sdf(frame: pd.DataFrame, output: str | Path, seed: int = 20260725) -> pd.DataFrame:
    # Keep pandas optional at module import time.  AlphaFold's native runtime
    # only needs the lightweight warhead-selection helpers and intentionally
    # does not carry the data-analysis stack.
    import pandas as pd

    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    failures: list[dict[str, object]] = []
    writer = Chem.SDWriter(str(output))
    kept = []
    for idx, row in frame.iterrows():
        try:
            mol = mol_from_smiles(row["standardized_smiles"])
            mol = Chem.AddHs(mol)
            params = AllChem.ETKDGv3()
            params.randomSeed = int(seed + idx)
            status = AllChem.EmbedMolecule(mol, params)
            if status != 0:
                raise ValueError("conformer_generation_failed")
            try:
                AllChem.UFFOptimizeMolecule(mol, maxIters=200)
            except Exception:
                pass
            mol = Chem.RemoveHs(mol)
            for col in frame.columns:
                value = row[col]
                if pd.notna(value):
                    mol.SetProp(str(col).upper(), str(value))
            writer.write(mol)
            kept.append(idx)
        except Exception as exc:
            failures.append({"compound_id": row.get("compound_id", f"row_{idx}"), "stage": "chemistry", "failure_type": type(exc).__name__, "failure_reason": str(exc)})
    writer.close()
    return pd.DataFrame(failures)
