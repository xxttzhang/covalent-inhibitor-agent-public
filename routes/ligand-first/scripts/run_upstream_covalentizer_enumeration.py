"""Run upstream CovUtils.multi_react without invoking PBS or DOCKovalent.

Input is a SMILES file of preselected fragments. This adapter does not perform
fragment selection, attachment planning, docking, or claim a complete campaign.
"""
from __future__ import annotations

import _bootstrap  # canonical route and shared runtime, independent of cwd

import argparse
import importlib.util
import json
import os
import hashlib
from pathlib import Path

from rdkit import Chem

from covscreen.jobs import sha256_file
from covscreen.upstream_parent_mapping import parent_mapping_evidence


def enumerate_traced(module, install_rules: Path, restore_rules: Path,
                     fragment_file: Path, out: Path) -> list[dict]:
    """Isolate upstream calls so its global deduplication cannot erase lineage.

    Input IDs identify submitted fragments, not verified parent atom mappings.
    Duplicate structures deliberately retain one row per originating record.
    """
    inputs = []
    for line_number, line in enumerate(fragment_file.read_text().splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        fields = line.split(maxsplit=1)
        mol = Chem.MolFromSmiles(fields[0])
        if mol is None:
            raise ValueError(f"invalid fragment SMILES at input line {line_number}")
        inputs.append((line_number, fields[0], fields[1] if len(fields) > 1 else None))
    rows = []
    for line_number, smiles, input_id in inputs:
        work = out / f"fragment_{line_number:06d}"
        work.mkdir(parents=True, exist_ok=True)
        source = work / "input.smi"
        source.write_text(smiles + "\n")
        intermediate = work / "silicon_intermediates.smi"
        product_file = work / "restored_products.smi"
        module.multi_react([str(install_rules), str(source), str(intermediate)])
        module.multi_react([str(restore_rules), str(intermediate), str(product_file)])
        for product_number, line in enumerate(product_file.read_text().splitlines(), 1):
            product_smiles, upstream_id = line.split()
            mol = Chem.MolFromSmiles(product_smiles)
            silicon = bool(mol and any(a.GetAtomicNum() == 14 for a in mol.GetAtoms()))
            single = bool(mol and len(Chem.GetMolFrags(mol)) == 1)
            radical = bool(mol and any(a.GetNumRadicalElectrons() for a in mol.GetAtoms()))
            accepted = mol is not None and not silicon and single and not radical
            rows.append({
                "candidate_id": f"fragment_{line_number:06d}_product_{product_number:06d}",
                "input_line": line_number, "input_id": input_id,
                "input_smiles": smiles, "upstream_id": upstream_id,
                "smiles": product_smiles,
                "structure_sha256": hashlib.sha256(
                    (Chem.MolToSmiles(mol) if mol else product_smiles).encode()).hexdigest(),
                "sanitization_pass": mol is not None, "silicon_remaining": silicon,
                "single_component": single, "radical_present": radical,
                "chemical_gate": "PASS" if accepted else "FAIL",
                "source_product_file": str(product_file),
                "source_product_sha256": sha256_file(product_file),
            })
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream-repo", type=Path, required=True)
    parser.add_argument("--fragment-smiles", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--parent-smiles-file", type=Path,
                        help="One parent SMILES record shared by all submitted fragments.")
    parser.add_argument("--min-parent-fraction", type=float, default=0.6)
    parser.add_argument("--protected-core-smarts")
    parser.add_argument("--export-docking-inputs", action="store_true")
    parser.add_argument("--target-id")
    parser.add_argument("--site-id")
    parser.add_argument("--parent-id")
    args = parser.parse_args()
    if args.export_docking_inputs and not all(
        (args.parent_smiles_file, args.target_id, args.site_id, args.parent_id)
    ):
        parser.error("docking export requires parent SMILES file, target ID, site ID and parent ID")
    repo = args.upstream_repo.resolve()
    source = repo / "Code/CovUtils.py"
    install_rules = repo / "Code/numbered_reaction.re"
    restore_rules = repo / "Scripts/reform_elec.re"
    inputs = [source, install_rules, restore_rules, args.fragment_smiles.resolve()]
    for path in inputs:
        if not path.is_file():
            parser.error(f"required upstream input missing: {path}")
    # CovUtils evaluates a default parameter from this environment at import.
    previous = os.environ.get("COVALENTIZER")
    try:
        os.environ["COVALENTIZER"] = str(repo)
        spec = importlib.util.spec_from_file_location("upstream_covutils", source)
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
    finally:
        if previous is None:
            os.environ.pop("COVALENTIZER", None)
        else:
            os.environ["COVALENTIZER"] = previous
    out = args.output_dir.resolve()
    out.mkdir(parents=True, exist_ok=True)
    rows = enumerate_traced(module, install_rules, restore_rules,
                            args.fragment_smiles.resolve(), out)
    if args.parent_smiles_file:
        parent_file = args.parent_smiles_file.resolve()
        parent_records = [line for line in parent_file.read_text().splitlines()
                          if line.strip() and not line.lstrip().startswith("#")]
        if len(parent_records) != 1:
            parser.error("--parent-smiles-file must contain exactly one parent record")
        inputs.append(parent_file)
        for row in rows:
            row["parent_mapping"] = (parent_mapping_evidence(
                parent_records[0].split()[0], row["input_smiles"], row["smiles"],
                min_parent_fraction=args.min_parent_fraction,
                protected_core_smarts=args.protected_core_smarts,
            ) if row["chemical_gate"] == "PASS" else
                {"status": "FAIL", "failure_reasons": ["CHEMISTRY_GATE_FAIL"]})
    accepted = [row for row in rows if row["chemical_gate"] == "PASS"
                and row.get("parent_mapping", {}).get("status", "PASS") == "PASS"]
    products = out / "accepted_products.smi"
    products.write_text("".join(f'{row["smiles"]}\t{row["candidate_id"]}\n' for row in accepted))
    manifest = {
        "status": "completed" if accepted else "no_candidates",
        "upstream": "https://github.com/LondonLab/Covalentizer",
        "method": "unmodified CovUtils.multi_react, installation then electrophile restoration",
        "input_sha256": {str(p): sha256_file(p) for p in inputs},
        "candidate_count": len(rows),
        "accepted_count": len(accepted),
        "parent_gate_evaluated": args.parent_smiles_file is not None,
        "rejected_count": len(rows) - len(accepted),
        "candidates": rows,
        "outputs": {products.name: {"path": str(products), "sha256": sha256_file(products)}},
        "limitations": ["Input fragments must already be selected and traced to their parent.",
                        "Upstream sulfur rules produce vinyl sulfonamides.",
                        "Optional parent mapping reports graph correspondence, not reaction atom ancestry.",
                        "Fragmentation, four-warhead route handoff and docking are not implemented by this adapter."],
    }
    (out / "upstream_enumeration_manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    if args.export_docking_inputs:
        from covscreen.upstream_handoff import export_handoff
        handoff = export_handoff(out / "upstream_enumeration_manifest.json", out / "docking_inputs",
                                 target_id=args.target_id, site_id=args.site_id, parent_id=args.parent_id)
        if not handoff["accepted_count"]:
            return 4
    print(json.dumps({"status": manifest["status"], "candidate_count": len(rows)}))
    return 0 if accepted else 4


if __name__ == "__main__":
    raise SystemExit(main())
