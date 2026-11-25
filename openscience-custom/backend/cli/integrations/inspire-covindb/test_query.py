import csv
import os
import tempfile
import unittest
from pathlib import Path

import query


class QueryTests(unittest.TestCase):
    def test_deployment_root_is_explicit_and_not_a_query_argument(self):
        previous = os.environ.pop('COVINDB_ROOT', None)
        try:
            with self.assertRaisesRegex(ValueError, 'COVINDB_ROOT'):
                query.catalog()
            os.environ['COVINDB_ROOT'] = str(self.root)
            self.assertEqual(query.query({'target': 'EGFR', 'site': 'C797'})['total_matching_records'], 3)
            with self.assertRaises(ValueError):
                query.query({'target': 'EGFR', 'root': '/etc'})
            with self.assertRaises(ValueError):
                query.catalog('relative')
        finally:
            if previous is None:
                os.environ.pop('COVINDB_ROOT', None)
            else:
                os.environ['COVINDB_ROOT'] = previous

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        fields = ['', *query.FIELDS]
        records = [
            {'': '0', 'Inhibitor': 'CI000001', 'Inhibitor_Name': "Name 'one'", 'Target': 'P00533', 'Target_Gene': 'EGFR', 'Site': 'CYS-797', 'Value': '<10', 'Unit': 'nM'},
            {'': '1', 'Inhibitor': 'CI000001', 'Target': 'P00533', 'Target_Gene': 'EGFR', 'Site': 'CYS-797'},
            {'': '2', 'Inhibitor': 'CI000002', 'Target': 'P00533', 'Target_Gene': 'EGFR', 'Site': 'CYS-1797'},
            {'': '3', 'Inhibitor': 'CI000003', 'Target': 'P00533', 'Target_Gene': 'EGFR', 'Site': 'CYS-797; CYS-775'},
            {'': '4', 'Inhibitor': 'CI000004', 'Target': 'P00533', 'Target_Gene': 'EGFR', 'Site': 'SER-797'},
            {'': '5', 'Inhibitor': 'CI000005', 'Target': 'OTHER', 'Target_Gene': 'EGFR2', 'Site': 'CYS-797'},
        ]
        with (self.root / 'CovInDB_All.csv').open('w', newline='') as stream:
            writer = csv.DictWriter(stream, fields)
            writer.writeheader()
            writer.writerows(records)

    def tearDown(self):
        self.temp.cleanup()

    def test_actual_rows_and_distinct_ids(self):
        result = query.query({'target': 'EGFR', 'site': 'C797', 'limit': 1}, self.root)
        self.assertEqual(result['total_matching_records'], 3)
        self.assertEqual(result['unique_inhibitors'], 2)
        self.assertEqual(result['next_offset'], 1)
        self.assertEqual(result['records'][0]['Value'], '<10')
        self.assertEqual(result['records'][0]['Unit'], 'nM')
        self.assertEqual(len(result['source']['sha256']), 64)

    def test_accession_pagination(self):
        result = query.query({'target': 'p00533', 'site': 'cys797', 'limit': 2, 'offset': 1}, self.root)
        self.assertEqual([r['Inhibitor'] for r in result['records']], ['CI000001', 'CI000003'])
        self.assertFalse(result['has_more'])
        self.assertIsNone(result['next_offset'])

    def test_empty_is_not_error(self):
        result = query.query({'target': 'NOT_A_GENE'}, self.root)
        self.assertEqual(result['total_matching_records'], 0)
        self.assertEqual(result['records'], [])

    def test_name_and_id(self):
        self.assertEqual(query.query({'inhibitor': 'ci000001'}, self.root)['total_matching_records'], 2)
        self.assertEqual(query.query({'inhibitor': "'one'"}, self.root)['total_matching_records'], 1)

    def test_injection_is_literal_data(self):
        self.assertEqual(query.query({'target': "EGFR'; touch /tmp/wrong #"}, self.root)['total_matching_records'], 0)

    def test_invalid_arguments(self):
        for args in [{}, {'site': 'C797'}, {'target': 'EGFR', 'path': '/etc/passwd'},
                     {'target': 'EGFR', 'site': 'C797S'}, {'target': 'EGFR', 'site': '797'},
                     {'target': 'EGFR', 'site': 'XXX797'}, {'target': 'EGFR', 'limit': 51},
                     {'target': 'EGFR', 'offset': -1}, {'target': 'EGFR', 'limit': True}]:
            with self.subTest(args=args), self.assertRaises(ValueError):
                query.query(args, self.root)

    def test_missing_file_is_error(self):
        with self.assertRaises(FileNotFoundError):
            query.query({'target': 'EGFR'}, self.root / 'missing')

    def test_schema_mismatch_is_error(self):
        (self.root / 'CovInDB_All.csv').write_text('invalid,column\n1,2\n')
        with self.assertRaises(ValueError):
            query.query({'target': 'EGFR'}, self.root)

    def test_catalog_does_not_claim_all_files_exist(self):
        result = query.catalog(self.root)
        self.assertTrue(result['files'][0]['available'])
        self.assertFalse(result['files'][1]['available'])


if __name__ == '__main__':
    unittest.main()
