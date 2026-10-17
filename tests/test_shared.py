import base64
import hashlib
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import tkinter as tk
import unittest
from unittest.mock import Mock, patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import shared
from service import Service
import updates


class SharedTests(unittest.TestCase):
    def setup_service(self, folder):
        client = Mock(); client.snapshot.return_value = {'active': 'one', 'sessions': {'one': {'label': 'One'}, 'two': {'label': 'Two'}},
            'server': {'state': 'running'}, 'players': {'online': 2}, 'address': 'localhost:25565', 'operation': None}
        return Service(Path(folder) / 'service.sqlite3', client), client
    def request(self, service, path, token='', method='GET', payload=None):
        body = json.dumps(payload).encode() if payload is not None else b''
        environ = {'PATH_INFO': path, 'REQUEST_METHOD': method, 'HTTP_AUTHORIZATION': 'Bearer ' + token,
                   'CONTENT_LENGTH': str(len(body)), 'wsgi.input': io.BytesIO(body)}
        status = []
        response = b''.join(service(environ, lambda code, headers: status.append(code)))
        return int(status[0].split()[0]), json.loads(response)
    def test_unauthenticated_requests_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            service, _ = self.setup_service(folder)
            self.assertEqual(self.request(service, '/v1/snapshot')[0], 401)
    def test_actual_http_client_can_join_but_cannot_switch(self):
        from wsgiref.simple_server import make_server, WSGIRequestHandler
        class Quiet(WSGIRequestHandler):
            def log_message(self, *args): pass
        with tempfile.TemporaryDirectory() as folder:
            service, client = self.setup_service(folder)
            server = make_server('127.0.0.1', 0, service, handler_class=Quiet)
            thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
            try:
                url = 'http://127.0.0.1:' + str(server.server_port)
                invite = service.invite(url, 'Friend')
                remote = shared.RemoteClient(url, invite['token'])
                self.assertEqual(remote.snapshot()['active'], 'one')
                self.assertEqual(remote.play('one')['state'], 'complete')
                with self.assertRaisesRegex(RuntimeError, 'Player access'): remote.play('two')
                client.play.assert_not_called()
            finally:
                server.shutdown(); server.server_close(); thread.join(3)
    def test_token_database_stores_only_hash(self):
        with tempfile.TemporaryDirectory() as folder:
            service, _ = self.setup_service(folder); invite = service.invite('https://example.com', 'Friend')
            with service.db() as db: row = db.execute('SELECT * FROM users').fetchone()
            self.assertNotIn(invite['token'], repr(dict(row)))
            self.assertEqual(self.request(service, '/v1/me', invite['token'])[1]['role'], 'player')
            with service.db() as db: db.execute('UPDATE users SET revoked=1')
            self.assertEqual(self.request(service, '/v1/snapshot', invite['token'])[0], 401)
    def test_player_cannot_switch_stop_or_run_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            service, client = self.setup_service(folder); invite = service.invite('https://example.com', 'Friend')
            status, _ = self.request(service, '/v1/actions', invite['token'], 'POST', {'action': 'play', 'session': 'two', 'allow_disconnect': True})
            self.assertEqual(status, 403); client.play.assert_not_called()
            status, _ = self.request(service, '/v1/actions', invite['token'], 'POST', {'action': 'command', 'command': 'op friend'})
            self.assertEqual(status, 400)
    def test_operator_operations_are_serialized(self):
        with tempfile.TemporaryDirectory() as folder:
            service, client = self.setup_service(folder); invite = service.invite('https://example.com', 'Operator', 'operator')
            gate = threading.Event(); client.play.side_effect = lambda *args: gate.wait(3)
            try:
                self.assertEqual(self.request(service, '/v1/actions', invite['token'], 'POST', {'action': 'play', 'session': 'two'})[0], 202)
                self.assertEqual(self.request(service, '/v1/actions', invite['token'], 'POST', {'action': 'play', 'session': 'one'})[0], 400)
                self.assertEqual(client.play.call_count, 1)
            finally:
                gate.set()
                for _ in range(100):
                    if not service.operation.locked(): break
                    time.sleep(.01)
    def test_snapshot_contains_no_hosting_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            service, _ = self.setup_service(folder); invite = service.invite('https://example.com', 'Friend')
            status, data = self.request(service, '/v1/snapshot', invite['token'])
            self.assertEqual(status, 200)
            for key in ('token', 'sftp_password', 'api_key', 'client_path'): self.assertNotIn(key, json.dumps(data))
    def test_non_https_invites_rejected(self):
        for url in ('http://example.com', 'file:///secret', 'https://name:password@example.com', 'https://example.com?token=secret'):
            with self.subTest(url=url), self.assertRaises(ValueError): shared.validate_url(url)
        self.assertEqual(shared.validate_url('http://127.0.0.1:8765'), 'http://127.0.0.1:8765')
    def test_installed_pack_uses_local_guid_and_checks_hashes(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); profile = root / 'A different local profile name'; (profile / 'mods').mkdir(parents=True)
            (profile / 'mods/mod.jar').write_bytes(b'good')
            (profile / 'minecraftinstance.json').write_text(json.dumps({'guid': '11111111-2222-4333-8444-555555555555'}))
            requirements = {'mods': shared.jar_manifest(profile)}
            self.assertEqual(shared.find_profile(requirements, root, verify=True), profile)
            (profile / 'mods/mod.jar').write_bytes(b'evil')
            with self.assertRaises(ValueError): shared.find_profile(requirements, root, verify=True)


class UpdateTests(unittest.TestCase):
    def signed(self):
        key = Ed25519PrivateKey.generate(); public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
        release = {'version': '2.1.0', 'url': 'https://example.com/app.msi', 'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest(), 'format': 'msi'}
        return key, public, {'release': release, 'signature': base64.b64encode(key.sign(updates.canonical(release))).decode()}
    def test_trusted_release_and_version_order(self):
        _, public, envelope = self.signed()
        self.assertEqual(updates.verify_manifest(envelope, public)['version'], '2.1.0')
        self.assertIsNone(updates.verify_manifest(envelope, public, current='2.2.0'))
    def test_tampered_manifest_is_rejected(self):
        _, public, envelope = self.signed(); envelope['release']['url'] = 'https://attacker.example/evil.msi'
        with self.assertRaises(Exception): updates.verify_manifest(envelope, public)
    def test_wrong_publisher_key_rejected(self):
        _, _, envelope = self.signed(); _, public, _ = self.signed()
        with self.assertRaises(Exception): updates.verify_manifest(envelope, public)
    def test_corrupt_download_preserves_installed_app(self):
        _, _, envelope = self.signed()
        with tempfile.TemporaryDirectory() as folder, patch('updates.APP_HOME', Path(folder)), patch('urllib.request.urlopen', return_value=io.BytesIO(b'evil')):
            with self.assertRaises(ValueError): updates.download(envelope['release'])
            self.assertEqual(list((Path(folder) / 'updates').iterdir()), [])
    def test_signatures_do_not_allow_http_installers(self):
        key, public, envelope = self.signed(); envelope['release']['url'] = 'http://example.com/app.msi'
        envelope['signature'] = base64.b64encode(key.sign(updates.canonical(envelope['release']))).decode()
        with self.assertRaises(ValueError): updates.verify_manifest(envelope, public)


class HomeTests(unittest.TestCase):
    def test_new_player_has_no_advanced_controls_or_play_before_connection(self):
        from desktop import Desktop
        root = tk.Tk(); root.withdraw()
        try:
            with patch('desktop.CONFIG_PATH', Path('does-not-exist')), patch('shared.load_local', return_value={'auto_update': True, 'service_url': ''}):
                home = Desktop(root, start_workers=False)
            self.assertEqual(str(home.play_button.cget('state')), 'disabled')
            self.assertEqual(root.title(), 'Build Explore Control 2.0.0')
        finally: root.destroy()
