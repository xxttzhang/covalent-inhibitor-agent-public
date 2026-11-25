"""Compatibility import; maintain routes/ligand-first/src instead."""
import sys
from covscreen.layout import enable_ligand_first
enable_ligand_first()
from covscreen_ligand_first import ligand_acceptance as _implementation
sys.modules[__name__] = _implementation
