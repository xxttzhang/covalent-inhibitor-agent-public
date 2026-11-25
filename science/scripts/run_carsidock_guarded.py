"""Launch unchanged upstream inference with declared-reaction product filtering."""
import argparse
import json
import runpy
import sys
from pathlib import Path

from covscreen.carsidock_reaction_guard import select_declared_products


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--carsidock-repo", type=Path, required=True)
    parser.add_argument("upstream_args", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    repo = args.carsidock_repo.resolve()
    sys.path.insert(0, str(repo))
    from src.utils import cov_prepare
    original = cov_prepare.mol_edit

    def guarded(mol, *positional, **keywords):
        products = original(mol, *positional, **keywords)
        selected = select_declared_products(mol, products)
        print(json.dumps({"reaction_guard": True,
                          "compound": mol.GetProp("_Name") if mol.HasProp("_Name") else None,
                          "generated": len(products), "retained": len(selected),
                          "rejected": len(products) - len(selected)}), file=sys.stderr)
        return selected

    cov_prepare.mol_edit = guarded
    upstream = args.upstream_args
    if upstream and upstream[0] == "--":
        upstream = upstream[1:]
    entry = repo / "run_carsidockcov_screening.py"
    sys.argv = [str(entry), *upstream]
    runpy.run_path(str(entry), run_name="__main__")


if __name__ == "__main__":
    main()
