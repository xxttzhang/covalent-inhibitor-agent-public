from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "inspect_covdock_output.py"
SPEC = importlib.util.spec_from_file_location("inspect_covdock_output", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


@dataclass
class Atom:
    index: int
    chain: str
    resnum: int
    pdbres: str
    pdbname: str
    element: str
    xyz: tuple[float, float, float]
    bonded_atoms: list["Atom"] = field(default_factory=list)


@dataclass
class Structure:
    atom: list[Atom]


def connect(first: Atom, second: Atom) -> None:
    first.bonded_atoms.append(second)
    second.bonded_atoms.append(first)


def test_reaction_geometry_verifies_target_link_bond_length_and_clash() -> None:
    sg = Atom(1, "A", 797, "CYS", "SG", "S", (0.0, 0.0, 0.0))
    cb = Atom(2, "A", 797, "CYS", "CB", "C", (-1.8, 0.0, 0.0))
    electrophile = Atom(3, "", 1, "LIG", "C1", "C", (1.8, 0.0, 0.0))
    ligand = Atom(4, "", 1, "LIG", "C2", "C", (3.0, 0.0, 0.0))
    protein = Atom(5, "A", 798, "GLY", "CA", "C", (0.0, 3.0, 0.0))
    connect(sg, cb)
    connect(sg, electrophile)
    connect(electrophile, ligand)
    evidence = MODULE.inspect_reaction_geometry(
        Structure([sg, cb, electrophile, ligand, protein]),
        attachment_chain="A",
        attachment_residue=797,
        nucleophilic_atom="SG",
    )
    assert evidence["geometry_inspection_status"] == "SUCCESS"
    assert evidence["covalent_link_present"] is True
    assert evidence["covalent_bond_length_angstrom"] == 1.8
    assert evidence["covalent_partner_atom_index"] == 3
    assert evidence["ligand_heavy_atom_count"] == 2
    assert evidence["severe_clash"] is False


def test_reaction_geometry_reports_missing_external_link() -> None:
    sg = Atom(1, "A", 797, "CYS", "SG", "S", (0.0, 0.0, 0.0))
    cb = Atom(2, "A", 797, "CYS", "CB", "C", (-1.8, 0.0, 0.0))
    connect(sg, cb)
    evidence = MODULE.inspect_reaction_geometry(
        Structure([sg, cb]),
        attachment_chain="A",
        attachment_residue=797,
        nucleophilic_atom="SG",
    )
    assert evidence["geometry_inspection_status"] == "COVALENT_LINK_MISSING"
    assert evidence["covalent_link_present"] is False


def test_cached_coordinates_preserve_exhaustive_distance_and_bound_sdk_reads():
    import math
    import random
    randomizer = random.Random(7)

    class CountedAtom:
        def __init__(self, index, residue, name, xyz):
            self.index, self.pdbres, self.pdbname = index, residue, name
            self.chain, self.resnum = ("A", 797) if index == 1 else ("B", index)
            self.element, self.bonded_atoms = "S" if index == 1 else "C", []
            self.coordinates, self.reads = xyz, 0

        @property
        def xyz(self):
            self.reads += 1
            return self.coordinates

    target = CountedAtom(1, "CYS", "SG", (0., 0., 0.))
    ligand = [CountedAtom(i, "LIG", "C", (1.8 + i / 10, 0., 0.)) for i in range(2, 12)]
    protein = [target] + [CountedAtom(i, "GLY", "CA",
        tuple(randomizer.uniform(-5, 5) for _ in range(3))) for i in range(12, 212)]
    connect(target, ligand[0])
    for first, second in zip(ligand, ligand[1:]):
        connect(first, second)
    expected = min(math.dist(l.coordinates, p.coordinates) for l in ligand for p in protein
                   if (l.index, p.index) != (2, 1))
    actual = MODULE.inspect_reaction_geometry(Structure(protein + ligand),
        attachment_chain="A", attachment_residue=797, nucleophilic_atom="SG")
    assert actual["minimum_nonbonded_heavy_atom_distance_angstrom"] == expected
    assert actual["severe_clash"] == (expected < 1.2)
    assert max(atom.reads for atom in protein + ligand) <= 2


def test_structure_properties_cannot_override_real_entry_index_or_title():
    structure = SimpleNamespace(title="actual__i7", property={"pose_index": 42, "title": "forged__i8"})
    rows, _ = MODULE.inspect_structures([structure])
    assert rows[0]["pose_index"] == 0
    assert rows[0]["title"] == "actual__i7"


@pytest.mark.parametrize("coordinates", [(float("nan"), 0, 0), (0, float("inf"), 0), (0, 1)])
def test_nonfinite_or_incomplete_coordinates_fail_closed(coordinates):
    with pytest.raises(ValueError, match="three finite"):
        MODULE._coordinates(SimpleNamespace(xyz=coordinates))


@pytest.mark.parametrize("residue,element,status", [("HIS", "S", "TARGET_NOT_CYSTEINE_SULFUR"),
    ("CYS", "C", "TARGET_NOT_CYSTEINE_SULFUR"), ("CYS", "S", "PARTNER_NOT_LIGAND_HEAVY_ATOM")])
def test_wrong_target_or_disulfide_link_is_not_ligand_geometry(residue, element, status):
    target = Atom(1, "A", 797, residue, "SG", element, (0., 0., 0.))
    partner = Atom(2, "A", 798, "CYS", "SG", "S", (1.8, 0., 0.))
    connect(target, partner)
    result = MODULE.inspect_reaction_geometry(Structure([target, partner]), attachment_chain="A",
        attachment_residue=797, nucleophilic_atom="SG")
    assert result["geometry_inspection_status"] == status
    assert result["covalent_link_present"] is False


def test_readback_request_has_no_command_or_output_write_parameters():
    request = {"version": "covdock-structure-readback-v1", "files": ["/inspire/pose.maegz"], "attachment_residue": "A:797"}
    assert MODULE.readback_request(request) == request
    for changed in ({**request, "command": "something"}, {**request, "files": []},
                    {**request, "files": ["/inspire/../old.mae"]}, {**request, "attachment_residue": "B:797"},
                    {**request, "files": request["files"] * 2}):
        with pytest.raises(ValueError):
            MODULE.readback_request(changed)
