"""Real artifact and rejection tests; no synthetic passing receptor or mocked run."""
import copy
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

BASE = Path(__file__).resolve().parent
WORKSPACE = BASE.parents[4]
OUT = WORKSPACE / 'reports/research-loop-20261013/receptor-preparation-v2'


def module(filename, name):
    spec = importlib.util.spec_from_file_location(name, BASE / filename)
    loaded = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(loaded)
    return loaded


evaluator = module('research-scientific-evaluator-v2.py', 'scientific_v2')
auditor = module('research-scientific-audit-v2_1.py', 'audit_v2_1')


class ScientificV2Tests(unittest.TestCase):
    def setUp(self):
        self.manifest = evaluator.load_manifest()
        self.pdb = WORKSPACE / self.manifest['target']['receptor_path']

    def modified_proof(self, mutation):
        temporary = tempfile.TemporaryDirectory(dir=WORKSPACE / 'reports/research-loop-20261013')
        self.addCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        binding = json.loads((WORKSPACE / self.manifest['target']['mapping_path']).read_text())
        audit = json.loads((OUT / 'independent-audit-v2.1.json').read_text())
        mutation(audit)
        audit_path = directory / 'audit.json'
        audit_path.write_text(json.dumps(audit))
        binding['references']['independent_audit'] = {'path':str(audit_path.relative_to(WORKSPACE)), 'sha256':evaluator.sha(audit_path)}
        binding_path = directory / 'binding.json'
        binding_path.write_text(json.dumps(binding))
        manifest = copy.deepcopy(self.manifest)
        manifest['target'].update(mapping_path=str(binding_path.relative_to(WORKSPACE)), mapping_sha256=evaluator.sha(binding_path))
        return manifest

    def test_real_full_proof_stays_failed(self):
        audit, atoms = evaluator.receptor_audit(self.pdb, self.manifest)
        self.assertEqual(audit['status'], 'fail')
        self.assertEqual(audit['mapping_status'], 'pass')
        self.assertEqual(len(atoms), 5327)
        self.assertFalse(audit['modeled_coordinates_are_experimental'])
        self.assertEqual({c['id'] for c in audit['checks'] if c['status'] == 'fail'}, {'standard_amino_acid_stereochemistry', 'intramolecular_distance_angles', 'intramolecular_distance_internal'})

    def test_historical_result_cannot_claim_success(self):
        result = evaluator.evaluate(WORKSPACE / 'reports/layout-migration-20261012/gpu-final', self.pdb, self.manifest)
        self.assertEqual(result['outcome'], 'insufficient_evidence')
        self.assertEqual(result['counts'], {'pass':0, 'fail':0, 'unknown':5})
        self.assertEqual(result['accepted_candidate_ids'], [])
        self.assertFalse(result['new_remote_compute'])
        self.assertFalse(result['native_execution_provenance_verified'])

    def test_forged_overall_pass_rejected(self):
        manifest = self.modified_proof(lambda audit: audit.update(status='pass'))
        with self.assertRaisesRegex(ValueError, 'status contradicts'):
            evaluator.receptor_audit(self.pdb, manifest)

    def test_missing_required_check_rejected(self):
        manifest = self.modified_proof(lambda audit: audit['checks'].pop())
        with self.assertRaisesRegex(ValueError, 'missing/duplicated'):
            evaluator.receptor_audit(self.pdb, manifest)

    def test_cross_bound_source_hash_rejected(self):
        manifest = self.modified_proof(lambda audit: audit.update(source_sha256='0' * 64))
        with self.assertRaisesRegex(ValueError, 'identity mismatch: source_sha256'):
            evaluator.receptor_audit(self.pdb, manifest)

    def test_stale_binding_hash_rejected(self):
        self.manifest['target']['mapping_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'Proof bytes differ'):
            evaluator.receptor_audit(self.pdb, self.manifest)

    def test_ile_ccd_atom_naming_and_stereo(self):
        reference = auditor.sequence_reference(['ALA', 'ILE', 'ALA'])
        atoms = {atom.GetPDBResidueInfo().GetName().strip():atom for atom in reference.GetAtoms() if atom.GetPDBResidueInfo().GetResidueName().strip() == 'ILE'}
        self.assertIsNotNone(reference.GetBondBetweenAtoms(atoms['CG1'].GetIdx(), atoms['CD1'].GetIdx()))
        self.assertIsNone(reference.GetBondBetweenAtoms(atoms['CG2'].GetIdx(), atoms['CD1'].GetIdx()))
        auditor.Chem.AssignStereochemistry(reference, cleanIt=True, force=True)
        self.assertEqual(atoms['CA'].GetProp('_CIPCode'), 'S')
        self.assertEqual(atoms['CB'].GetProp('_CIPCode'), 'S')

    def test_intramolecular_bounds_are_not_interprotein_ratio(self):
        mol = auditor.Chem.MolFromSmiles('CCCCCC')
        bounds = auditor.rdDistGeom.GetMoleculeBoundsMatrix(mol, set15bounds=True, scaleVDW=True, doTriangleSmoothing=True, useMacrocycle14config=False)
        self.assertNotEqual(float(bounds[5,0]) * .7, 2 * auditor.Chem.GetPeriodicTable().GetRvdw(6) * .75)
        audit = json.loads((OUT / 'independent-audit-v2.1.json').read_text())
        self.assertEqual(audit['diagnostics']['legacy_self_vdw_ratio']['role'], 'diagnostic_only_not_acceptance')
        self.assertNotIn('protein_heavy_atom_clashes', audit['required_checks'])

    def test_model_failures_have_actual_bad_distances_and_provenance(self):
        audit = json.loads((OUT / 'independent-audit-v2.1.json').read_text())
        atom_map = {tuple(a['identity']):a for a in json.loads((OUT / 'atom-mapping.json').read_text())['atoms']}
        atoms = {a['key']:a for a in auditor.parse_atoms(self.pdb)}
        for check in audit['checks']:
            if check['id'] in {'intramolecular_distance_angles', 'intramolecular_distance_internal'}:
                failures = check['measurements']['first_100_violations']
                self.assertEqual(len(failures), check['measurements']['violation_count'])
                self.assertEqual(check['measurements']['retained_observed_pair_violations'], 0)
                tolerance = .25 if check['id'].endswith('angles') else .30
                for failure in failures:
                    first,second = [tuple(key) for key in failure['atoms']]
                    measured = float(auditor.np.linalg.norm(auditor.np.array(atoms[first]['xyz']) - auditor.np.array(atoms[second]['xyz'])))
                    self.assertAlmostEqual(measured, failure['distance_angstrom'], places=10)
                    self.assertLess(measured, failure['bounds'][0] * (1 - tolerance))
                    self.assertTrue(any(atom_map[key]['coordinate_origin'] != 'retained_observed_coordinate' for key in (first,second)))

    def test_original_and_prepared_bytes_unchanged(self):
        self.assertEqual(evaluator.sha(self.pdb), 'e98fbae80cb196ba885a4c202ae7f2107a668245f6638c4c846c02361cfd8a6a')
        policy = json.loads((BASE / 'research-scientific-preparation-policy-v2.json').read_text())
        self.assertEqual(evaluator.sha(WORKSPACE / policy['source']['path']), '5b020cd186307d657de80611793554b074f6936a3443d33f81d9b1a58004962e')
        audit_policy = json.loads((BASE / 'research-scientific-audit-policy-v2_1.json').read_text())
        frozen = json.loads((BASE / 'research-scientific-manifest.json').read_text())
        self.assertEqual(audit_policy['geometry'], frozen['geometry'])
        self.assertEqual(evaluator.sha(BASE / 'research-scientific-audit-v2_1.py'), audit_policy['auditor']['sha256'])
        self.assertEqual(evaluator.sha(OUT / 'independent-audit-v2.json'), audit_policy['initial_audit']['sha256'])


if __name__ == '__main__':
    unittest.main()
