"""Convert one audited Schrodinger CMS into an OpenMM-readable topology.

Conversion and independent force-field auditing are separate gates.  This
module writes Gromacs/OpenMM inputs while preserving source constraints and
records exact evidence, but it never declares the system MD-ready.
"""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import re
import shlex
import sys


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _regular(path, digest, label):
    path = Path(path)
    if (path.is_symlink() or not path.is_file()
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or sha(path) != digest):
        raise ValueError(label + " checksum mismatch")
    return path


def read_cms_tables(source):
    """Read indexed CMS tables without interpreting force-field values."""
    lines = Path(source).read_text().splitlines()
    blocks = []
    index = 0
    while index < len(lines):
        line = lines[index].strip()
        if line == "f_m_ct {":
            blocks.append({})
        match = re.match(r"(m_atom|ffio_\w+)\[(\d+)\]\s*\{", line)
        if not match:
            index += 1
            continue
        if not blocks:
            raise ValueError("CMS table appears outside a component block")
        name, count = match.group(1), int(match.group(2))
        cursor = index + 1
        keys = []
        while cursor < len(lines) and lines[cursor].strip() != ":::":
            if lines[cursor].strip() and not lines[cursor].lstrip().startswith("#"):
                keys.append(lines[cursor].strip())
            cursor += 1
        if cursor >= len(lines):
            raise ValueError("CMS table header is unterminated")
        if name == "m_atom":
            blocks[-1]["atom_count"] = count
        else:
            rows = []
            for raw in lines[cursor + 1:cursor + count + 1]:
                values = shlex.split(raw)
                if len(values) != len(keys) + 1:
                    raise ValueError("CMS table row has an unexpected width: " + name)
                rows.append(dict(zip(keys, values[1:])))
            blocks[-1][name] = rows
        index = cursor + count + 2
    if len(blocks) < 2 or any("atom_count" not in block for block in blocks):
        raise ValueError("CMS component inventory is incomplete")
    return blocks


def normalize_component_titles(source, destination):
    """Give repeated CMS components unique titles and prove no other edits."""
    original = Path(source).read_bytes()
    lines = original.decode().splitlines(keepends=True)
    changes = []
    for start, line in enumerate(lines):
        if line.strip() != "f_m_ct {":
            continue
        end = next((i for i in range(start + 1, len(lines)) if lines[i].strip() == ":::"), None)
        if end is None:
            raise ValueError("CMS component header is unterminated")
        headers = [value.strip() for value in lines[start + 1:end]]
        if "s_m_title" not in headers:
            raise ValueError("CMS component lacks s_m_title")
        position = end + 1 + headers.index("s_m_title")
        if position >= len(lines):
            raise ValueError("CMS component title value is absent")
        replacement = " ct_%03d \n" % len(changes)
        changes.append({"line_0based": position, "original": lines[position],
                        "replacement": replacement})
        lines[position] = replacement
    if not changes:
        raise ValueError("CMS contains no component titles")
    restored = list(lines)
    for change in changes:
        restored[change["line_0based"]] = change["original"]
    if "".join(restored).encode() != original:
        raise ValueError("CMS normalization changed content beyond titles")
    normalized = "".join(lines)
    Path(destination).write_text(normalized)
    return {"source_sha256": hashlib.sha256(original).hexdigest(),
            "normalized_sha256": hashlib.sha256(normalized.encode()).hexdigest(),
            "only_title_lines_changed": True, "changes": changes}


# Historical import remains available to existing consumers.
from covscreen.md_parameter_contract import parameter_contract as _parameter_contract


def convert_parameterized_cms(source, source_sha256, parameter_audit,
                              parameter_audit_sha256, intermol_source, output):
    """Write a hash-bound conversion pending independent force-field audit."""
    source = _regular(source, source_sha256, "Parameterized CMS")
    parameter_path = _regular(parameter_audit, parameter_audit_sha256, "Parameter audit")
    audit = _parameter_contract(json.loads(parameter_path.read_text()), source_sha256)
    intermol_source = Path(intermol_source).resolve()
    if (intermol_source.is_symlink() or not intermol_source.is_dir()
            or not (intermol_source / "intermol/desmond/desmond_parser.py").is_file()):
        raise ValueError("Registered InterMol source is unavailable")
    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("OpenMM conversion output must be a new directory")
    output.mkdir(parents=True, exist_ok=False)

    # Imports are delayed so contract failures do not depend on this optional runtime.
    sys.path.insert(0, str(intermol_source))
    try:
        import numpy as np
        import parmed.unit as units
        from intermol.desmond import desmond_parser
        from intermol.forces.constraint import Constraint
        from intermol.gromacs import gromacs_parser
    finally:
        if sys.path[0] == str(intermol_source):
            sys.path.pop(0)

    class CompleteConstraintWriter(gromacs_parser.GromacsParser):
        def write_moleculetypes(self, top):
            for mol_name, mol_type in self.system.molecule_types.items():
                self.current_molecule_type = mol_type
                if mol_type.rigidwaters:
                    raise ValueError("Rigid-water parser state was not replaced")
                top.write("[ moleculetype ]\n" + mol_name.replace(" ", "_").replace('"', "")
                          + " " + str(mol_type.nrexcl) + "\n\n")
                self.write_atoms(top)
                if mol_type.pair_forces:
                    self.write_pairs(top)
                if mol_type.bond_forces or mol_type.constraints:
                    self.write_bonds(top)
                if mol_type.angle_forces:
                    self.write_angles(top)
                if mol_type.dihedral_forces:
                    self.write_dihedrals(top)
                if mol_type.virtual_forces:
                    self.write_virtual_sites(top)
                if mol_type.exclusions:
                    self.write_exclusions(top)

        def write_bonds(self, top):
            super().write_bonds(top)
            constraints = self.current_molecule_type.constraints
            if not constraints:
                return
            top.write("[ constraints ]\n; source FFIO constraints, unchanged distances\n")
            pairs = {}
            for constraint in constraints:
                if constraint.type not in {"AH1", "AH2", "AH3"}:
                    raise ValueError("Unsupported reconstructed constraint " + constraint.type)
                for offset in range(1, constraint.n + 1):
                    pair = tuple(sorted((constraint.atom1,
                        getattr(constraint, "atom" + str(offset + 1)))))
                    length = getattr(constraint, "length" + str(offset)).value_in_unit(units.nanometer)
                    if pair in pairs and abs(pairs[pair] - length) >= 1e-12:
                        raise ValueError("Conflicting source constraint lengths")
                    pairs[pair] = length
            for (first, second), length in sorted(pairs.items()):
                top.write(f"{first} {second} 1 {length:.12f}\n")
            top.write("\n")

    unique = output / "unique.cms"
    normalization = normalize_component_titles(source, unique)
    (output / "title-normalization.json").write_text(
        json.dumps(normalization, indent=2, allow_nan=False) + "\n")
    system = desmond_parser.load(str(unique))
    if system.n_atoms != audit["total_atoms"]:
        raise ValueError("Atom count changed during CMS conversion")
    blocks = read_cms_tables(source)
    components = list(system.molecule_types.values())
    if len(blocks) - 1 != len(components):
        raise ValueError("CMS and InterMol component counts differ")
    for block, component in zip(blocks[1:], components):
        component.rigidwaters.clear()
        component.constraints.clear()
        pairs = {}
        for row in block.get("ffio_constraints", []):
            kind = row["s_ffio_funct"]
            first = int(row["i_ffio_ai"])
            if kind.startswith("AH") and kind[2:].isdigit():
                for number in range(1, int(kind[2:]) + 1):
                    second = int(row["i_ffio_a" + "jkl"[number - 1]])
                    pairs[tuple(sorted((first, second)))] = float(row["r_ffio_c" + str(number)]) * .1
            elif kind == "HOH":
                second, third = int(row["i_ffio_aj"]), int(row["i_ffio_ak"])
                theta = math.radians(float(row["r_ffio_c1"]))
                ab, ac = float(row["r_ffio_c2"]) * .1, float(row["r_ffio_c3"]) * .1
                pairs[tuple(sorted((first, second)))] = ab
                pairs[tuple(sorted((first, third)))] = ac
                pairs[tuple(sorted((second, third)))] = math.sqrt(
                    ab * ab + ac * ac - 2 * ab * ac * math.cos(theta))
            else:
                raise ValueError("Unsupported native constraint " + kind)
        for (first, second), length in sorted(pairs.items()):
            component.constraints.add(Constraint(first, second, length * units.nanometer, "AH1"))
    elements = {}
    for atom in system.atoms:
        elements.setdefault(atom.atomtype[0], set()).add(atom.atomic_number)
    if any(len(numbers) != 1 or next(iter(numbers)) <= 0 for numbers in elements.values()):
        raise ValueError("Atom type maps to an ambiguous or invalid element")
    for name, numbers in elements.items():
        system.atomtypes[name].atomic_number = next(iter(numbers))
    (output / "atomtype-elements.json").write_text(json.dumps(
        {name: next(iter(numbers)) for name, numbers in elements.items()},
        indent=2, allow_nan=False) + "\n")
    component_records, charge = [], 0.0
    for name, component in system.molecule_types.items():
        atoms = [atom for molecule in component.molecules for atom in molecule.atoms]
        component_charge = sum(atom.charge[0].value_in_unit(units.elementary_charge)
                               for atom in atoms)
        charge += component_charge
        component_records.append({"name": name, "atoms": len(atoms),
            "molecules": len(component.molecules), "charge_e": component_charge,
            "bonds": len(component.bond_forces), "angles": len(component.angle_forces),
            "dihedrals": len(component.dihedral_forces),
            "constraints": len(component.constraints)})
    if abs(charge - audit["total_charge_e"]) >= 1e-6:
        raise ValueError("System charge changed during CMS conversion")
    CompleteConstraintWriter(str(output / "system.top"), str(output / "system.gro"), system).write()
    atoms = list(system.atoms)
    np.savez(output / "source-atom-arrays.npz",
        positions_nm=np.array([[value.value_in_unit(units.nanometer) for value in atom.position]
                               for atom in atoms]),
        mass_da=np.array([atom.mass[0].value_in_unit(units.dalton) for atom in atoms]),
        charge_e=np.array([atom.charge[0].value_in_unit(units.elementary_charge) for atom in atoms]),
        atomic_number=np.array([atom.atomic_number for atom in atoms]),
        box_nm=np.array(system.box_vector.value_in_unit(units.nanometer)))
    artifact_names = ["unique.cms", "title-normalization.json", "atomtype-elements.json",
                      "system.top", "system.gro", "source-atom-arrays.npz"]
    result = {"schema": "openmm-conversion-artifact-v1",
        "status": "conversion_written_forcefield_audit_pending",
        "system_id": audit["system_id"], "source_cms_sha256": source_sha256,
        "parameter_audit_sha256": parameter_audit_sha256,
        "forcefield": audit["forcefield"], "water": audit["water"],
        "simulation_state": audit["simulation_state"], "atoms": system.n_atoms,
        "charge_e": charge, "combination_rule": system.combination_rule,
        "components": component_records,
        "artifacts": {name: sha(output / name) for name in artifact_names},
        "independent_forcefield_audit_completed": False,
        "minimization_completed": False, "md_system_ready": False,
        "campaign_ready": False, "simulation_started": False}
    (output / "conversion.json").write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    return result
