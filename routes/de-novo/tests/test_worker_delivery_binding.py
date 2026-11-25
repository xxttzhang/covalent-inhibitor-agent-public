"""Results delivery uses the owning backend, including non-default ports."""
import importlib.util
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('delivery_worker', Path(__file__).resolve().parents[1] / 'runtime/worker.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class DeliveryBinding(unittest.TestCase):
    def test_bound_backend_and_project(self):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'path': self.path}).encode())

            def log_message(self, *args):
                pass

        with tempfile.TemporaryDirectory() as directory:
            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                binding = Path(directory) / 'binding.json'
                binding.write_text(json.dumps({'backend_port': server.server_port, 'project': '/test/project'}))
                worker = object.__new__(module.Worker)
                worker.request = {'directory': '/test/project'}
                with patch.dict(os.environ, {'COVALENT_RUNTIME_ROOT': directory}):
                    self.assertEqual(worker.api('/file/artifact-store'),
                                     {'path': '/file/artifact-store?directory=%2Ftest%2Fproject'})
                    worker.request['directory'] = '/wrong/project'
                    with self.assertRaisesRegex(RuntimeError, 'binding differs'):
                        worker.api('/file/artifact-store')
                    worker.request['directory'] = '/test/project'
                    binding.write_text(json.dumps({'backend_port': True, 'project': '/test/project'}))
                    with self.assertRaisesRegex(RuntimeError, 'binding differs'):
                        worker.api('/file/artifact-store')
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
