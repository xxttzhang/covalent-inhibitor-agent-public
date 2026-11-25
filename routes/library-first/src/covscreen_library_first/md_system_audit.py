"""Independently audit an OpenMM conversion against its parameterized CMS."""
from __future__ import annotations

import collections
import hashlib
import json
import math
from pathlib import Path
import re

from .md_system_conversion import read_cms_tables, sha


def _regular(path, digest, label):
    path = Path(path)
    if (path.is_symlink() or not path.is_file()
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or sha(path) != digest):
        raise ValueError(label + " checksum mismatch")
    return path


def _near(actual, expected, label, np, tolerance=2e-5):
    if not np.allclose(actual, expected, rtol=1e-7, atol=tolerance):
        raise ValueError(label + " differs from source CMS")


def audit_openmm_conversion(source, source_sha256, parameter_audit,
                            parameter_audit_sha256, conversion_directory,
                            conversion_manifest_sha256, output):
    """Rebuild OpenMM forces from TOP/GRO and compare every supported term."""
    source = _regular(source, source_sha256, "Parameterized CMS")
    parameter_path = _regular(parameter_audit, parameter_audit_sha256, "Parameter audit")
    parameter = json.loads(parameter_path.read_text())
    conversion_directory = Path(conversion_directory)
    if conversion_directory.is_symlink() or not conversion_directory.is_dir():
        raise ValueError("Conversion artifact directory is unavailable")
    conversion_path = _regular(conversion_directory / "conversion.json",
                               conversion_manifest_sha256, "Conversion manifest")
    conversion = json.loads(conversion_path.read_text())
    if (conversion.get("schema") != "openmm-conversion-artifact-v1"
            or conversion.get("status") != "conversion_written_forcefield_audit_pending"
            or conversion.get("source_cms_sha256") != source_sha256
            or conversion.get("parameter_audit_sha256") != parameter_audit_sha256
            or conversion.get("independent_forcefield_audit_completed") is not False
            or conversion.get("simulation_started") is not False
            or parameter.get("parameterized_cms_sha256") != source_sha256
            or parameter.get("total_atoms") != conversion.get("atoms")
            or parameter.get("system_id") != conversion.get("system_id")):
        raise ValueError("Conversion, parameter and CMS parents are inconsistent")
    expected_artifacts = conversion.get("artifacts")
    required = {"unique.cms", "title-normalization.json", "atomtype-elements.json",
                "system.top", "system.gro", "source-atom-arrays.npz"}
    if not isinstance(expected_artifacts, dict) or set(expected_artifacts) != required:
        raise ValueError("Conversion artifact inventory is incomplete")
    for name, digest in expected_artifacts.items():
        _regular(conversion_directory / name, digest, "Conversion artifact " + name)
    # Optional scientific dependencies are imported only after the complete
    # immutable parent chain has passed.
    import numpy as np
    import openmm as mm
    from openmm import app, unit

    output = Path(output)
    if output.exists() or output.is_symlink():
        raise ValueError("Independent force-field audit output must be a new directory")
    output.mkdir(parents=True, exist_ok=False)

    blocks = read_cms_tables(source)
    atoms_expected = parameter["total_atoms"]
    if (blocks[0]["atom_count"] != atoms_expected or len(blocks) < 2
            or any(not block.get("ffio_sites") for block in blocks[1:])
            or sum(block["atom_count"] for block in blocks[1:]) != atoms_expected):
        raise ValueError("CMS atom/component inventory differs from parameter audit")
    gro = app.GromacsGroFile(str(conversion_directory / "system.gro"))
    top = app.GromacsTopFile(str(conversion_directory / "system.top"),
                             periodicBoxVectors=gro.getPeriodicBoxVectors())
    system = top.createSystem(nonbondedMethod=app.PME,
        nonbondedCutoff=0.9 * unit.nanometer, constraints=None,
        rigidWater=False, ewaldErrorTolerance=1e-5)
    force_groups = collections.defaultdict(list)
    for force in system.getForces():
        force_groups[force.__class__.__name__].append(force)
    expected_force_counts = {"NonbondedForce": 1, "CustomNonbondedForce": 1,
        "CustomBondForce": 1, "HarmonicBondForce": 1, "HarmonicAngleForce": 1,
        "RBTorsionForce": 1, "CMMotionRemover": 1}
    if any(len(force_groups[name]) != count for name, count in expected_force_counts.items()):
        raise ValueError("Converted OpenMM force inventory is unsupported")
    forces = {name: values[0] for name, values in force_groups.items()}

    mass, charge, sigma, epsilon = [], [], [], []
    constraints, exclusions, pairs = {}, set(), {}
    bonds, angles, torsions = {}, {}, {}
    constrained_bonds, constrained_angles = set(), set()
    offset = 0
    functions = collections.Counter()
    for block in blocks[1:]:
        sites = block["ffio_sites"]
        if block["atom_count"] % len(sites):
            raise ValueError("CMS component atom count is not a site multiple")
        repeats = block["atom_count"] // len(sites)
        vdw = {row["s_ffio_name"]: row for row in block["ffio_vdwtypes"]}
        for repeat in range(repeats):
            base = offset + repeat * len(sites)
            for site in sites:
                potential = vdw[site["s_ffio_vdwtype"]]
                if potential["s_ffio_funct"].lower() != "lj12_6_sig_epsilon":
                    raise ValueError("Unsupported source van der Waals function")
                mass.append(float(site["r_ffio_mass"]))
                charge.append(float(site["r_ffio_charge"]))
                sigma.append(float(potential["r_ffio_c1"]) * 0.1)
                epsilon.append(float(potential["r_ffio_c2"]) * 4.184)

            def indices(row, names):
                return tuple(base + int(row["i_ffio_a" + name]) - 1 for name in names)

            for row in block.get("ffio_constraints", []):
                kind = row["s_ffio_funct"]
                if kind.startswith("AH") and kind[2:].isdigit():
                    for number in range(1, int(kind[2:]) + 1):
                        pair = tuple(sorted(indices(row, "i" + "jkl"[number - 1])))
                        constraints[pair] = float(row["r_ffio_c" + str(number)]) * .1
                elif kind == "HOH":
                    first, second, third = indices(row, "ijk")
                    theta = math.radians(float(row["r_ffio_c1"]))
                    ab, ac = float(row["r_ffio_c2"]) * .1, float(row["r_ffio_c3"]) * .1
                    constraints[tuple(sorted((first, second)))] = ab
                    constraints[tuple(sorted((first, third)))] = ac
                    constraints[tuple(sorted((second, third)))] = math.sqrt(
                        ab * ab + ac * ac - 2 * ab * ac * math.cos(theta))
                else:
                    raise ValueError("Unsupported source constraint " + kind)
            for row in block.get("ffio_exclusions", []):
                exclusions.add(tuple(sorted(indices(row, "ij"))))
            for row in block.get("ffio_pairs", []):
                pair = tuple(sorted(indices(row, "ij")))
                pair_data = pairs.setdefault(pair, {})
                kind = row["s_ffio_funct"].upper()
                if kind not in {"LJ", "COULOMB"} or kind in pair_data:
                    raise ValueError("Unsupported or duplicate source pair term")
                pair_data[kind] = float(row["r_ffio_c1"])
            for row in block.get("ffio_bonds", []):
                kind = row["s_ffio_funct"].upper()
                functions["bond:" + kind] += 1
                if kind not in {"HARM", "HARM_CONSTRAINED"}:
                    raise ValueError("Unsupported source bond function")
                pair = tuple(sorted(indices(row, "ij")))
                if pair in bonds:
                    raise ValueError("Duplicate source bond")
                bonds[pair] = (float(row["r_ffio_c1"]) * .1,
                               float(row["r_ffio_c2"]) * 4.184 * 200)
                if kind == "HARM_CONSTRAINED":
                    constrained_bonds.add(pair)
            for row in block.get("ffio_angles", []):
                kind = row["s_ffio_funct"].upper()
                functions["angle:" + kind] += 1
                if kind not in {"HARM", "HARM_CONSTRAINED"}:
                    raise ValueError("Unsupported source angle function")
                atoms = indices(row, "ijk")
                key = min(atoms, atoms[::-1])
                if key in angles:
                    raise ValueError("Duplicate source angle")
                angles[key] = (math.radians(float(row["r_ffio_c1"])),
                               float(row["r_ffio_c2"]) * 4.184 * 2)
                if kind == "HARM_CONSTRAINED":
                    constrained_angles.add(key)
            for row in block.get("ffio_dihedrals", []):
                kind = row["s_ffio_funct"].upper()
                functions["torsion:" + kind] += 1
                if kind not in {"OPLS_PROPER", "OPLS_IMPROPER"}:
                    raise ValueError("Unsupported source torsion function")
                if (float(row["r_ffio_c0"]) != 0
                        or any(float(row["r_ffio_c" + str(number)]) != 0
                               for number in (5, 6, 7))):
                    raise ValueError("Unsupported non-OPLS torsion coefficients")
                v1, v2, v3, v4 = [float(row["r_ffio_c" + str(number)]) * 4.184
                                   for number in range(1, 5)]
                coefficients = np.array([.5 * (v1 + v3) + v2,
                    .5 * (-v1 + 3 * v3), -v2 + 4 * v4, -2 * v3, -4 * v4, 0])
                atoms = indices(row, "ijkl")
                key = min(atoms, atoms[::-1])
                torsions[key] = torsions.get(key, np.zeros(6)) + coefficients
        offset += block["atom_count"]
    if len(mass) != offset or offset != system.getNumParticles() or offset != atoms_expected:
        raise ValueError("OpenMM particle count differs from CMS")
    _near([system.getParticleMass(i).value_in_unit(unit.dalton) for i in range(offset)],
          mass, "particle masses", np)
    nb = forces["NonbondedForce"]
    _near([nb.getParticleParameters(i)[0].value_in_unit(unit.elementary_charge)
           for i in range(offset)], charge, "particle charges", np)
    custom = forces["CustomNonbondedForce"]
    if custom.getEnergyFunction() != "A1*A2/r^12-C1*C2/r^6":
        raise ValueError("Converted LJ energy expression changed")
    lj_coefficients = np.array([[2 * math.sqrt(value) * size ** 3,
                                 2 * math.sqrt(value) * size ** 6]
                                for size, value in zip(sigma, epsilon)])
    _near(np.array([custom.getParticleParameters(i) for i in range(offset)]),
          lj_coefficients, "particle LJ coefficients", np, 1e-10)
    actual_constraints = {}
    for index in range(system.getNumConstraints()):
        first, second, length = system.getConstraintParameters(index)
        key = tuple(sorted((first, second)))
        if key in actual_constraints:
            raise ValueError("Duplicate converted constraint")
        actual_constraints[key] = length.value_in_unit(unit.nanometer)
    if actual_constraints.keys() != constraints.keys():
        raise ValueError("Converted constraint identities differ from CMS")
    _near([actual_constraints[pair] for pair in sorted(constraints)],
          [constraints[pair] for pair in sorted(constraints)],
          "constraint distances", np, 1e-8)
    actual_exclusions = {tuple(sorted(custom.getExclusionParticles(i)))
                         for i in range(custom.getNumExclusions())}
    if actual_exclusions != exclusions:
        raise ValueError("Converted exclusions differ from CMS")
    actual_pairs = {}
    for index in range(nb.getNumExceptions()):
        first, second, q, _, energy = nb.getExceptionParameters(index)
        if abs(energy.value_in_unit(unit.kilojoule_per_mole)) >= 1e-12:
            raise ValueError("Unexpected LJ term in NonbondedForce exception")
        actual_pairs[tuple(sorted((first, second)))] = q.value_in_unit(
            unit.elementary_charge ** 2)
    if actual_pairs.keys() != exclusions:
        raise ValueError("Converted Coulomb exception identities differ from CMS")
    for pair, value in actual_pairs.items():
        expected = pairs.get(pair, {}).get("COULOMB", 0) * charge[pair[0]] * charge[pair[1]]
        _near(value, expected, "Coulomb exception", np, 1e-8)
    lj_pairs = forces["CustomBondForce"]
    if lj_pairs.getEnergyFunction() != "-C/r^6+A/r^12":
        raise ValueError("Converted LJ pair expression changed")
    seen_pairs = set()
    for index in range(lj_pairs.getNumBonds()):
        first, second, values = lj_pairs.getBondParameters(index)
        pair = tuple(sorted((first, second)))
        if pair not in pairs or "LJ" not in pairs[pair] or pair in seen_pairs:
            raise ValueError("Unexpected or duplicate converted LJ pair")
        _near(values, lj_coefficients[first] * lj_coefficients[second] * pairs[pair]["LJ"],
              "LJ pair", np, 1e-10)
        seen_pairs.add(pair)
    if seen_pairs != {pair for pair, values in pairs.items() if "LJ" in values}:
        raise ValueError("Converted LJ pair identities differ from CMS")
    bond_force = forces["HarmonicBondForce"]
    actual_bonds, zero_bonds = {}, []
    for index in range(bond_force.getNumBonds()):
        first, second, length, coefficient = bond_force.getBondParameters(index)
        values = (length.value_in_unit(unit.nanometer),
            coefficient.value_in_unit(unit.kilojoule_per_mole / unit.nanometer ** 2))
        pair = tuple(sorted((first, second)))
        if abs(values[1]) < 1e-12:
            zero_bonds.append({"atoms": pair, "parameters": values})
        elif pair in actual_bonds:
            raise ValueError("Duplicate converted nonzero bond")
        else:
            actual_bonds[pair] = values
    for pair, values in bonds.items():
        if pair not in actual_bonds:
            if pair not in constrained_bonds:
                raise ValueError("Unconstrained CMS bond is absent from OpenMM")
            _near(constraints[pair], values[0], "constrained bond equilibrium", np, 1e-8)
        else:
            _near(actual_bonds[pair], values, "bond parameters", np)
    if actual_bonds.keys() - bonds.keys():
        raise ValueError("OpenMM has an extra nonzero bond term")
    angle_force = forces["HarmonicAngleForce"]
    actual_angles = {}
    for index in range(angle_force.getNumAngles()):
        first, second, third, theta, coefficient = angle_force.getAngleParameters(index)
        atoms = (first, second, third)
        key = min(atoms, atoms[::-1])
        if key in actual_angles:
            raise ValueError("Duplicate converted angle")
        actual_angles[key] = (theta.value_in_unit(unit.radian),
            coefficient.value_in_unit(unit.kilojoule_per_mole / unit.radian ** 2))
    if actual_angles.keys() - angles.keys():
        raise ValueError("OpenMM has an extra angle term")
    for atoms, values in angles.items():
        if atoms not in actual_angles:
            if atoms not in constrained_angles:
                raise ValueError("Unconstrained CMS angle is absent from OpenMM")
            first, second, third = atoms
            ab, bc, ac = [constraints[tuple(sorted(pair))]
                          for pair in ((first, second), (second, third), (first, third))]
            theta = math.acos((ab * ab + bc * bc - ac * ac) / (2 * ab * bc))
            _near(theta, values[0], "constrained angle equilibrium", np, 1e-8)
        else:
            _near(actual_angles[atoms], values, "angle parameters", np)
    torsion_force = forces["RBTorsionForce"]
    actual_torsions = {}
    for index in range(torsion_force.getNumTorsions()):
        first, second, third, fourth, *coefficients = torsion_force.getTorsionParameters(index)
        atoms = (first, second, third, fourth)
        key = min(atoms, atoms[::-1])
        values = np.array([value.value_in_unit(unit.kilojoule_per_mole)
                           for value in coefficients])
        actual_torsions[key] = actual_torsions.get(key, np.zeros(6)) + values
    if actual_torsions.keys() - torsions.keys():
        raise ValueError("OpenMM has an extra torsion term")
    for atoms, values in torsions.items():
        _near(actual_torsions.get(atoms, np.zeros(6)), values,
              "torsion parameters", np)
    system_path = output / "system.xml"
    system_path.write_text(mm.XmlSerializer.serialize(system))
    report = {"schema": "openmm-forcefield-audit-artifact-v1",
        "status": "OPENMM_FORCEFIELD_AUDITED_MINIMIZATION_PENDING",
        "system_id": conversion["system_id"], "atoms": offset,
        "source_cms_sha256": source_sha256,
        "parameter_audit_sha256": parameter_audit_sha256,
        "conversion_manifest_sha256": conversion_manifest_sha256,
        "constraints": len(constraints), "exclusions": len(exclusions),
        "pair_terms": len(pairs), "bonds": len(bonds), "angles": len(angles),
        "source_torsion_keys": len(torsions),
        "converted_torsion_keys": len(actual_torsions),
        "zero_force_extra_bonds": zero_bonds,
        "source_functions": dict(functions), "system_xml_sha256": sha(system_path),
        "independent_forcefield_audit_completed": True,
        "independent_energy_comparison_completed": False,
        "minimization_completed": False, "dynamics_validated": False,
        "md_system_ready": False, "campaign_ready": False,
        "simulation_started": False}
    report_path = output / "forcefield-audit.json"
    report_path.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n")
    return report
