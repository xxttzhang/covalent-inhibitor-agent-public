import numpy as np
import pytest

from covscreen.pose_consistency import aligned_ligand_rmsd


def test_aligned_ligand_rmsd_removes_rigid_body_transform():
    moving_protein = np.asarray([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
    moving_ligand = np.asarray([[0.2, 0.3, 1.0], [1.2, 0.3, 1.0]], dtype=float)
    rotation = np.asarray([[0, -1, 0], [1, 0, 0], [0, 0, 1]], dtype=float)
    translation = np.asarray([4.0, -2.0, 3.0])
    fixed_protein = moving_protein @ rotation + translation
    fixed_ligand = moving_ligand @ rotation + translation

    result = aligned_ligand_rmsd(
        fixed_protein, moving_protein, fixed_ligand, moving_ligand
    )

    assert result["protein_alignment_rmsd_angstrom"] == pytest.approx(0, abs=1e-12)
    assert result["af3_covdock_ligand_rmsd_angstrom"] == pytest.approx(0, abs=1e-12)


def test_aligned_ligand_rmsd_preserves_internal_pose_difference():
    protein = np.asarray([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype=float)
    fixed_ligand = np.asarray([[0, 0, 1], [1, 0, 1]], dtype=float)
    moving_ligand = np.asarray([[0, 0, 1], [3, 0, 1]], dtype=float)

    result = aligned_ligand_rmsd(
        protein, protein, fixed_ligand, moving_ligand
    )

    assert result["af3_covdock_ligand_rmsd_angstrom"] == pytest.approx(np.sqrt(2))
