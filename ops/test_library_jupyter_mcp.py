import json
import shlex
import subprocess
import sys
from types import SimpleNamespace
import unittest

from library_jupyter_mcp import command_for, response_for


class LibraryRelay(unittest.TestCase):
    def test_exact_request_and_shell_characters_round_trip(self):
        message = {'jsonrpc': '2.0', 'id': 7, 'method': 'tools/call',
                   'params': {'arguments': {'text': "quotes '\" $(touch forbidden)\n中文"}}}
        server = shlex.join([sys.executable, '-c',
            "import json,sys; x=json.loads(sys.stdin.readline()); print(json.dumps({'jsonrpc':'2.0','id':x['id'],'result':x['params']}))"])
        completed = subprocess.run(shlex.split(command_for(server, message)), capture_output=True, text=True, check=True)
        result = SimpleNamespace(completed=True, truncated=False, returncode=0, output=completed.stdout)
        self.assertEqual(response_for(result, 7)['result'], message['params'])
        with self.assertRaises(ValueError):
            response_for(result, 8)
        result.truncated = True
        with self.assertRaises(ValueError):
            response_for(result, 7)

    def test_zero_exit_requires_one_matching_response(self):
        value = {'jsonrpc': '2.0', 'id': 1, 'result': {}}
        for output in ('', json.dumps(value)+'\n'+json.dumps(value)):
            result = SimpleNamespace(completed=True, truncated=False, returncode=0,
                output=json.dumps({'returncode': 0, 'output': output}))
            with self.assertRaises(ValueError):
                response_for(result, 1)


if __name__ == '__main__':
    unittest.main()
