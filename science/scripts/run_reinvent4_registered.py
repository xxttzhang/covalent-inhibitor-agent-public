"""Compatibility command; maintain routes/de-novo/scripts."""
import sys
from pathlib import Path
import runpy
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/"src"))
from covscreen.layout import de_novo_script
_target=de_novo_script("run_reinvent4_registered.py")
sys.path.insert(0,str(_target.parent))
if __name__=="__main__":
    runpy.run_path(str(_target),run_name="__main__")
else:
    exec(compile(_target.read_bytes(),str(_target),"exec"),globals())
