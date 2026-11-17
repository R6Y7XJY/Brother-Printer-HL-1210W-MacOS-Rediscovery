"""Verify CUPS transport behavior without transmitting a print job."""
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import brother_backend as backend


class BackendTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        (self.root / 'printer.json').write_text(json.dumps({'serial': 'TEST123'}))
        self.found = {'ip': '192.168.1.77', 'model': 'Brother HL-1210W series', 'serial': 'TEST123'}

    def tearDown(self):
        self.directory.cleanup()

    def call(self, options='', device='brotherauto://TEST123', found=True):
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(backend, 'ROOT', self.root))
            stack.enter_context(patch.object(backend, 'CACHE', self.root / 'cache'))
            stack.enter_context(patch.object(backend.sys, 'argv', ['backend', '42', 'user', 'Title', '2', options, '/spool/job']))
            stack.enter_context(patch.dict(os.environ, {'DEVICE_URI': device}))
            finder = stack.enter_context(patch.object(backend, 'find_printer', return_value=self.found))
            if not found:
                finder.side_effect = RuntimeError('offline')
            execute = stack.enter_context(patch.object(backend.os, 'execve', side_effect=SystemExit(99)))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            return backend.main(), execute

    def test_exact_identity_required_before_discovery(self):
        with patch.object(backend, 'find_printer') as finder:
            with self.assertRaisesRegex(RuntimeError, 'identity'):
                self.call(device='brotherauto://OTHER')
            finder.assert_not_called()

    def test_check_only_sends_no_job(self):
        result, execute = self.call(options='brother-connection-check-only=true')
        self.assertEqual(result, 0)
        execute.assert_not_called()
        saved = json.loads((self.root / 'cache/last-seen.json').read_text())
        self.assertEqual(saved['job_id'], '42')
        self.assertEqual(saved['mode'], 'connection-check-only')

    def test_handoff_preserves_job_arguments_and_uses_discovered_ip(self):
        captured = {}
        def capture(path, arguments, environment):
            captured.update(path=path, arguments=arguments, environment=environment)
            raise SystemExit(99)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(backend, 'ROOT', self.root))
            stack.enter_context(patch.object(backend, 'CACHE', self.root / 'cache'))
            args = ['backend', '42', 'user', 'Title', '2', 'media=A4', '/spool/job']
            stack.enter_context(patch.object(backend.sys, 'argv', args))
            stack.enter_context(patch.dict(os.environ, {'DEVICE_URI': 'brotherauto://TEST123'}))
            stack.enter_context(patch.object(backend, 'find_printer', return_value=self.found))
            stack.enter_context(patch.object(backend.os, 'execve', side_effect=capture))
            stack.enter_context(contextlib.redirect_stderr(io.StringIO()))
            with self.assertRaises(SystemExit):
                backend.main()
        self.assertEqual(captured['path'], backend.IPP)
        self.assertEqual(captured['arguments'], ['ipp://192.168.1.77/'] + args[1:])
        self.assertEqual(captured['environment']['DEVICE_URI'], 'ipp://192.168.1.77/')

    def test_offline_does_not_start_sender(self):
        with self.assertRaisesRegex(RuntimeError, 'offline'):
            self.call(found=False)


if __name__ == '__main__':
    unittest.main()
