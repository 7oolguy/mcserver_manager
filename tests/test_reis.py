import json
import unittest
from unittest.mock import MagicMock, Mock, patch

import shared


class ReisTests(unittest.TestCase):
    def client(self, names=None, active='one'):
        config = shared.reis_config(dict(type='reis-direct', version=1, panel_url='https://panel.example.com',
                                        server_id='example', game_host='game.example.com', game_port=25565))
        client = shared.ReisClient(config, 'friend-only-key')
        names = names or ['state.json', 'catalog.json']
        state = {'active': active, 'sessions': {'one': {'label': 'One', 'client_path': 'PRIVATE OWNER PATH'}, 'two': {'label': 'Two'}}}
        def api(route):
            if route.startswith('/files/list'): return {'data': [{'attributes': {'name': name}} for name in names]}
            if 'state.json' in route: return state
            if 'catalog.json' in route: return {'one': {'mods': []}}
            if route == '/resources': return {'attributes': {'current_state': 'running'}}
            raise AssertionError('Unexpected API call ' + route)
        client.manager.api = Mock(side_effect=api)
        return client

    @patch('shared.player_info', return_value={'online': 1, 'names': []})
    def test_read_only_join_and_no_owner_data(self, _):
        client = self.client()
        data = client.snapshot()
        self.assertNotIn('PRIVATE OWNER PATH', json.dumps(data))
        self.assertNotIn('friend-only-key', json.dumps(data))
        self.assertEqual(client.play('one'), {'state': 'complete'})
        with self.assertRaisesRegex(ValueError, 'active session'): client.play('two', True)
        self.assertTrue(all(call.args[0].startswith(('/files/list', '/files/contents', '/resources')) and len(call.args) == 1
                            for call in client.manager.api.call_args_list))

    @patch('shared.player_info', return_value={'online': 0})
    def test_remote_locks_and_recovery_prevent_join(self, _):
        for lock in ('operation', 'lock', 'pending.json.previous'):
            with self.subTest(lock=lock):
                with self.assertRaisesRegex(ValueError, 'operation'):
                    self.client(['state.json', 'catalog.json', lock]).play('one')

    @patch('shared.player_info', return_value={'online': 0})
    def test_switch_overlapping_snapshot_blocks_join(self, _):
        client = self.client()
        original = client.read
        reads = []
        def read(filename):
            data = original(filename)
            if filename.startswith('state'):
                reads.append(filename)
                return dict(data, active='two' if len(reads) % 2 == 0 else 'one')
            return data
        client.read = read
        with self.assertRaisesRegex(ValueError, 'operation'): client.play('one')

    def test_connection_file_rejects_http_and_credentials(self):
        for url in ('http://example.com', 'https://owner:password@example.com', 'https://example.com/path'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                shared.reis_config(dict(type='reis-direct', version=1, panel_url=url, server_id='example', game_host='localhost', game_port=25565))

    def test_offline_active_session_starts_without_file_writes_or_switch(self):
        client = self.client()
        offline = {'active': 'one', 'server': {'state': 'offline'}, 'operation': None, 'recovery_required': False}
        running = dict(offline, server={'state': 'running'})
        client.snapshot = Mock(side_effect=[offline, offline, running])
        client.manager.api = Mock()
        client.manager.wait_state = Mock()
        self.assertEqual(client.play('one'), {'state': 'complete'})
        client.manager.api.assert_called_once_with('/power', {'signal': 'start'})
        client.manager.wait_state.assert_called_once_with('running')

    def test_start_rechecks_active_session_and_lock(self):
        for fresh in ({'active': 'two', 'operation': None}, {'active': 'one', 'operation': {'by': 'Owner'}}):
            client = self.client()
            offline = {'active': 'one', 'server': {'state': 'offline'}, 'operation': None, 'recovery_required': False}
            client.snapshot = Mock(side_effect=[offline, dict(offline, **fresh)])
            client.manager.api = Mock()
            with self.assertRaisesRegex(ValueError, 'changed'): client.play('one')
            client.manager.api.assert_not_called()

    def test_failed_connection_does_not_replace_existing_settings_or_key(self):
        invite = dict(type='reis-direct', version=1, panel_url='https://panel.example.com', server_id='example', game_host='localhost', game_port=25565)
        with patch('shared.ReisClient') as client, patch('shared.save_local') as save, patch('keyring.set_password') as store:
            client.return_value.snapshot.side_effect = RuntimeError('Forbidden')
            with self.assertRaises(RuntimeError): shared.connect_reis(invite, 'friend-key')
            save.assert_not_called(); store.assert_not_called()

    def test_connection_export_contains_no_credentials_or_owner_paths(self):
        manager = MagicMock()
        manager.config = dict(panel_url='https://panel.example.com', server_id='example', game_host='localhost', game_port=25565,
                              sftp_user='OWNER', client_path='PRIVATE')
        with patch('shared.export_catalog'), patch.object(shared.CATALOG.__class__, 'read_text', return_value='{}'), patch('sessions.write') as write:
            invite = shared.export_reis_connection(manager)
        self.assertEqual(set(invite), set(shared.REIS_FIELDS) | {'type', 'version'})
        self.assertEqual(write.call_args.args[1], '_control_sessions/catalog.json')

