"""Portable CurseForge client ZIP plus credential-free Reis setup metadata."""
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import struct
import tempfile
import uuid
import zipfile

from core import APP_HOME, Manager
import sessions
import shared

METADATA = 'bec-setup.json'
FOLDERS = {'mods', 'config', 'defaultconfigs', 'resourcepacks', 'shaderpacks', 'kubejs', 'datapacks'}
MAX_BYTES = 10 * 1024 ** 3


def server_list(address, label):
    """Minimal uncompressed Java NBT servers.dat with a single server entry."""
    def string(value):
        raw = value.encode('utf-8')
        return struct.pack('>H', len(raw)) + raw
    return (b'\x0a\0\0\x09' + string('servers') + b'\x0a\0\0\0\x01' +
            b'\x08' + string('name') + string(label) + b'\x08' + string('ip') + string(address) + b'\0\0')


def export(manager, session_id, output):
    state = sessions.inventory(manager)
    if state.get('recovery_required') or state.get('operation_active'):
        raise ValueError('Wait for the session operation to finish before exporting.')
    sessions.name(session_id)
    if session_id not in state['sessions']: raise ValueError('Select a registered session.')
    item = state['sessions'][session_id]
    profile = Path(item.get('client_path', '')).resolve()
    if not (profile / 'minecraftinstance.json').is_file(): raise ValueError('This session needs its matching local CurseForge profile.')
    output = Path(output).resolve()
    if profile == output or profile in output.parents: raise ValueError('Save the ZIP outside the client profile.')
    if output.exists(): raise ValueError('Choose a new filename; existing exports are preserved.')
    connection = shared.export_reis_connection(manager)
    catalog = json.loads(shared.CATALOG.read_text(encoding='utf-8'))
    setup = {'type': 'bec-friend-pack', 'version': 1, 'session': session_id,
             'requirements': catalog[session_id], 'connection': connection}
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='bec-export-', dir=output.parent) as folder:
        temporary = Path(folder) / 'pack.zip'
        exporter = Manager(dict(manager.config, client_path=str(profile)), progress=manager.progress, cancel=manager.cancel)
        exporter.export_pack(temporary)
        address = f"{connection['game_host']}:{connection['game_port']}"
        with zipfile.ZipFile(temporary, 'a', zipfile.ZIP_DEFLATED) as archive:
            loader, loader_version = catalog[session_id]['loader'].split('-', 1)
            loader = {'fabric': 'fabric-loader', 'quilt': 'quilt-loader'}.get(loader, loader)
            if loader not in ('neoforge', 'forge', 'fabric-loader', 'quilt-loader'): raise ValueError('Unsupported mod loader.')
            index = {'formatVersion': 1, 'game': 'minecraft', 'versionId': 'local-' + session_id,
                     'name': item['label'], 'files': [],
                     'dependencies': {'minecraft': catalog[session_id]['minecraft'], loader: loader_version}}
            archive.writestr('modrinth.index.json', json.dumps(index, indent=2))
            archive.writestr('overrides/servers.dat', server_list(address, item['label']))
            setup['files'] = {}
            for entry in archive.infolist():
                if entry.filename.startswith('overrides/') and not entry.is_dir():
                    with archive.open(entry) as source: digest = hashlib.file_digest(source, 'sha256').hexdigest()
                    setup['files'][entry.filename[len('overrides/'):]] = {'size': entry.file_size, 'sha256': digest}
            archive.writestr(METADATA, json.dumps(setup, indent=2))
        manager.progress('Verifying friend setup ZIP…')
        inspect(temporary)
        manager.check_cancel()
        # Exclusive creation prevents overwriting a file created during export.
        try:
            with output.open('xb') as target, temporary.open('rb') as source: shutil.copyfileobj(source, target)
        except FileExistsError: raise ValueError('That export filename is already in use.') from None
        except BaseException:
            output.unlink(missing_ok=True)
            raise
    return {'exported': str(output), 'session': session_id, 'bytes': output.stat().st_size,
            'note': 'Send this ZIP to your friend. In the Play app choose Import friend setup ZIP.'}


def inspect(path):
    """Validate before handing a user-supplied ZIP to CurseForge; never extract it."""
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 100000 or sum(e.file_size for e in entries) > MAX_BYTES:
            raise ValueError('Setup ZIP is too large.')
        seen = set()
        for entry in entries:
            raw = entry.filename.rstrip('/')
            parts = PurePosixPath(raw).parts
            if (not parts or raw.startswith('/') or '\\' in raw or ':' in raw or
                any(p in ('.', '..') or p.endswith((' ', '.')) or re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\.|$)', p, re.I) for p in raw.split('/')) or
                any(ord(c) < 32 for c in raw) or raw.casefold() in seen):
                raise ValueError('Unsafe or duplicate path in setup ZIP.')
            seen.add(raw.casefold())
            if stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
                raise ValueError('Links and encrypted files are not supported.')
            allowed = raw in ('manifest.json', 'modrinth.index.json', METADATA, 'overrides', 'overrides/servers.dat') or (
                len(parts) >= 2 and parts[0] == 'overrides' and parts[1] in FOLDERS)
            if not allowed: raise ValueError('Unexpected file in setup ZIP: ' + raw)
        for filename in ('manifest.json', 'modrinth.index.json', METADATA):
            if archive.getinfo(filename).file_size > 2 * 1024 ** 2: raise ValueError('Invalid setup metadata.')
        setup = json.loads(archive.read(METADATA))
        manifest = json.loads(archive.read('manifest.json'))
        index = json.loads(archive.read('modrinth.index.json'))
        if setup.get('type') != 'bec-friend-pack' or setup.get('version') != 1:
            raise ValueError('Choose a friend setup ZIP exported by the controller.')
        sessions.name(setup['session']); shared.reis_config(setup['connection'])
        if manifest.get('manifestType') != 'minecraftModpack' or manifest.get('overrides') != 'overrides' or manifest.get('files') != []:
            raise ValueError('Unsupported modpack manifest.')
        requirements = setup['requirements']
        if manifest['minecraft']['version'] != requirements['minecraft'] or manifest['minecraft']['modLoaders'][0]['id'] != requirements['loader']:
            raise ValueError('Minecraft or loader requirements do not match the ZIP.')
        loader, loader_version = requirements['loader'].split('-', 1)
        loader = {'fabric': 'fabric-loader', 'quilt': 'quilt-loader'}.get(loader, loader)
        if (index.get('formatVersion') != 1 or index.get('game') != 'minecraft' or index.get('files') != [] or
            index.get('dependencies') != {'minecraft': requirements['minecraft'], loader: loader_version}):
            raise ValueError('Invalid Modrinth pack metadata.')
        expected = {m['name']: m for m in requirements['mods']}
        if not expected or len(expected) != len(requirements['mods']): raise ValueError('Invalid client mod requirements.')
        found = {}; files = {}
        # Reading every file checks ZIP CRCs, including configs/scripts and resources.
        for entry in entries:
            if entry.is_dir(): continue
            digest = hashlib.sha256()
            with archive.open(entry) as source:
                while chunk := source.read(1024 * 1024): digest.update(chunk)
            parts = PurePosixPath(entry.filename).parts
            if entry.filename.startswith('overrides/'):
                files[entry.filename[len('overrides/'):]] = {'size': entry.file_size, 'sha256': digest.hexdigest()}
            if len(parts) == 3 and parts[:2] == ('overrides', 'mods') and parts[2].endswith('.jar'):
                found[parts[2]] = {'name': parts[2], 'size': entry.file_size, 'sha256': digest.hexdigest()}
        if found != expected: raise ValueError('Mod files do not match the client requirements.')
        if files != setup.get('files'): raise ValueError('Client configs or scripts do not match the setup metadata.')
        return setup


def import_pack(path, token):
    setup = inspect(path)
    # Authenticate against live server metadata, not a promise inside the ZIP.
    config = shared.reis_config(setup['connection'])
    snapshot = shared.ReisClient(config, token).snapshot()
    live = snapshot['sessions'].get(setup['session'])
    if not live or any(live.get(key) != setup['requirements'].get(key) for key in ('mods', 'minecraft', 'loader')):
        raise ValueError('This ZIP no longer matches the server. Ask the owner for a new export.')
    directory = APP_HOME / 'imports'; directory.mkdir(parents=True, exist_ok=True)
    extension = '.mrpack' if shared.load_local().get('launcher') == 'modrinth' else '.zip'
    target = directory / (str(uuid.uuid4()) + extension)
    partial = target.with_suffix('.partial')
    try:
        with Path(path).open('rb') as source, partial.open('xb') as output: shutil.copyfileobj(source, output)
        inspect(partial)  # Also validate the retained copy before opening it later.
        partial.replace(target)
        shared.connect_reis(setup['connection'], token)
        local = shared.load_local()
        local.setdefault('pending_imports', {})[setup['session']] = {'zip': str(target), 'requirements': setup['requirements'], 'files': setup['files']}
        shared.save_local(local)
    except BaseException:
        partial.unlink(missing_ok=True); target.unlink(missing_ok=True)
        raise
    completed = finish_pending()
    return {'zip': str(target), 'session': setup['session'], 'ready': setup['session'] in completed}


def finish_pending():
    local = shared.load_local(); completed = []
    for session_id, item in list(local.get('pending_imports', {}).items()):
        try: profile = shared.find_profile(item['requirements'], local['instances_root'], local['profiles'].get(session_id), verify=True)
        except ValueError: continue
        matches = True
        for relative, expected in item['files'].items():
            path = profile / relative
            if not path.is_file() or path.stat().st_size != expected['size']:
                matches = False; break
            with path.open('rb') as source: digest = hashlib.file_digest(source, 'sha256').hexdigest()
            if digest != expected['sha256']: matches = False; break
        if not matches: continue
        local['profiles'][session_id] = str(profile)
        del local['pending_imports'][session_id]; completed.append(session_id)
    if completed: shared.save_local(local)
    return completed


def open_curseforge():
    executable = Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/CurseForge Windows/CurseForge.exe'
    if not executable.is_file(): raise ValueError('Open CurseForge, choose Minecraft → Import, and select the setup ZIP.')
    os.startfile(str(executable))


def open_import(path):
    path = Path(path)
    inspect(path)
    if shared.load_local().get('launcher') == 'modrinth':
        target = path.with_suffix('.mrpack')
        if path != target:
            if target.exists(): inspect(target)
            else:
                with path.open('rb') as source, target.open('xb') as output: shutil.copyfileobj(source, output)
        os.startfile(str(target))
    else:
        open_curseforge()
