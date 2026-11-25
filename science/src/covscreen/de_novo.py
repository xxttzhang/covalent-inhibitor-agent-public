"""Compatibility alias; maintain routes/de-novo/src."""
import sys
from covscreen.layout import enable_de_novo
enable_de_novo()
from covscreen_de_novo import de_novo as _implementation
sys.modules[__name__] = _implementation
