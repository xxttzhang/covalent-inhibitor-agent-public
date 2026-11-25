from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from covscreen.af3_results import parse_af3_job


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-dir", type=Path, required=True)
    ap.add_argument("--input-json", type=Path, required=True)
    ap.add_argument(
        "--target-config",
        type=Path,
        default=Path("configs/targets/BTK.yaml"),
    )
    ap.add_argument("--manifest-csv", type=Path)
    ap.add_argument("--identity-json", type=Path)
    ap.add_argument("--ligand-smiles")
    ap.add_argument("--output-prefix", type=Path, required=True)
    args = ap.parse_args()

    af3_input = json.loads(args.input_json.read_text())
    ligand = next(
        entity["ligand"]
        for entity in af3_input["sequences"]
        if "ligand" in entity
    )
    ligand_smiles = ligand.get("smiles") or args.ligand_smiles
    if not ligand_smiles:
        raise ValueError(
            "--ligand-smiles is required for userCCD ligand result parsing"
        )
    result = parse_af3_job(
        args.job_dir,
        ligand_smiles=ligand_smiles,
        target_config=args.target_config,
    )
    result["input_json"] = str(args.input_json.resolve())
    result["ligand_smiles"] = ligand_smiles
    result["covalent_bond_constraint"] = bool(
        af3_input.get("bondedAtomPairs")
    )

    if args.manifest_csv:
        manifest = pd.read_csv(args.manifest_csv)
        row = manifest.loc[
            manifest["json_path"].map(lambda value: Path(value).name)
            == args.input_json.name
        ]
        if len(row) != 1:
            raise ValueError(
                f"Expected one identity row for {args.input_json.name}, "
                f"found {len(row)}"
            )
        for key, value in row.iloc[0].items():
            result[key] = value.item() if hasattr(value, "item") else value
    if args.identity_json:
        identity = json.loads(args.identity_json.read_text())
        for key, value in identity.items():
            if key not in {"input_json", "user_ccd"}:
                result[key] = value

    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    json_path = args.output_prefix.with_suffix(".json")
    csv_path = args.output_prefix.with_suffix(".csv")
    json_path.write_text(json.dumps(result, indent=2) + "\n")
    pd.DataFrame([result]).to_csv(csv_path, index=False)
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
