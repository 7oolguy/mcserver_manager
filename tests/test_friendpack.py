import hashlib
import json
from pathlib import Path
import sqlite3
import stat
import tempfile
import unittest
from unittest.mock import Mock, patch
import uuid
import zipfile
from contextlib import closing

from core import Manager, DEFAULTS
import friendpack
import launcher
import shared


class FriendPackTests(unittest.TestCase):
    def make_pack(self, root):
        profile = root / 'owner'
        for relative, content in {'mods/mod.jar': b'client-mod', 'config/test.toml': b'setting=true',
                                  'kubejs/server_scripts/progression.js': b'// progression',
                                  'saves/private/level.dat': b'PRIVATE WORLD', 'accounts.json': b'PRIVATE ACCOUNT'}.items():
            path = profile / relative; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(content)
        (profile / 'minecraftinstance.json').write_text(json.dumps({'name': 'Test pack', 'guid': str(uuid.uuid4()), 'gameVersion': '1.21.1', 'baseModLoader': {'name': 'neoforge-21.1.248'}}))
        connection = dict(type='reis-direct', version=1, panel_url='https://panel.example.com', server_id='example', game_host='game.example.com', game_port=25565)
        item = {'label': 'Test pack', 'minecraft': '1.21.1', 'loader': 'neoforge-21.1.248', 'mods': shared.jar_manifest(profile)}
        catalog = root / 'catalog.json'; catalog.write_text(json.dumps({'test': item}))
        manager = Manager(dict(DEFAULTS, client_path=str(profile)))
        state = {'active': 'test', 'sessions': {'test': {'label': 'Test pack', 'client_path': str(profile)}}}
        output = root / 'friend.zip'
        with patch('sessions.inventory', return_value=state), patch('shared.export_reis_connection', return_value=connection), patch('shared.CATALOG', catalog):
            friendpack.export(manager, 'test', output)
        return output, item

    def test_export_roundtrip_both_launchers_and_no_owner_data(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); output, item = self.make_pack(root)
            setup = friendpack.inspect(output)
            self.assertEqual(setup['requirements']['mods'], item['mods'])
            self.assertNotIn(str(root), json.dumps(setup))
            self.assertEqual(set(setup['connection']), set(shared.REIS_FIELDS) | {'type', 'version'})
            with zipfile.ZipFile(output) as archive:
                self.assertIn('overrides/kubejs/server_scripts/progression.js', archive.namelist())
                self.assertFalse(any('saves/' in name or 'accounts' in name or 'minecraftinstance' in name for name in archive.namelist()))
                self.assertEqual(json.loads(archive.read('modrinth.index.json'))['dependencies'], {'minecraft': '1.21.1', 'neoforge': '21.1.248'})
                self.assertIn(b'game.example.com:25565', archive.read('overrides/servers.dat'))
                self.assertTrue(archive.read('overrides/servers.dat').startswith(b'\x0a\0\0'))

    def test_traversal_duplicate_and_link_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); output, _ = self.make_pack(root)
            for name in ('../escape', 'overrides/config/../../escape', 'overrides/config/CON.txt', 'MANIFEST.JSON', 'overrides/config/bad:stream'):
                target = root / (str(uuid.uuid4()) + '.zip'); target.write_bytes(output.read_bytes())
                with zipfile.ZipFile(target, 'a') as archive: archive.writestr(name, 'bad')
                with self.subTest(name=name), self.assertRaises(ValueError): friendpack.inspect(target)
            target = root / 'link.zip'; target.write_bytes(output.read_bytes())
            entry = zipfile.ZipInfo('overrides/config/link'); entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            with zipfile.ZipFile(target, 'a') as archive: archive.writestr(entry, '/private')
            with self.assertRaises(ValueError): friendpack.inspect(target)

    def test_changed_mod_and_script_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); output, _ = self.make_pack(root)
            for changed in ('overrides/mods/mod.jar', 'overrides/kubejs/server_scripts/progression.js'):
                target = root / (str(uuid.uuid4()) + '.zip')
                with zipfile.ZipFile(output) as source, zipfile.ZipFile(target, 'w') as dest:
                    for entry in source.infolist(): dest.writestr(entry, b'tampered' if entry.filename == changed else source.read(entry))
                with self.subTest(changed=changed), self.assertRaises(ValueError): friendpack.inspect(target)

    def test_import_pending_then_detects_new_local_profile(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); output, item = self.make_pack(root)
            home = root / 'appdata'; instances = root / 'instances'; instances.mkdir()
            settings = home / 'player-settings.json'
            snapshot = {'sessions': {'test': item}}
            with patch('friendpack.APP_HOME', home), patch('shared.LOCAL_SETTINGS', settings), patch('shared.APP_HOME', home), patch('shared.ReisClient') as client, patch('shared.connect_reis'):
                local = shared.load_local(); local['instances_root'] = str(instances); shared.save_local(local)
                client.return_value.snapshot.return_value = snapshot
                result = friendpack.import_pack(output, 'friend-api-key')
                self.assertFalse(result['ready']); self.assertTrue(Path(result['zip']).exists())
                self.assertIn('test', shared.load_local()['pending_imports'])
                profile = instances / 'New local profile'; profile.mkdir()
                with zipfile.ZipFile(output) as archive:
                    for entry in archive.infolist():
                        if entry.filename.startswith('overrides/'):
                            path = profile / entry.filename[len('overrides/'):]; path.parent.mkdir(parents=True, exist_ok=True); path.write_bytes(archive.read(entry))
                (profile / 'minecraftinstance.json').write_text(json.dumps({'guid': str(uuid.uuid4())}))
                self.assertEqual(friendpack.finish_pending(), ['test'])
                self.assertEqual(shared.load_local()['profiles']['test'], str(profile))
                self.assertNotIn('friend-api-key', settings.read_text())

    def test_stale_pack_does_not_change_connection(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); output, _ = self.make_pack(root)
            with patch('shared.ReisClient') as client, patch('shared.connect_reis') as connect:
                client.return_value.snapshot.return_value = {'sessions': {'test': {'mods': []}}}
                with self.assertRaisesRegex(ValueError, 'no longer matches'): friendpack.import_pack(output, 'friend-key')
                connect.assert_not_called()

    def test_modrinth_registered_profile_has_local_launch_id(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); profile = root / 'instances' / 'my-pack'; (profile / 'mods').mkdir(parents=True)
            with closing(sqlite3.connect(root / 'app.db')) as db:
                db.execute('CREATE TABLE instances(id TEXT, path TEXT, name TEXT)')
                db.execute('INSERT INTO instances VALUES(?,?,?)', ('local-instance-id', 'my-pack', 'Local pack'))
                db.execute('CREATE TABLE minecraft_users(access_token TEXT)')
                db.execute("INSERT INTO minecraft_users VALUES('PRIVATE TOKEN')")
                db.commit()
            info = launcher.profile_info(profile, launcher='modrinth', modrinth_root=root)
            self.assertEqual(info['uri'], 'modrinth://launch/instance/local-instance-id')
            self.assertNotIn('PRIVATE TOKEN', json.dumps(info))
            self.assertEqual(Path(info['client_path']).resolve(), profile.resolve())

    def test_modrinth_selection_skips_old_curseforge_mapping(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder); old = root / 'curseforge'; (old / 'mods').mkdir(parents=True)
            (old / 'mods/mod.jar').write_bytes(b'mod')
            (old / 'minecraftinstance.json').write_text(json.dumps({'guid': str(uuid.uuid4())}))
            profile = root / 'modrinth' / 'instances' / 'local'; (profile / 'mods').mkdir(parents=True)
            (profile / 'mods/mod.jar').write_bytes(b'mod')
            with closing(sqlite3.connect(root / 'modrinth' / 'app.db')) as db:
                db.execute('CREATE TABLE instances(id TEXT, path TEXT, name TEXT)')
                db.execute("INSERT INTO instances VALUES('local-id','local','Pack')"); db.commit()
            config = dict(launcher='modrinth', instances_root=str(root), modrinth_root=str(root/'modrinth'))
            with patch('shared.load_local', return_value=config):
                found = shared.find_profile({'mods': shared.jar_manifest(old)}, mapped=old, verify=True)
            self.assertEqual(found.resolve(), profile.resolve())
