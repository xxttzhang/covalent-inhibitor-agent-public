"""Independent VS raw-Maestro readback with exact atom lineage.

Only whitelisted scalar properties are read. Embedded vendor Python/pickle
properties are never evaluated. Every pose keeps its own receptor coordinates.
"""
import gzip
import io
import math
from pathlib import Path

from rdkit import Chem

from .covdock_receptor import attachment_site
from .jobs import sha256_file


PROTEIN = set('ALA ARG ASN ASP CYS GLN GLU GLY HIS ILE LEU LYS MET PHE PRO SER THR TRP TYR VAL'.split())
ALIASES = {'ASH':'ASP', 'CYM':'CYS', 'CYX':'CYS', 'GLH':'GLU', 'HID':'HIS', 'HIE':'HIS', 'HIP':'HIS', 'LYN':'LYS'}
MAX_BYTES = 256 * 1024 * 1024
MAX_POSES_PER_CANDIDATE = 100


class ReactionSiteMismatch(ValueError):
    """A readable candidate pose reacted at a different declared atom."""


def residue(atom):
    info = atom.GetPDBResidueInfo()
    if info is None or info.GetAltLoc().strip():
        raise ValueError('VS output requires explicit, unambiguous atom/residue identity')
    name = info.GetResidueName().strip()
    return (info.GetChainId().strip(), info.GetResidueNumber(), info.GetInsertionCode().strip(),
            ALIASES.get(name, name), info.GetName().strip(), atom.GetAtomicNum())


def subset(molecule, indexes):
    selected = set(indexes)
    value = Chem.RWMol(molecule)
    for index in reversed(range(molecule.GetNumAtoms())):
        if index not in selected:
            value.RemoveAtom(index)
    return value.GetMol()


def ordered_identity(original, prepared):
    """Require the identity order before trusting vendor original-atom numbers."""
    left, right = Chem.RemoveHs(original), Chem.RemoveHs(prepared)
    for value in (left, right):
        for atom in value.GetAtoms():
            atom.SetAtomMapNum(0)
    if left.GetNumAtoms() != right.GetNumAtoms() or left.GetNumBonds() != right.GetNumBonds():
        raise ValueError('VS preparation changed the original heavy atom graph')
    matches = right.GetSubstructMatches(left, useChirality=True, uniquify=False, maxMatches=4096)
    if tuple(range(left.GetNumAtoms())) not in matches:
        raise ValueError('VS preparation changed heavy atom order or stereochemical identity')
    if Chem.MolToSmiles(left) != Chem.MolToSmiles(right):
        raise ValueError('VS preparation changed candidate identity')


def expected_product(original):
    mol = Chem.RemoveHs(original)
    maps = {atom.GetAtomMapNum(): atom.GetIdx() for atom in mol.GetAtoms() if atom.GetAtomMapNum()}
    if 905 not in maps or 906 not in maps:
        raise ValueError('VS Michael input lacks mapped alpha/beta carbons')
    alpha, beta = maps[905], maps[906]
    result = Chem.RWMol(mol)
    bond = result.GetBondBetweenAtoms(alpha, beta)
    if bond is None or bond.GetBondType() != Chem.BondType.DOUBLE:
        raise ValueError('VS Michael source does not contain its declared double bond')
    bond.SetBondType(Chem.BondType.SINGLE)
    bond.SetStereo(Chem.BondStereo.STEREONONE)
    for index in (alpha, beta):
        atom = result.GetAtomWithIdx(index)
        atom.SetNoImplicit(False)
        atom.SetNumExplicitHs(0)
    sulfur = result.AddAtom(Chem.Atom(16))
    result.AddBond(beta, sulfur, Chem.BondType.SINGLE)
    product = result.GetMol()
    Chem.SanitizeMol(product)
    Chem.AssignStereochemistry(product, cleanIt=True, force=True)
    return product, beta, sulfur


def protein_graph(molecule, indexes):
    heavy = {index:residue(molecule.GetAtomWithIdx(index)) for index in indexes
             if molecule.GetAtomWithIdx(index).GetAtomicNum() > 1}
    if len(set(heavy.values())) != len(heavy):
        raise ValueError('VS receptor contains duplicate heavy atom identifiers')
    bonds = {tuple(sorted((heavy[b.GetBeginAtomIdx()], heavy[b.GetEndAtomIdx()])))
             for b in molecule.GetBonds() if b.GetBeginAtomIdx() in heavy and b.GetEndAtomIdx() in heavy}
    return set(heavy.values()), bonds


def scalar_properties(molecule):
    properties = {}
    for name in molecule.GetPropNames():
        normalized = name.replace('\\_', '_')
        if normalized not in {'s_sd_COMPOUND_ID', 's_sd_compound_id', 's_sd_candidate_id',
                              'i_sd_INPUT_SDF_INDEX', 'r_i_docking_score'}:
            continue
        if normalized in properties:
            raise ValueError('VS output contains ambiguous duplicate identity properties')
        properties[normalized] = molecule.GetProp(name)
    return properties


def inspect_complex(molecule, original, prepared, receptor, attachment, input_index):
    ordered_identity(original, prepared)
    if molecule.GetNumConformers() != 1 or not all(math.isfinite(float(v))
            for point in molecule.GetConformer().GetPositions() for v in point):
        raise ValueError('VS complex requires one finite coordinate set')
    chain, number = attachment_site(attachment)
    metadata = {atom.GetIdx():residue(atom) for atom in molecule.GetAtoms()}
    targets = [index for index, key in metadata.items() if key[:5] == (chain, number, '', 'CYS', 'SG')]
    if len(targets) != 1 or molecule.GetAtomWithIdx(targets[0]).GetAtomicNum() != 16:
        raise ValueError('VS output target cysteine sulfur is not unique')
    target = targets[0]
    if any(atom.GetAtomicNum() == 1 for atom in molecule.GetAtomWithIdx(target).GetNeighbors()):
        raise ValueError('VS product target sulfur still retains a thiol hydrogen')
    protein = {index for index, key in metadata.items() if key[3] in PROTEIN}
    partners = [a.GetIdx() for a in molecule.GetAtomWithIdx(target).GetNeighbors() if a.GetIdx() not in protein]
    if len(partners) != 1 or molecule.GetAtomWithIdx(partners[0]).GetAtomicNum() != 6:
        raise ValueError('VS output requires one carbon partner covalently bound to target SG')
    ligand, pending = set(), [partners[0]]
    while pending:
        index = pending.pop()
        if index == target or index in ligand:
            continue
        if index in protein:
            raise ValueError('VS ligand has an unexpected second protein connection')
        ligand.add(index)
        pending.extend(atom.GetIdx() for atom in molecule.GetAtomWithIdx(index).GetNeighbors())
    if protein | ligand != set(metadata):
        raise ValueError('VS complex contains unaccounted components')
    if protein_graph(molecule, protein) != protein_graph(receptor, range(receptor.GetNumAtoms())):
        raise ValueError('VS output changed the prepared receptor heavy atom inventory or connectivity')
    properties = scalar_properties(molecule)
    name = original.GetProp('_Name')
    if molecule.GetProp('_Name') != name or any(value != name for key, value in properties.items()
            if key.startswith('s_sd_')):
        raise ValueError('VS candidate title and identity properties disagree')
    if properties.get('i_sd_INPUT_SDF_INDEX') != str(input_index):
        raise ValueError('VS output candidate index differs from approved input order')
    score = float(properties['r_i_docking_score']) if 'r_i_docking_score' in properties else None
    if score is not None and not math.isfinite(score):
        raise ValueError('VS docking score is not finite')
    expected, beta, sulfur = expected_product(original)
    heavy_prepared = [atom.GetIdx()+1 for atom in prepared.GetAtoms() if atom.GetAtomicNum() > 1]
    raw_by_prepared = {}
    for index in ligand:
        atom = molecule.GetAtomWithIdx(index)
        if atom.GetAtomicNum() == 1:
            continue
        if not atom.HasProp('i_cdock_internal_original_atom_number'):
            raise ValueError('VS ligand lacks vendor atom lineage')
        ordinal = atom.GetIntProp('i_cdock_internal_original_atom_number')
        if ordinal in raw_by_prepared:
            raise ValueError('VS ligand atom lineage is duplicated')
        raw_by_prepared[ordinal] = index
    if set(raw_by_prepared) != set(heavy_prepared):
        raise ValueError('VS ligand atom lineage differs from prepared input')
    raw_order = [raw_by_prepared[index] for index in heavy_prepared] + [target]
    if raw_order[beta] != partners[0]:
        raise ReactionSiteMismatch('VS target SG is not attached to the declared beta carbon')
    for index, raw_index in enumerate(raw_order[:-1]):
        actual, wanted = molecule.GetAtomWithIdx(raw_index), expected.GetAtomWithIdx(index)
        if ((actual.GetAtomicNum(), actual.GetFormalCharge(), actual.GetIsotope()) !=
                (wanted.GetAtomicNum(), wanted.GetFormalCharge(), wanted.GetIsotope())
                or actual.GetTotalNumHs(includeNeighbors=True) != wanted.GetTotalNumHs(includeNeighbors=True)):
            raise ValueError('VS product atom identity, charge, isotope or hydrogen count changed')
    product = subset(molecule, raw_order)
    compact = {raw:index for index, raw in enumerate(sorted(raw_order))}
    ordered = tuple(compact[index] for index in raw_order)
    product = Chem.RenumberAtoms(product, list(ordered))
    target_atom = product.GetAtomWithIdx(sulfur)
    if target_atom.GetFormalCharge() != 0:
        raise ValueError('VS product sulfur has unexpected formal charge')
    for index, atom in enumerate(product.GetAtoms()):
        atom.SetAtomMapNum(expected.GetAtomWithIdx(index).GetAtomMapNum())
        # The portable heavy graph restores implicit H after excluding explicit
        # vendor H atoms. SG is capped only for graph comparison, not for MD.
        atom.SetNumExplicitHs(0)
        atom.SetNoImplicit(False)
    Chem.SanitizeMol(product)
    Chem.AssignStereochemistryFrom3D(product, replaceExistingTags=True)
    if (product.GetNumAtoms() != expected.GetNumAtoms() or product.GetNumBonds() != expected.GetNumBonds()
            or tuple(range(expected.GetNumAtoms())) not in product.GetSubstructMatches(
                expected, useChirality=True, uniquify=False, maxMatches=4096)):
        raise ValueError('VS product graph, charge or original stereochemistry differs from Michael input')
    ligand_product = subset(product, range(sulfur))
    Chem.SanitizeMol(ligand_product)
    return {'candidate_id':name, 'input_index':input_index, 'score':score, 'product':product, 'complex':molecule,
        'ligand':ligand_product, 'receptor':subset(molecule, protein), 'target_index':target,
        'partner_index':partners[0], 'raw_ligand_indices':raw_order[:-1],
        'product_identity_verified':True, 'prepared_receptor_identity_verified':True}


def read(path: Path, originals, prepared, receptor, attachment, *, expected_sha256,
         reaction_site_rejections=None):
    if len(originals) != len(prepared) or not originals:
        raise ValueError('VS readback requires the complete original and prepared candidate set')
    if path.is_symlink() or sha256_file(path) != expected_sha256:
        raise ValueError('VS raw output checksum differs from registered evidence')
    # Bound decompression before supplying RDKit; no unbounded gzip expansion.
    opener = gzip.open if path.suffix.lower() == '.maegz' else open
    with opener(path, 'rb') as stream:
        raw = stream.read(MAX_BYTES+1)
    if len(raw) > MAX_BYTES:
        raise ValueError('VS raw output exceeds the bounded readback size')
    source = io.BytesIO(raw)
    supplier = Chem.MaeMolSupplier(source, sanitize=False, removeHs=False)
    identities = [mol.GetProp('_Name') for mol in originals]
    if len(identities) != len(set(identities)):
        raise ValueError('VS input candidate names are not unique')
    rows, counts = [], {name:0 for name in identities}
    pose_index = 0
    while not supplier.atEnd():
        mol = next(supplier)
        if mol is None or not mol.HasProp('_Name') or mol.GetProp('_Name') not in counts:
            raise ValueError('VS raw output has an unreadable or unapproved candidate')
        name = mol.GetProp('_Name')
        counts[name] += 1
        if counts[name] > MAX_POSES_PER_CANDIDATE:
            raise ValueError('VS raw output exceeds the per-candidate pose bound')
        index = identities.index(name)
        try:
            row = inspect_complex(mol, originals[index], prepared[index], receptor, attachment, index)
        except ReactionSiteMismatch as error:
            if reaction_site_rejections is None:
                raise
            reaction_site_rejections.append({'pose_index': pose_index, 'candidate_id': name,
                'input_index': index, 'reason': str(error), 'product_identity_verified': False})
        else:
            rows.append({**row, 'pose_index': pose_index})
        pose_index += 1
    if not rows:
        raise ValueError('VS raw output contains no poses')
    if sha256_file(path) != expected_sha256:
        raise ValueError('VS raw output changed during readback')
    return rows, counts
