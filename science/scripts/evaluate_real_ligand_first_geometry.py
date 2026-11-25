"""Compatibility command; canonical source lives in routes/ligand-first."""
import sys
from pathlib import Path
import runpy
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from covscreen.layout import ligand_script
_target = ligand_script(Path(__file__).resolve().parents[1], "evaluate_real_ligand_first_geometry.py")
sys.path.insert(0, str(_target.parent))
if __name__ == "__main__":
    runpy.run_path(str(_target), run_name="__main__")
else:
    exec(compile(_target.read_bytes(), str(_target), "exec"), globals())
