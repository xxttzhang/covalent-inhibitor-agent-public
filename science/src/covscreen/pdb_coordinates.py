"""Read one explicit PDB model without merging coordinate alternatives."""
from __future__ import annotations

import math
from pathlib import Path


def read_pdb_coordinates(pdb_path: Path, entry_id: str, model: int | None = None):
    if model is not None and (type(model) is not int or model < 1):
        raise ValueError("Model must be a positive integer")
    raw = pdb_path.read_bytes()
    lines = raw.decode("ascii").splitlines()
    header_ids = {line[62:66].strip().upper() for line in lines if line.startswith("HEADER")}
    if header_ids != {str(entry_id).upper()}:
        raise ValueError("PDB HEADER does not match the mapped entry identity")
    models = [int(line[10:14]) for line in lines if line.startswith("MODEL ")]
    if len(models) != len(set(models)):
        raise ValueError("Duplicate PDB model identifiers")
    if len(models) > 1 and model is None:
        raise ValueError("Multiple coordinate models require explicit selection")
    selected_model = model if model is not None else (models[0] if models else 1)
    if selected_model not in (models or [1]):
        raise ValueError("Requested coordinate model is absent")
    current = None if models else 1
    atoms = []
    for line in lines:
        record = line[:6].strip()
        if record == "MODEL":
            current = int(line[10:14])
        elif record == "ENDMDL":
            current = None
        elif record in {"ATOM", "HETATM"} and current == selected_model:
            res_id = str(int(line[22:26])) + line[26:27].strip()
            atom_chain, resname = line[21:22].strip(), line[17:20].strip()
            xyz = tuple(float(line[start:start + 8]) for start in (30, 38, 46))
            occupancy = float(line[54:60])
            if not all(math.isfinite(v) for v in (*xyz, occupancy)) or not 0 <= occupancy <= 1:
                raise ValueError("Nonfinite coordinates or invalid occupancy")
            atoms.append({"serial": int(line[6:11]), "name": line[12:16].strip(),
                "altloc": line[16:17].strip(), "chain": atom_chain, "residue": res_id,
                "resname": resname, "xyz": xyz, "occupancy": occupancy,
                "element": line[76:78].strip().upper(), "record": record})
    if len({atom['serial'] for atom in atoms}) != len(atoms):
        raise ValueError('Duplicate atom serials in selected model')
    return raw, lines, atoms, selected_model
