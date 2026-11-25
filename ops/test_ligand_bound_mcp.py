import copy
import json
import os
from pathlib import Path
import shlex
import sys
import tempfile
import unittest
from unittest.mock import patch

from ligand_bound_mcp import connection, remote_command


class BoundMcp(unittest.TestCase):
    def setUp(self):
        self.binding = {
            "release_id": "a" * 64, "manifest_sha256": "b" * 64,
            "release_root": "/inspire/project/releases/" + "a" * 64,
            "data_root": "/inspire/project/consumer",
        }

    def test_command_keeps_remote_verifier_program_one_argument(self):
        binding = {**self.binding, "data_root": "/inspire/project/consumer's $(touch bad)"}
        words = shlex.split(remote_command(binding))
        self.assertEqual(words[-4:-1], ["-u", "-B", "-c"])
        compile(words[-1], "<remote-binding>", "exec")
        self.assertIn(repr(binding), words[-1])
        self.assertIn("namespace['verify']", words[-1])
        self.assertIn("os.environ['COVALENT_APPROVAL_KEY_FILE']", words[-1])

    def test_instance_switch_is_forwarded_only_when_enabled(self):
        assignment = "os.environ['COVALENT_LIGAND_VS_INSTANCE']='asgbie3'"
        for value in ("", "asgbie3", "asgbie4"):
            with self.subTest(value=value), patch.dict(os.environ, {"COVALENT_LIGAND_VS_INSTANCE": value}):
                program = shlex.split(remote_command(self.binding))[-1]
                compile(program, "<remote-binding>", "exec")
                self.assertEqual(assignment in program, value == "asgbie3")
                self.assertEqual("os.environ['COVALENT_LIGAND_VS_INSTANCE']='asgbie4'" in program, value == "asgbie4")

    def test_rejects_unbound_or_overlapping_roots(self):
        for changes in (
            {"release_id": "moving-current"},
            {"manifest_sha256": "not-a-digest"},
            {"data_root": "/tmp/consumer"},
            {"data_root": "/inspire/project/../consumer"},
            {"data_root": "/inspire/project"},
            {"data_root": self.binding["release_root"] + "/data"},
            {"release_root": "/inspire/project/releases/wrong"},
            {"command": "anything"},
        ):
            binding = {**copy.copy(self.binding), **changes}
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                remote_command(binding)

    def test_frozen_config_guard_is_in_both_remote_processes(self):
        words = shlex.split(remote_command(self.binding, verify_runtime_config=True))
        for program in (words[3], words[-1]):
            compile(program, '<frozen-runtime>', 'exec')
            self.assertIn("config.read_bytes()!=(root/'science/configs/local.inspire.yaml').read_bytes()", program)

    def test_explicit_host_runtime_avoids_engine_activation_and_requires_exact_assets(self):
        runtime = {'python': '/inspire/acceptance/host/bin/python', 'sha256': 'c' * 64,
                   'lock': '/inspire/acceptance/requirements.lock', 'lock_sha256': 'd' * 64}
        words = shlex.split(remote_command(self.binding, runtime=runtime, verify_runtime_config=True))
        self.assertNotIn('source', words)
        self.assertIn(runtime['python'], words)
        compile(words[-1], '<independent-host>', 'exec')
        for change in ({'sha256': 'bad'}, {'python': '/tmp/python'}, {'command': 'anything'}):
            with self.assertRaises(ValueError):
                remote_command(self.binding, runtime={**runtime, **change})

    def test_covalentizer_verifies_bytes_before_environment_binding(self):
        import hashlib
        import os
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            names = ('Code/CovUtils.py', 'Code/numbered_reaction.re', 'Scripts/reform_elec.re')
            files = {}
            for name in names:
                path = root / name
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(name.encode())
                files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
            asset = {'root': '/inspire/assets/fixed', 'files': files}
            runtime = {'python': '/inspire/host/python', 'sha256': 'c' * 64,
                       'lock': '/inspire/host/lock', 'lock_sha256': 'd' * 64,
                       'covalentizer': asset}
            words = shlex.split(remote_command(self.binding, runtime=runtime))
            for program in (words[3], words[-1]):
                compile(program, '<asset-bound>', 'exec')
                check = 'asset=' + program.split('\nasset=', 1)[1].split("os.environ[", 1)[0]
                check = check.replace(repr(asset['root']), repr(str(root)))
                namespace = {'pathlib': __import__('pathlib'), 'hashlib': hashlib}
                exec(check, namespace)
                target = root / names[0]
                original = target.read_bytes()
                target.write_bytes(b'changed')
                with self.assertRaisesRegex(ValueError, 'asset changed'):
                    exec(check, namespace)
                target.write_bytes(original)
            self.assertIn("os.environ['COVALENTIZER']=asset['root']", words[-1])
            for bad in ({'root': '/tmp/assets', 'files': files},
                        {'root': asset['root'], 'files': {}},
                        {'root': asset['root'], 'files': {**files, '../extra': 'a' * 64}},
                        {'root': asset['root'], 'files': files, 'command': 'run'}):
                with self.assertRaises(ValueError):
                    remote_command(self.binding, runtime={**runtime, 'covalentizer': bad})

    def test_connection_profile_is_private_and_workspace_stays_one_proxy_argument(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            key = root / 'identity'
            key.touch()
            profile = root / 'connection.json'
            value = {'notebook': 'cpu-example', 'workspace': "CPU's $(touch unsafe) 空间", 'account': 'default',
                     'cli': sys.executable, 'identity_file': str(key), 'port': 22222}
            profile.write_text(json.dumps(value))
            profile.chmod(0o600)
            argv = connection(profile)
            proxy = next(item.removeprefix('ProxyCommand=') for item in argv if item.startswith('ProxyCommand='))
            parts = shlex.split(proxy)
            self.assertEqual(parts[parts.index('--workspace') + 1], value['workspace'])
            self.assertEqual(argv[-1], 'root@cpu-example')
            profile.chmod(0o644)
            with self.assertRaisesRegex(ValueError, 'owner-only'):
                connection(profile)
            profile.chmod(0o600)
            profile.write_text(json.dumps({**value, 'notebook': '-oProxyCommand=bad'}))
            with self.assertRaisesRegex(ValueError, 'notebook'):
                connection(profile)


if __name__ == "__main__":
    unittest.main()
