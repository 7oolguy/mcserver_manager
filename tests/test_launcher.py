import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlparse

from core import DEFAULTS
import launcher


class LauncherTests(unittest.TestCase):
    def profile(self, root):
        root = Path(root)
        (root / 'mods').mkdir()
        (root / 'minecraftinstance.json').write_text(json.dumps({
            'name': 'Correct pack', 'guid': '11111111-2222-4333-8444-555555555555'}))
        return str(root)

    def manager(self):
        manager = Mock(); manager.config = dict(DEFAULTS)
        manager.status.return_value = {'state': 'running'}
        return manager

    def test_uses_installed_profile_guid_in_curseforge_protocol(self):
        with tempfile.TemporaryDirectory() as root:
            info = launcher.profile_info(self.profile(root))
            url = urlparse(info['uri'])
            self.assertEqual((url.scheme, url.netloc), ('curseforge', 'launch-game'))
            self.assertEqual(parse_qs(url.query), {'instanceId': ['11111111-2222-4333-8444-555555555555'], 'gameId': ['432']})

    def test_launches_active_pack(self):
        with tempfile.TemporaryDirectory() as root:
            state = {'active': 'new', 'sessions': {'new': {'client_path': self.profile(root)}}}
            opener = Mock()
            with patch('launcher.sessions.inventory', return_value=state):
                result = launcher.launch_active(self.manager(), opener)
            self.assertEqual(result['profile'], 'Correct pack')
            opener.assert_called_once()

    def test_offline_server_does_not_launch_client(self):
        manager = self.manager(); manager.status.return_value = {'state': 'offline'}
        opener = Mock()
        with patch('launcher.sessions.inventory', return_value={'active': 'new', 'sessions': {'new': {}}}):
            with self.assertRaisesRegex(RuntimeError, 'Start the active server'):
                launcher.launch_active(manager, opener)
        opener.assert_not_called()

    def test_bad_profile_does_not_switch_server(self):
        with patch('launcher.sessions.inventory', return_value={'sessions': {'new': {'client_path': ''}}}), patch('launcher.sessions.switch') as switch:
            with self.assertRaises(ValueError): launcher.play_session(self.manager(), 'new', Mock())
            switch.assert_not_called()

    def test_launch_happens_after_server_switch(self):
        with tempfile.TemporaryDirectory() as root:
            state = {'active': 'old', 'sessions': {'new': {'client_path': self.profile(root)}}}
            calls = []
            def switch(*args, **kwargs):
                calls.append('server-ready'); return {'active': 'new', 'sessions': state['sessions']}
            with patch('launcher.sessions.inventory', return_value=state), patch('launcher.sessions.switch', side_effect=switch):
                launcher.play_session(self.manager(), 'new', lambda uri: calls.append('client-launch'))
            self.assertEqual(calls, ['server-ready', 'client-launch'])

    def test_failed_save_or_switch_never_launches_client(self):
        with tempfile.TemporaryDirectory() as root:
            state = {'sessions': {'new': {'client_path': self.profile(root)}}}
            opener = Mock()
            with patch('launcher.sessions.inventory', return_value=state), patch('launcher.sessions.switch', side_effect=TimeoutError('Save failed')):
                with self.assertRaises(TimeoutError): launcher.play_session(self.manager(), 'new', opener)
            opener.assert_not_called()

    def test_pending_recovery_blocks_client_launch(self):
        opener = Mock()
        with patch('launcher.sessions.inventory', return_value={'recovery_required': True}):
            with self.assertRaises(RuntimeError): launcher.launch_active(self.manager(), opener)
        opener.assert_not_called()
