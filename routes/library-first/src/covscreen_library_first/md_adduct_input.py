"""Compatibility exports for the shared covalent-adduct admission implementation."""
from covscreen.md_adduct_input import (
    sha,
    _regular_json,
    _candidate,
    _parents,
    _graph_qc,
    admit_covalent_adduct_input,
    load_covalent_adduct_directory,
    load_covalent_adduct_input,
    SUPPORTED_REACTIONS
)

__all__ = ['sha', '_regular_json', '_candidate', '_parents', '_graph_qc', 'admit_covalent_adduct_input', 'load_covalent_adduct_directory', 'load_covalent_adduct_input', 'SUPPORTED_REACTIONS']
