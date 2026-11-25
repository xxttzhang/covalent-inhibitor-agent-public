"""Read-back and invalid-input tests; do not rerun preparation or a valid audit."""
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

CODE=Path(__file__).resolve().parent
ROOT=CODE.parents[4]
RESULT=ROOT/'reports/research-loop-20261013/receptor-preparation-v4'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AuditBindingTest(unittest.TestCase):
    def test_actual_audit_binds_receipt_all_artifacts_and_initial_lineage(self):
        audit=json.loads((RESULT/'independent-audit-v4.json').read_text())
        receipt=json.loads((RESULT/'preparation-receipt.json').read_text())
        self.assertEqual(audit['preparation_receipt_sha256'],sha(RESULT/'preparation-receipt.json'))
        self.assertEqual(audit['topology_sha256'],sha(RESULT/'topology.json'))
        self.assertEqual(audit['preparation_artifacts'],receipt['artifacts'])
        self.assertTrue(audit['audit_input_binding_complete'])
        self.assertEqual(len(audit['checks']),11)
        self.assertEqual(set(audit['required_checks']),{item['id'] for item in audit['checks']})
        for name,value in audit['preparation_artifacts'].items():
            self.assertEqual(value,sha(RESULT/name))
        for ref in audit['initial_inputs'].values():
            self.assertEqual(ref['sha256'],sha(ROOT/ref['path']))
        initial=ROOT/audit['initial_inputs']['receipt']['path']
        for name,value in audit['initial_artifacts'].items():
            self.assertEqual(value,sha(initial.parent/name))
        self.assertEqual(json.loads((RESULT/'observed-atom-drift.json').read_text())['maximum_angstrom'],0)
        self.assertLessEqual(audit['runtime']['preparation_plus_audit_cpu_core_hours'],0.15)
        parent=ROOT/audit['initial_inputs']['parent_v3_receipt']['path']
        self.assertEqual(audit['parent_v3_artifacts'],json.loads(parent.read_text())['artifacts'])
        for name,value in audit['parent_v3_artifacts'].items():
            self.assertEqual(value,sha(parent.parent/name))
        hydrogen=json.loads((RESULT/'hydrogen-rebuild.json').read_text())
        self.assertEqual(hydrogen['hydrogen_count'],2672)
        self.assertEqual(hydrogen['heavy_atom_displacement_nm'],0)
        self.assertEqual(hydrogen['seed_attempts'],1)
        self.assertFalse(hydrogen['source_intermediate_is_accepted'])
        stage=receipt['stages'][-2]
        self.assertFalse(stage['artificial_chirality_restraint'])
        self.assertFalse(stage['soft_nonbonded'])
        self.assertTrue(stage['tolerance_reached'])

    def test_scientific_core_and_thresholds_unchanged(self):
        previous=(CODE/'research-scientific-audit-v3.py').read_text()
        current=(CODE/'research-scientific-audit-v4.py').read_text()
        self.assertEqual(previous.split('    checks = []',1)[1].split('\ndef finish(',1)[0],
                         current.split('    checks = []',1)[1].split('\ndef finish(',1)[0])
        v3=json.loads((CODE/'research-scientific-preparation-policy-v3.json').read_text())
        v4=json.loads((CODE/'research-scientific-preparation-policy-v4.json').read_text())
        self.assertEqual(v3['geometry'],v4['geometry'])

    def test_changed_topology_is_rejected_before_any_scientific_reaudit(self):
        with tempfile.TemporaryDirectory(prefix='SYNTHETIC-v4-invalid-audit-') as folder:
            target=Path(folder)
            for source in RESULT.iterdir():
                if source.is_file() and source.name!='independent-audit-v4.json':
                    shutil.copyfile(source,target/source.name)
            topology=json.loads((target/'topology.json').read_text())
            topology['synthetic_test_only']=True
            topology['bonds']=topology['bonds'][1:]
            (target/'topology.json').write_text(json.dumps(topology))
            child=subprocess.run([sys.executable,'-B',str(CODE/'research-scientific-audit-v4.py'),
                '--policy',str(CODE/'research-scientific-preparation-policy-v4.json'),
                '--audit-policy',str(CODE/'research-scientific-audit-policy-v4.json'),'--directory',str(target)],
                text=True,capture_output=True,timeout=10)
            self.assertNotEqual(child.returncode,0)
            self.assertIn('sha(directory / filename) == digest',child.stderr)
            self.assertFalse((target/'independent-audit-v4.json').exists())


if __name__=='__main__':
    unittest.main()
