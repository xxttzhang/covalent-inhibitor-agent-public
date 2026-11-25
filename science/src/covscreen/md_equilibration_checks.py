"""Existing NPT final-window diagnostics shared by execution and receipt verification."""


def equilibration_checks(window):
    import numpy as np
    temperatures = np.asarray([row["temperature_k"] for row in window])
    densities = np.asarray([row["density_kg_m3"] for row in window])
    volumes = np.asarray([row["volume_nm3"] for row in window])
    return {"temperature_window": bool(270 <= temperatures.mean() <= 330
            and np.all((temperatures > 200) & (temperatures < 400))),
        "density_window": bool(np.all((densities > 500) & (densities < 1500))),
        "volume_stability": bool(volumes.std()/volumes.mean() < 0.10),
        "constraint_stability": all(row["max_relative_constraint_error"] < 1e-4 for row in window),
        "covalent_geometry": all(1.4 < row["covalent_distance_angstrom"] < 2.4 for row in window)}
