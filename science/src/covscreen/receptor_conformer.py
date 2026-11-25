"""Plan explicit protein alternate-location choices from deposited evidence."""
from __future__ import annotations

from pathlib import Path

from .pdb_coordinates import read_pdb_coordinates


def plan_alternate_locations(source, mapping, model=None):
    _, _, atoms, selected_model = read_pdb_coordinates(
        Path(source), mapping["entry_id"], model
    )
    grouped = {}
    for atom in atoms:
        if (atom["record"] != "ATOM" or atom["chain"] != mapping["auth_asym_id"]
                or not atom["altloc"]):
            continue
        residue = grouped.setdefault(atom["residue"], {})
        residue.setdefault(atom["altloc"], []).append(atom)
    residues = []
    for residue_id, alternatives in sorted(grouped.items(), key=lambda item: item[0]):
        options = []
        for label, records in sorted(alternatives.items()):
            occupancies = [float(atom["occupancy"]) for atom in records]
            options.append({
                "label": label,
                "atom_count": len(records),
                "atom_names": sorted(atom["name"] for atom in records),
                "occupancy_sum": sum(occupancies),
                "occupancy_mean": sum(occupancies) / len(occupancies),
                "minimum_occupancy": min(occupancies),
            })
        ranked = sorted(
            options,
            key=lambda option: (option["occupancy_mean"], option["occupancy_sum"], option["atom_count"]),
            reverse=True,
        )
        recommended = ranked[0]["label"] if len(ranked) == 1 or (
            ranked[0]["occupancy_mean"], ranked[0]["occupancy_sum"], ranked[0]["atom_count"]
        ) != (
            ranked[1]["occupancy_mean"], ranked[1]["occupancy_sum"], ranked[1]["atom_count"]
        ) else None
        residues.append({"author_residue_id": residue_id, "options": options,
                         "unique_occupancy_recommendation": recommended})
    return {
        "schema": "receptor-alternate-location-plan-v1",
        "entry_id": mapping["entry_id"],
        "author_chain": mapping["auth_asym_id"],
        "model": selected_model,
        "residues": residues,
        "required_author_residue_ids": [row["author_residue_id"] for row in residues],
        "automatic_selection_allowed": False,
        "selection_rule": "Caller must explicitly select one deposited label for every listed residue.",
    }


def validate_alternate_location_selection(plan, choices):
    options = {
        row["author_residue_id"]: {option["label"] for option in row["options"]}
        for row in plan["residues"]
    }
    if set(choices) != set(options):
        raise ValueError("Select one alternate location for every ambiguous residue, and no others")
    invalid = {residue: label for residue, label in choices.items()
               if label not in options[residue]}
    if invalid:
        raise ValueError(f"Alternate-location labels are not present in deposited evidence: {invalid}")
