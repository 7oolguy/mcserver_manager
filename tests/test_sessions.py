import contextlib
import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import shutil
import zipfile
import unittest
from unittest.mock import Mock, patch

from core import Manager, DEFAULTS
import sessions


class LocalSFTP:
    def __init__(self, root):
        self.root = Path(root)
        self.fail_after = None
        self.moves = 0
        self.fail_journal_after_move = False
        self.connection_lost = False
        self.reject_overwrite = False

    def lstat(self, path): return (self.root / path).lstat()
    def mkdir(self, path): (self.root / path).mkdir()
    def rmdir(self, path): (self.root / path).rmdir()
    def open(self, path, mode):
        stream = (self.root / path).open(mode)
        stream.prefetch = lambda **kwargs: None
        return stream
    def stat(self, path): return (self.root / path).stat()
    def put(self, source, dest, callback=None):
        shutil.copyfile(source, self.root / dest)
        if callback: callback(1, 1)
    def remove(self, path): (self.root / path).unlink()
    def posix_rename(self, src, dst):
        if self.fail_journal_after_move and self.moves and dst.endswith('/pending.json'):
            self.fail_journal_after_move = False
            self.connection_lost = True
            raise OSError('Connection lost after rename, before journal update')
        if self.reject_overwrite and (self.root / dst).exists(): raise OSError('Overwrite unsupported')
        os.replace(self.root / src, self.root / dst)
    def rename(self, src, dst):
        if self.connection_lost: raise OSError('Connection lost')
        if self.fail_after is not None and self.moves == self.fail_after:
            raise OSError('Simulated connection loss')
        if (self.root / dst).exists(): raise FileExistsError(dst)
        (self.root / src).rename(self.root / dst)
        self.moves += 1
    def listdir_attr(self, path):
        return [SimpleNamespace(filename=p.name, st_mode=p.lstat().st_mode) for p in (self.root / path).iterdir()]


class SessionTests(unittest.TestCase):
    def setup_server(self, root):
        sftp = LocalSFTP(root)
        manager = Manager(dict(DEFAULTS))
        manager.status = Mock(return_value={'state': 'offline'})
        manager.save_stop = Mock()
        @contextlib.contextmanager
        def connection(): yield sftp
        manager.sftp = connection
        (root / 'mods').mkdir()
        (root / 'mods/old.jar').write_bytes(b'old mod')
        (root / 'server.properties').write_text('level-name=world')
        (root / 'world').mkdir()
        (root / 'world/player.dat').write_bytes(b'inventory and progress')
        sessions.register(manager, 'old', 'Old pack')
        target = root / sessions.ROOT / 'new'
        target.mkdir(); (target / 'mods').mkdir()
        (target / 'mods/new.jar').write_bytes(b'new mod')
        (target / 'server.properties').write_text('level-name=new_world')
        state = sessions.read(sftp, sessions.ROOT + '/state.json')
        state['sessions']['new'] = {'label': 'New pack'}
        sessions.write(sftp, sessions.ROOT + '/state.json', state)
        return manager, sftp

    def test_round_trip_preserves_world_and_mods(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); manager, _ = self.setup_server(root)
            sessions.switch(manager, 'new')
            self.assertFalse((root / 'world').exists())
            (root / 'new_world').mkdir()
            (root / 'new_world/player.dat').write_bytes(b'new inventory')
            sessions.switch(manager, 'old')
            self.assertEqual((root / 'world/player.dat').read_bytes(), b'inventory and progress')
            self.assertEqual((root / 'mods/old.jar').read_bytes(), b'old mod')
            sessions.switch(manager, 'new')
            self.assertEqual((root / 'new_world/player.dat').read_bytes(), b'new inventory')

    def test_recover_each_interruption_point(self):
        for failure in range(5):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as directory:
                root = Path(directory); manager, sftp = self.setup_server(root)
                sftp.fail_after = failure
                with self.assertRaises(OSError): sessions.switch(manager, 'new')
                self.assertTrue(sessions.blocked(manager))
                with self.assertRaises(RuntimeError): manager.power('start')
                sftp.fail_after = None
                result = sessions.recover(manager)
                self.assertEqual(result['active'], 'new')
                self.assertFalse(sessions.blocked(manager))
                sessions.switch(manager, 'old')
                self.assertEqual((root / 'world/player.dat').read_bytes(), b'inventory and progress')

    def test_failed_save_never_moves_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); manager, _ = self.setup_server(root)
            manager.save_stop.side_effect = TimeoutError('Save not confirmed')
            with self.assertRaises(TimeoutError): sessions.switch(manager, 'new')
            self.assertTrue((root / 'world/player.dat').exists())
            self.assertEqual(sessions.inventory(manager)['active'], 'old')

    def test_recovery_after_rename_before_journal_update(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); manager, sftp = self.setup_server(root)
            sftp.fail_journal_after_move = True
            with self.assertRaises(OSError): sessions.switch(manager, 'new')
            sftp.connection_lost = False
            self.assertEqual(sessions.recover(manager)['active'], 'new')
            sessions.switch(manager, 'old')
            self.assertEqual((root / 'world/player.dat').read_bytes(), b'inventory and progress')

    def test_host_without_posix_overwrite_support(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); manager, sftp = self.setup_server(root)
            sftp.reject_overwrite = True
            sessions.switch(manager, 'new')
            self.assertFalse(sessions.blocked(manager))
            sessions.switch(manager, 'old')
            self.assertFalse(sessions.blocked(manager))
            self.assertEqual((root / 'world/player.dat').read_bytes(), b'inventory and progress')

    def test_invalid_destination_does_not_stop_server(self):
        with tempfile.TemporaryDirectory() as directory:
            manager, _ = self.setup_server(Path(directory))
            with self.assertRaises(ValueError): sessions.switch(manager, 'missing')
            manager.save_stop.assert_not_called()

    def test_import_archive_and_load_preserves_old_server(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as source:
            root = Path(directory); manager, _ = self.setup_server(root)
            package = Path(source) / 'pack.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr('mods/pack.jar', b'new pack')
                archive.writestr('server.properties', 'level-name=pack_world')
                archive.writestr('kubejs/server_scripts/recipes.js', 'recipes')
            def api(route, payload):
                with zipfile.ZipFile(root / payload['root'].lstrip('/') / payload['file']) as archive:
                    archive.extractall(root / payload['root'].lstrip('/'))
            manager.api = api
            with patch('sessions.upload_archive', side_effect=lambda m, s, p, r: s.put(str(p), r)):
                result = sessions.import_archive(manager, 'pack', 'Pack', package)
            self.assertEqual(result['active'], 'old')
            self.assertFalse(sessions.blocked(manager))
            sessions.switch(manager, 'pack')
            self.assertEqual((root / 'kubejs/server_scripts/recipes.js').read_text(), 'recipes')
            sessions.switch(manager, 'old')
            self.assertEqual((root / 'world/player.dat').read_bytes(), b'inventory and progress')

    def test_import_rejects_traversal_before_connecting(self):
        with tempfile.TemporaryDirectory() as source:
            package = Path(source) / 'pack.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr('mods/pack.jar', b'jar')
                archive.writestr('server.properties', 'level-name=world')
                archive.writestr('../world/level.dat', b'bad')
            manager = Manager(); manager.sftp = Mock()
            with self.assertRaises(ValueError): sessions.import_archive(manager, 'pack', 'Pack', package)
            manager.sftp.assert_not_called()

    def test_corrupt_upload_never_becomes_selectable(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as source:
            root = Path(directory); manager, _ = self.setup_server(root)
            package = Path(source) / 'pack.zip'
            with zipfile.ZipFile(package, 'w') as archive:
                archive.writestr('mods/pack.jar', b'jar')
                archive.writestr('server.properties', 'level-name=world')
            def corrupt(manager, sftp, package, remote):
                with sftp.open(remote, 'wb') as stream: stream.write(b'corrupted upload')
            with patch('sessions.upload_archive', side_effect=corrupt):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    sessions.import_archive(manager, 'pack', 'Pack', package)
            self.assertNotIn('pack', sessions.inventory(manager)['sessions'])
            self.assertFalse(sessions.blocked(manager))
            self.assertEqual((root / 'world/player.dat').read_bytes(), b'inventory and progress')
