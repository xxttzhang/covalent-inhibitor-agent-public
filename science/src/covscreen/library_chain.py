"""Compatibility import; maintain the route package instead."""
import sys
from covscreen.layout import enable_library_first
enable_library_first()
from covscreen_library_first import library_chain as _implementation
sys.modules[__name__] = _implementation
