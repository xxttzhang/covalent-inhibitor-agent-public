"""Retain only upstream reaction products at the declared electrophile."""


def select_declared_products(input_mol, products, atom_map=906):
    atoms = [a for a in input_mol.GetAtoms() if a.GetAtomMapNum() == atom_map]
    if len(atoms) != 1:
        raise ValueError(f"expected exactly one electrophile atom map {atom_map}")
    selected = []
    for product in products:
        covatoms = [a for a in product.GetAtoms() if a.HasProp("covatom")]
        mapped = [a for a in product.GetAtoms() if a.GetAtomMapNum() == atom_map]
        if len(covatoms) != 1 or len(mapped) != 1:
            continue
        if product.GetBondBetweenAtoms(covatoms[0].GetIdx(), mapped[0].GetIdx()) is not None:
            selected.append(product)
    return selected
