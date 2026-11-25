import unittest
import json
import hashlib
import tempfile
from pathlib import Path
from hypothesis_remote_mcp import bridge_config, deliver, relative_artifact, relay

ROOT = '/inspire/test/hypothesis'


class BridgeBoundaries(unittest.TestCase):
    def test_external_configuration_preserves_exact_pairs_and_rejects_ambiguous_roots(self):
        with tempfile.TemporaryDirectory() as temp:
            file = Path(temp) / 'bridge.json'
            value = {'legacy_root': ROOT, 'release_roots': {ROOT: '/inspire/test/releases'},
                     'ssh': ['ssh', '-T', 'fixture'], 'delivery': temp}
            file.write_text(json.dumps(value))
            self.assertEqual(bridge_config(file), value)
            for changed in ({**value, 'legacy_root': '/inspire/other'},
                            {**value, 'release_roots': {ROOT: '/inspire/test/../releases'}},
                            {**value, 'ssh': []}, {**value, 'delivery': 'relative'}):
                file.write_text(json.dumps(changed))
                with self.assertRaises(ValueError):
                    bridge_config(file)
        self.assertEqual(bridge_config(None)['ssh'], [])
        self.assertEqual(bridge_config(None)['release_roots'], {})
        with self.assertRaisesRegex(ValueError, 'COVSCREEN_BRIDGE_CONFIG'):
            relay('must not execute', ROOT, ssh=[], delivery=None)

    def test_bound_delivery_copies_actual_artifacts_without_replaying_finalization(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            remote = root / 'remote'
            relative = 'hyp-' + 'a' * 24 + '/proposal-' + 'b' * 12 + '/report.md'
            source = remote / 'results/target-hypotheses' / relative
            source.parent.mkdir(parents=True)
            source.write_text('Engineering fixture: no activity conclusion')
            evidence = b'{"fixture":true}'
            collection = source.parent.parent
            (collection / 'evidence.json').write_bytes(evidence)
            manifest = json.dumps({'files': {'evidence.json': hashlib.sha256(evidence).hexdigest()}}).encode()
            (collection / 'manifest.json').write_bytes(manifest)
            (root / 'local').mkdir()
            value = {'schema_version': 'target-hypothesis-v1', 'hypothesis': {'route': 'de-novo'},
                     'evidence_id': collection.name, 'evidence_manifest_sha256': hashlib.sha256(manifest).hexdigest(),
                     'artifacts': {'report': str(source)}}
            def response():
                return {'result': {'content': [{'type': 'text', 'text': json.dumps(value)}], 'structuredContent': value}}
            with self.assertRaises(ValueError):
                deliver(response(), str(remote), delivery=root/'local', ssh=['/bin/sh', '-c'])
            result = deliver(response(), str(remote), delivery=root/'local', ssh=['/bin/sh', '-c'],
                             allowed_roots=(str(remote),))['result']
            artifact = root / 'local' / relative
            self.assertEqual(artifact.read_bytes(), source.read_bytes())
            self.assertEqual((root/'local'/collection.name/'evidence.json').read_bytes(), evidence)
            self.assertEqual(result['structuredContent'], json.loads(result['content'][0]['text']))
            self.assertEqual(result['structuredContent']['artifacts']['report'], str(artifact))
            source.write_text('Changed remote artifact')
            with self.assertRaisesRegex(ValueError, 'Immutable'):
                deliver(response(), str(remote), delivery=root/'local', ssh=['/bin/sh', '-c'],
                        allowed_roots=(str(remote),))

    def test_allowed_artifact(self):
        relative = "hyp-" + "a" * 24 + "/proposal-" + "b" * 12 + "/report.md"
        self.assertEqual(relative_artifact(ROOT + "/results/target-hypotheses/" + relative,
                                          ROOT, allowed_roots=(ROOT,)), relative)

    def test_reject_arbitrary_paths(self):
        for path in ("/etc/passwd", ROOT + "/.secrets/native-approval.key",
                     ROOT + "/results/target-hypotheses/../../secret",
                     ROOT + "/results/target-hypotheses/hyp-" + "a" * 24 + "/proposal-" + "b" * 12 + "/other.json"):
            with self.assertRaises(ValueError):
                relative_artifact(path, ROOT, allowed_roots=(ROOT,))

    def test_non_artifact_response_is_unchanged(self):
        message = {"id": 1, "result": {"content": [{"type": "text", "text": '{"state":"RUNNING"}'}]}}
        self.assertEqual(deliver(message), message)

    def test_non_json_message_is_unchanged(self):
        message = {"id": 1, "result": {"content": [{"type": "text", "text": "human-readable error"}]}}
        self.assertEqual(deliver(message), message)

    def test_evidence_sources_survive_line_limited_read(self):
        value = {"state": "EVIDENCE_READY", "evidence": {"sequence": "A" * 4000},
                 "source_index": {"PMID:123": {"title": "Actual source"}},
                 "hypothesis_schema": {"required": ["claims"]}}
        message = {"result": {"content": [{"type": "text", "text": json.dumps(value)}]}}
        text = deliver(message)["result"]["content"][0]["text"]
        self.assertEqual(json.loads(text), value)
        rendered = "\n".join(line[:2000] for line in text.splitlines())
        self.assertIn('"PMID:123"', rendered)
        self.assertIn('"hypothesis_schema"', rendered)

    def test_citation_error_distinguishes_collection_from_source(self):
        for error, expected in [("'evidence_id'", "Missing top-level evidence_id"),
                                ("Unknown evidence IDs: hyp-test", "NOT the top-level evidence_id")]:
            message = {"result": {"isError": True, "content": [{"type": "text", "text": error}]}}
            result = deliver(message)["result"]
            self.assertTrue(result["isError"])
            self.assertTrue(result["content"][0]["text"].startswith(error))
            self.assertIn(expected, result["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
