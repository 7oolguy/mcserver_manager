import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch, Mock

from core import Manager, LogCursor, redact, properties, safe_remote, DEFAULTS
import io
from types import SimpleNamespace
from app import cli


class CoreTests(unittest.TestCase):
    def test_live_log_incremental_partial_utf8_and_rotation(self):
        class Stream(io.BytesIO):
            def stat(self): return SimpleNamespace(st_size=len(self.getvalue()))
        cursor = LogCursor()
        self.assertEqual(cursor.read(Stream(b'first\npart'))['text'], 'first\n')
        self.assertEqual(cursor.read(Stream(b'first\npart'))['text'], '')
        self.assertEqual(cursor.read(Stream(b'first\npartial\n'))['text'], 'partial\n')
        update = cursor.read(Stream(b'restarted\n'))
        self.assertTrue(update['reset']); self.assertEqual(update['text'], 'restarted\n')
        cursor = LogCursor(); encoded = 'hello é\n'.encode()
        self.assertEqual(cursor.read(Stream(encoded[:-2]))['text'], '')
        self.assertEqual(cursor.read(Stream(encoded))['text'], 'hello é\n')

    def test_live_log_preserves_append_without_duplicates(self):
        class Stream(io.BytesIO):
            def stat(self): return SimpleNamespace(st_size=len(self.getvalue()))
        cursor = LogCursor(); original = b'A' * 128 + b'\n'
        cursor.read(Stream(original))
        self.assertEqual(cursor.read(Stream(original + b'new\n'))['text'], 'new\n')
        self.assertFalse(cursor.read(Stream(original + b'new\n'))['reset'])

    def test_log_redacts_api_keys(self):
        self.assertEqual(redact('token ptlc_example123 end'), 'token [API KEY REDACTED] end')

    def test_cancelled_export_preserves_previous_archive(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); profile = root / 'profile'; (profile / 'mods').mkdir(parents=True)
            (profile / 'mods/test.jar').write_bytes(b'jar')
            output = root / 'pack.zip'; output.write_bytes(b'previous')
            cancellation = Mock(); cancellation.is_set.return_value = True
            with self.assertRaises(RuntimeError):
                Manager(dict(DEFAULTS,client_path=str(profile)),cancel=cancellation).export_pack(output)
            self.assertEqual(output.read_bytes(), b'previous')

    def test_remote_paths_reject_traversal_and_absolute(self):
        for path in ('../world', '/etc/passwd', 'mods/../../x', 'C:\\temp', 'mods//x'):
            with self.subTest(path=path), self.assertRaises(ValueError): safe_remote(path)
        self.assertEqual(safe_remote('config\\DistantHorizons.toml'), 'config/DistantHorizons.toml')

    def test_property_edits_preserve_unrelated_values_and_comments(self):
        original = '# level-name=comment\nlevel-name=old\nonline-mode=true\nlevel-name=duplicate\n'
        changed = properties(original, {'level-name': 'new', 'level-seed': '42'})
        self.assertIn('# level-name=comment', changed)
        self.assertEqual(properties(changed)['online-mode'], 'true')
        self.assertEqual(changed.count('\nlevel-name='), 1)
        self.assertEqual(properties(changed)['level-seed'], '42')

    def test_property_injection_rejected(self):
        with self.assertRaises(ValueError): properties('', {'level-seed': '42\nonline-mode=false'})

    def test_online_writes_rejected_before_sftp(self):
        manager = Manager(); manager.status = Mock(return_value={'state': 'running'})
        manager.sftp = Mock()
        with self.assertRaises(RuntimeError): manager.write_bytes('server.properties', b'x')
        manager.sftp.assert_not_called()

    def test_missing_status_rejects_mutation(self):
        manager = Manager(); manager.status = Mock(side_effect=ValueError('Missing API key'))
        manager.sftp = Mock()
        with self.assertRaises(ValueError): manager.new_world('world2', '42')
        manager.sftp.assert_not_called()

    def test_save_stop_order(self):
        manager = Manager(); calls = []
        manager.status = Mock(return_value={'state': 'running'})
        manager.save = lambda: calls.append('save')
        manager.power = lambda signal: calls.append(signal)
        manager.save_stop()
        self.assertEqual(calls, ['save', 'stop'])

    def test_failed_save_does_not_stop(self):
        manager = Manager(); manager.status = Mock(return_value={'state': 'running'})
        manager.save = Mock(side_effect=TimeoutError('No save confirmation')); manager.power = Mock()
        with self.assertRaises(TimeoutError): manager.save_stop()
        manager.power.assert_not_called()

    def test_power_api_payload(self):
        manager = Manager(); manager.api = Mock(); manager.wait_state = Mock(return_value={'state': 'offline'})
        self.assertEqual(manager.power('stop'), {'state': 'offline'})
        manager.api.assert_called_once_with('/power', {'signal': 'stop'})
        with self.assertRaises(ValueError): manager.power('kill')

    def test_start_reports_starting_before_running(self):
        progress = []
        manager = Manager(progress=progress.append)
        manager.api = Mock()
        manager.status = Mock(side_effect=[{'state': 'starting'}, {'state': 'starting'}, {'state': 'running'}])
        with patch('core.time.sleep'), patch('sessions.blocked', return_value=False):
            self.assertEqual(manager.power('start')['state'], 'running')
        self.assertEqual(progress, ['Server: starting', 'Server: starting', 'Server: running'])

    def test_stop_reports_stopping_before_offline(self):
        progress = []
        manager = Manager(progress=progress.append)
        manager.api = Mock()
        manager.status = Mock(side_effect=[{'state': 'stopping'}, {'state': 'offline'}])
        with patch('core.time.sleep'):
            self.assertEqual(manager.power('stop')['state'], 'offline')
        self.assertEqual(progress, ['Server: stopping', 'Server: offline'])

    def test_api_credentials_not_forwarded_on_redirect(self):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self): return b'{"attributes":{"current_state":"offline"}}'
        opener = Mock(); opener.open.return_value = Response()
        with patch('core.secret', return_value='test-token'), patch('core.urllib.request.build_opener', return_value=opener) as builder:
            manager = Manager(dict(DEFAULTS))
            self.assertEqual(manager.status()['state'], 'offline')
            handler = builder.call_args.args[0]
            self.assertIsNone(handler.redirect_request(None, None, None, None, None, None))
            request = opener.open.call_args.args[0]
            self.assertTrue(request.full_url.endswith('/api/client/servers/' + manager.config['server_id'] + '/resources'))

    def test_export_excludes_worlds_and_accounts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); profile = root / 'profile'
            for relative in ('mods/test.jar', 'config/test.toml', 'saves/world/level.dat', 'accounts.json'):
                path = profile / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(b'test')
            config = dict(DEFAULTS, client_path=str(profile))
            output = root / 'pack.zip'; Manager(config).export_pack(output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(set(archive.namelist()), {'manifest.json', 'overrides/mods/test.jar', 'overrides/config/test.toml'})
                self.assertEqual(json.loads(archive.read('manifest.json'))['minecraft']['version'], '1.21.1')
            with self.assertRaises(ValueError): Manager(config).export_pack(profile / 'recursive.zip')

    def test_export_preserves_pack_loader_and_progression_scripts(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); profile = root / 'profile'
            for relative in ('mods/test.jar', 'kubejs/server_scripts/progression.js', 'datapacks/custom/pack.mcmeta'):
                path = profile / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('content')
            (profile / 'minecraftinstance.json').write_text(json.dumps({'name': 'My pack', 'gameVersion': '1.21.1', 'baseModLoader': {'name': 'neoforge-21.1.248'}}))
            output = root / 'pack.zip'; Manager(dict(DEFAULTS, client_path=str(profile))).export_pack(output)
            with zipfile.ZipFile(output) as archive:
                self.assertIn('overrides/kubejs/server_scripts/progression.js', archive.namelist())
                manifest = json.loads(archive.read('manifest.json'))
                self.assertEqual(manifest['minecraft']['modLoaders'][0]['id'], 'neoforge-21.1.248')
                self.assertEqual(manifest['name'], 'My pack')

    def test_cli_json_error_and_exit_code(self):
        with tempfile.TemporaryDirectory() as folder, patch('app.Manager') as manager:
            manager.return_value.status.side_effect = RuntimeError('No API key')
            output = Path(folder) / 'result.json'
            self.assertEqual(cli(['--output', str(output), 'status']), 1)
            self.assertEqual(json.loads(output.read_text())['ok'], False)


if __name__ == '__main__': unittest.main()
