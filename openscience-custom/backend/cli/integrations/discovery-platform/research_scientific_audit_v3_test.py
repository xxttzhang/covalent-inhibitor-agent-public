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
RESULT=ROOT/'reports/research-loop-20261013/receptor-preparation-v3'


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AuditBindingTest(unittest.TestCase):
    def test_actual_audit_binds_receipt_all_artifacts_and_initial_lineage(self):
        audit=json.loads((RESULT/'independent-audit-v3.json').read_text())
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
        self.assertLessEqual(audit['runtime']['preparation_plus_audit_cpu_core_hours'],0.25)

    def test_changed_topology_is_rejected_before_any_scientific_reaudit(self):
        with tempfile.TemporaryDirectory(prefix='SYNTHETIC-v3-invalid-audit-') as folder:
            target=Path(folder)
            for source in RESULT.iterdir():
                if source.is_file() and source.name!='independent-audit-v3.json':
                    shutil.copyfile(source,target/source.name)
            topology=json.loads((target/'topology.json').read_text())
            topology['synthetic_test_only']=True
            topology['bonds']=topology['bonds'][1:]
            (target/'topology.json').write_text(json.dumps(topology))
            child=subprocess.run([sys.executable,'-B',str(CODE/'research-scientific-audit-v3.py'),
                '--policy',str(CODE/'research-scientific-preparation-policy-v3.json'),
                '--audit-policy',str(CODE/'research-scientific-audit-policy-v3.json'),'--directory',str(target)],
                text=True,capture_output=True,timeout=10)
            self.assertNotEqual(child.returncode,0)
            self.assertIn('sha(directory / filename) == digest',child.stderr)
            self.assertFalse((target/'independent-audit-v3.json').exists())


if __name__=='__main__':
    unittest.main()
