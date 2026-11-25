"""Replay sealed Ligand candidate identities before choosing a docking engine.

No candidate truncation, receptor preparation, screening or approval occurs here.
The caller records any deferred stereochemical identities and route selection.
"""
import json

from .covdock_vs import bound_file, identity, molecules, unresolved_stereo
from .jobs import sha256_file
from .ligand_design import design_folder, verify
from .ligand_first_inputs import load_campaign, build_parent_pose_mol
from .ligand_parent_evidence import require_latest
from .rdkit_covalentizer import CovalentizedCandidate, evaluate_chemistry_quality
from .target_hypothesis import folder as evidence_folder, read as read_evidence


def sealed_sources(root, directory, required):
    manifest = bound_file(root, str((directory / 'manifest.json').relative_to(root)))
    verify(directory)
    entries = json.loads(manifest.read_text())['files']
    inventory = {str(manifest.relative_to(root)): sha256_file(manifest)}
    for path in required:
        name = str(path.relative_to(root))
        actual = sha256_file(bound_file(root, name))
        if entries.get(str(path.relative_to(directory))) != actual:
            raise ValueError('VS study source is not sealed: ' + name)
        inventory[name] = actual
    return inventory


def read_design(root, state):
    """Return the full verified batch; preserve source order and all identities."""
    require_latest(root, state['evidence_id'])
    source = evidence_folder(root, state['evidence_id'])
    evidence = read_evidence(root, state['evidence_id'])
    design = design_folder(root, state['design_id'])
    generation = design / 'generation'
    lineage = sealed_sources(root, source, [source / 'evidence.json'])
    lineage.update(sealed_sources(root, design, [design / 'design.json', design / 'uniprot.json',
        design / 'case/campaign.json', design / 'case/parent_pose.sdf',
        design / 'case/source_structures/structure.pdb',
        *sorted((design / 'case/evidence').glob('*.json'))]))
    lineage.update(sealed_sources(root, generation, [generation / name for name in
        ('campaign.json', 'candidates.json', 'candidates.sdf', 'review.json', 'summary.json')]))
    record = json.loads((design / 'design.json').read_text())
    if (record['design_id'] != state['design_id'] or record['evidence_id'] != state['evidence_id']
            or record['evidence_manifest_sha256'] != sha256_file(source / 'manifest.json')
            or evidence['state'] != 'EVIDENCE_READY'
            or any(evidence['evidence'].get(key) != value for key, value in state['target'].items())):
        raise ValueError('VS design does not belong to the current study evidence')
    uniprot = json.loads((design / 'uniprot.json').read_text())
    if (uniprot.get('primaryAccession') != evidence['evidence']['identity'].get('uniprot_id')
            or uniprot.get('sequence', {}).get('value') != evidence['evidence']['identity'].get('sequence')
            or uniprot.get('organism', {}).get('taxonId') != state['target']['taxon']):
        raise ValueError('VS target identity differs from current evidence')
    summary = json.loads((generation / 'summary.json').read_text())
    if summary.get('state') != 'NEEDS_POSE_EVALUATION' or summary.get('graph_review_passed') is not True:
        raise ValueError('VS requires verified nonempty graph generation')
    campaign = load_campaign(generation / 'campaign.json')
    original = load_campaign(design / 'case/campaign.json')
    if any(getattr(campaign, key) != getattr(original, key) for key in ('target', 'parent_pose', 'protection')):
        raise ValueError('VS generation changed the study target, parent or protection')
    parent = build_parent_pose_mol(original, design / 'case')
    rows = json.loads((generation / 'candidates.json').read_text())['candidates']
    mols = molecules(generation / 'candidates.sdf', False)
    if (len(rows) != len(mols) or summary['candidate_count'] != len(rows)
            or len({row['candidate_id'] for row in rows}) != len(rows)):
        raise ValueError('VS generated candidate identity/count mismatch')
    admitted, deferred = [], []
    for index, (row, mol) in enumerate(zip(rows, mols)):
        # Keep the same typed provenance contract at VS admission and LO
        # handoff; a sealed but malformed candidate must not reach execution.
        CovalentizedCandidate.model_validate(row)
        if (not mol.HasProp('candidate_id') or mol.GetProp('candidate_id') != row['candidate_id']
                or identity(mol) != row['canonical_smiles']
                or evaluate_chemistry_quality(mol, parent, campaign).status != 'PASS'):
            raise ValueError('VS saved candidate identity or chemistry changed')
        ambiguity = unresolved_stereo(mol)
        if ambiguity:
            deferred.append({'candidate_id': row['candidate_id'], 'input_sdf_index': index,
                'reason': 'UNRESOLVED_STEREOCHEMISTRY', 'centers': ambiguity})
        else:
            admitted.append(index)
    if any(sha256_file(bound_file(root, name)) != sha for name, sha in lineage.items()):
        raise ValueError('Ligand candidate source changed during identity replay')
    return {'design': design, 'lineage': lineage, 'uniprot': uniprot,
        'campaign': campaign, 'parent': parent, 'rows': rows, 'molecules': mols,
        'admitted_indices': admitted, 'deferrals': deferred}
