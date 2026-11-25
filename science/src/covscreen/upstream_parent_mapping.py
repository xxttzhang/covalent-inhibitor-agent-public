"""Conservative substructure evidence for upstream Covalentizer products.

Mappings are graph correspondences, not reaction-history atom identities. All
symmetry alternatives are retained; truncated searches must not pass the gate.
"""
from rdkit import Chem


def parent_mapping_evidence(parent_smiles: str, fragment_smiles: str,
                            product_smiles: str, *, min_parent_fraction: float,
                            protected_core_smarts: str | None = None,
                            max_matches: int = 128) -> dict:
    if not 0 < min_parent_fraction <= 1:
        raise ValueError("min_parent_fraction must be in (0, 1]")
    if max_matches < 1:
        raise ValueError("max_matches must be positive")
    parent, fragment, product = [Chem.MolFromSmiles(s) for s in
                                 (parent_smiles, fragment_smiles, product_smiles)]
    if any(m is None for m in (parent, fragment, product)):
        raise ValueError("parent, fragment and product must be valid SMILES")
    parent, fragment, product = [Chem.RemoveHs(m) for m in (parent, fragment, product)]
    if any(len(Chem.GetMolFrags(m)) != 1 for m in (parent, fragment, product)):
        raise ValueError("parent, fragment and product must be single components")
    if any(a.GetAtomicNum() == 0 for m in (parent, fragment, product) for a in m.GetAtoms()):
        raise ValueError("dummy atoms must be resolved before mapping")
    # uniquify=False exposes symmetry alternatives rather than silently choosing
    # an atom correspondence that may be wrong for a subsequent RMSD comparison.
    pm = parent.GetSubstructMatches(fragment, useChirality=True, uniquify=False,
                                    maxMatches=max_matches + 1)
    cm = product.GetSubstructMatches(fragment, useChirality=True, uniquify=False,
                                     maxMatches=max_matches + 1)
    truncated = len(pm) > max_matches or len(cm) > max_matches
    fraction = fragment.GetNumHeavyAtoms() / parent.GetNumHeavyAtoms() if pm and cm else 0.0
    core_ok = None
    if protected_core_smarts is not None:
        core = Chem.MolFromSmarts(protected_core_smarts)
        if core is None or not core.GetNumAtoms():
            raise ValueError("protected_core_smarts is invalid or empty")
        if not parent.HasSubstructMatch(core, useChirality=True):
            raise ValueError("protected core does not occur in the supplied parent")
        # Require the shared fragment itself to retain the protected core.
        core_ok = fragment.HasSubstructMatch(core, useChirality=True)
    failures = []
    if not pm:
        failures.append("FRAGMENT_NOT_IN_PARENT")
    if not cm:
        failures.append("FRAGMENT_NOT_RETAINED_IN_PRODUCT")
    if fraction < min_parent_fraction:
        failures.append("PARENT_RETENTION_BELOW_THRESHOLD")
    if core_ok is False:
        failures.append("PROTECTED_CORE_LOST")
    if truncated:
        failures.append("ATOM_MAPPING_SEARCH_TRUNCATED")
    return {
        "status": "FAIL" if failures else "PASS", "failure_reasons": failures,
        "method": "chirality-aware exact fragment substructure correspondence",
        "atom_index_convention": "zero-based RDKit heavy-atom order from supplied SMILES",
        "parent_smiles": parent_smiles, "fragment_smiles": fragment_smiles,
        "product_smiles": product_smiles,
        "parent_retained_fraction": fraction,
        "min_parent_fraction": min_parent_fraction,
        "protected_core_smarts": protected_core_smarts,
        "protected_core_retained": core_ok,
        "mapping_ambiguous": len(pm) != 1 or len(cm) != 1,
        "mapping_truncated": truncated,
        # Each position is the same fragment atom index in both arrays.
        "fragment_to_parent_matches": [list(m) for m in pm[:max_matches]],
        "fragment_to_product_matches": [list(m) for m in cm[:max_matches]],
        "limitations": "Graph correspondences do not establish reaction atom ancestry or preserve experimental interactions.",
    }
