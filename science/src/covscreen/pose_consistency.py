from __future__ import annotations

import numpy as np
from Bio.SVDSuperimposer import SVDSuperimposer


def protein_residue_identity(name: str) -> str:
    """Sequence identity only: protonation labels do not change the CA mapping.

    This does not equate chemical states or ligand graphs for energy comparison.
    """
    value = name.strip().upper()
    return {"HID": "HIS", "HIE": "HIS", "HIP": "HIS", "ASH": "ASP",
            "GLH": "GLU", "LYN": "LYS", "CYM": "CYS", "CYX": "CYS"}.get(value, value)


def aligned_ligand_rmsd(
    fixed_protein: np.ndarray,
    moving_protein: np.ndarray,
    fixed_ligand: np.ndarray,
    moving_ligand: np.ndarray,
) -> dict[str, float]:
    """Align ``moving`` protein coordinates onto ``fixed`` and score the ligand.

    Protein and ligand arrays must already use identical, audited atom order.
    The returned ligand RMSD is therefore not affected by pose-file ordering.
    """
    arrays = tuple(
        np.asarray(value, dtype=float)
        for value in (fixed_protein, moving_protein, fixed_ligand, moving_ligand)
    )
    fixed_protein, moving_protein, fixed_ligand, moving_ligand = arrays
    if fixed_protein.shape != moving_protein.shape:
        raise ValueError("protein coordinate arrays must have identical shape")
    if fixed_ligand.shape != moving_ligand.shape:
        raise ValueError("ligand coordinate arrays must have identical shape")
    if fixed_protein.ndim != 2 or fixed_protein.shape[1] != 3:
        raise ValueError("protein coordinates must have shape (N, 3)")
    if fixed_ligand.ndim != 2 or fixed_ligand.shape[1] != 3:
        raise ValueError("ligand coordinates must have shape (N, 3)")
    if len(fixed_protein) < 3:
        raise ValueError("at least three protein atoms are required for alignment")
    if len(fixed_ligand) < 1:
        raise ValueError("at least one ligand atom is required for RMSD")
    if not all(np.isfinite(value).all() for value in arrays):
        raise ValueError("coordinates must be finite")

    superimposer = SVDSuperimposer()
    superimposer.set(fixed_protein, moving_protein)
    superimposer.run()
    rotation, translation = superimposer.get_rotran()
    transformed_ligand = moving_ligand @ rotation + translation
    ligand_rmsd = float(
        np.sqrt(np.mean(np.sum((transformed_ligand - fixed_ligand) ** 2, axis=1)))
    )
    return {
        "protein_alignment_rmsd_angstrom": float(superimposer.get_rms()),
        "af3_covdock_ligand_rmsd_angstrom": ligand_rmsd,
    }
