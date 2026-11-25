"""Shared audited CMS parameter contract; no discovery-route dependency."""
import math


def parameter_contract(audit, source_sha256):
    required = {"system_id", "status", "forcefield", "water", "simulation_state",
                "parameters_audited", "simulation_started", "total_atoms",
                "total_charge_e", "parameterized_cms_sha256"}
    missing = sorted(required - set(audit))
    if missing:
        raise ValueError("Parameter audit missing fields: " + ", ".join(missing))
    if (audit["status"] != "COVALENT_PARAMETERIZATION_AUDITED"
            or audit["forcefield"] != "OPLS_2005" or audit["water"] != "TIP3P"
            or audit["simulation_state"] != "covalent_adduct"
            or audit["parameters_audited"] is not True
            or audit["simulation_started"] is not False
            or audit["parameterized_cms_sha256"] != source_sha256
            or type(audit["total_atoms"]) is not int or audit["total_atoms"] < 1
            or type(audit["total_charge_e"]) not in (int, float)
            or not math.isfinite(audit["total_charge_e"])):
        raise ValueError("Parameter audit is inconsistent with the CMS conversion contract")
    return audit

