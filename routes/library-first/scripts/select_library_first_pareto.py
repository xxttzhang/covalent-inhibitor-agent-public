"""Library-first command entry; ranking and traceability are shared with Ligand-first."""
import _bootstrap
from pathlib import Path

_source = _bootstrap.PROJECT / 'src/covscreen/pareto_cli.py'
exec(compile(_source.read_bytes(), str(_source), 'exec'), globals())
