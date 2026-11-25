"""Use the shared package shipped beside this route, never an unrelated checkout."""
from pathlib import Path
import sys

ROUTE = Path(__file__).resolve().parents[1]
ROOT = ROUTE.parents[1]
PROJECT = ROOT / 'science'
if not (PROJECT / 'src/covscreen').is_dir():
    raise RuntimeError('Shared covscreen package is missing beside this route')
sys.path[:0] = [str(ROUTE / 'src'), str(PROJECT / 'src')]
