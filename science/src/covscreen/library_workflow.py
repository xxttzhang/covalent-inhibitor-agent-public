"""Compatibility import; Library-first workflow lives in its route package."""
from .layout import enable_library_first

enable_library_first()
from covscreen_library_first.workflow import (  # noqa: E402,F401
    ENTRY, RESEARCH, RECEPTOR, INTERFACE, navigate, step, receptor_start_request,
)
