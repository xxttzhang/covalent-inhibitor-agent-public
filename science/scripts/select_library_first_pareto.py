"""Legacy shared Pareto command; no dependency on a route-specific implementation."""
from pathlib import Path
import sys

_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_root / 'src'))
_source = _root / 'src/covscreen/pareto_cli.py'
exec(compile(_source.read_bytes(), str(_source), 'exec'), globals())
