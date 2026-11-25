from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd
from rdkit import Chem
from rdkit.Chem import rdDepictor

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from covscreen.covalent_enumerator import CovalentEnumerationRequest, enumerate_covalent_candidates


def main() -> int:
    parser = argparse.ArgumentParser(description="Run deterministic, traceable RDKit warhead enumeration.")
    parser.add_argument("--request", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, required=True)
    parser.add_argument("--output-csv", type=Path, required=True)
    parser.add_argument("--output-sdf", type=Path)
    parser.add_argument("--output-table", type=Path)
    args = parser.parse_args()
    request = CovalentEnumerationRequest.model_validate_json(args.request.read_text())
    result = enumerate_covalent_candidates(request)
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False) + "\n")
    with args.output_csv.open("w", newline="") as handle:
        fields = [
            "rank", "candidate_id", "canonical_smiles", "scaffold_attachment_atom_index", "warhead_name",
            "linker_name", "linker_heavy_atom_count",
            "warhead_attachment_smarts", "reaction_class", "covalent_mode",
            "intrinsic_reactivity_tier", "chemistry_gate_pass", "scaffold_retained",
            "single_component", "formal_charge", "radical_electron_count",
            "warhead_match_count", "provenance",
        ]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for candidate in result.candidates:
            writer.writerow(candidate.model_dump(mode="json"))
    rows = []
    for index, candidate in enumerate(result.candidates):
        rows.append(
            {
                "compound_id": candidate.candidate_id,
                "protomer_id": candidate.candidate_id,
                "input_sdf_index": index,
                "standardized_smiles": candidate.canonical_smiles,
                "label": "candidate",
                "attachment_atom_index": candidate.scaffold_attachment_atom_index,
                "warhead_class": candidate.warhead_name,
                "linker_name": candidate.linker_name,
                "linker_heavy_atom_count": candidate.linker_heavy_atom_count,
                "reaction_type": candidate.reaction_class,
                "chemistry_gate_pass": candidate.chemistry_gate_pass,
                "intrinsic_reactivity_tier": candidate.intrinsic_reactivity_tier,
                "provenance": candidate.provenance,
            }
        )
    table = pd.DataFrame(rows)
    if args.output_table:
        args.output_table.parent.mkdir(parents=True, exist_ok=True)
        if args.output_table.suffix.lower() == ".parquet":
            table.to_parquet(args.output_table, index=False)
        else:
            table.to_csv(args.output_table, index=False)
    if args.output_sdf:
        args.output_sdf.parent.mkdir(parents=True, exist_ok=True)
        writer = Chem.SDWriter(str(args.output_sdf))
        try:
            for row in rows:
                molecule = Chem.MolFromSmiles(row["standardized_smiles"])
                if molecule is None:
                    continue
                rdDepictor.Compute2DCoords(molecule)
                molecule.SetProp("_Name", row["compound_id"])
                molecule.SetProp("COMPOUND_ID", row["compound_id"])
                molecule.SetIntProp("INPUT_SDF_INDEX", row["input_sdf_index"])
                molecule.SetProp("WARHEAD_CLASS", row["warhead_class"])
                molecule.SetProp("LINKER_NAME", row["linker_name"])
                writer.write(molecule)
        finally:
            writer.close()
    print(json.dumps({"status": result.status, "candidate_count": len(result.candidates)}))
    return 0 if result.status == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
