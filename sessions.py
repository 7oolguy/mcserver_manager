"""Whole-server sessions. Renames are journaled before touching active files."""
import hashlib
import json
import re
import stat
import zipfile
import urllib.parse
import urllib.request
import tarfile
import tempfile
import contextlib
from datetime import datetime
from pathlib import Path

ROOT = '_control_sessions'
RESERVED = {ROOT, '_control_backups'}


def backup(manager):
    manager.require_offline()
    files = [e['name'] for e in manager.list_files()
             if e['name'] not in RESERVED and not e['name'].startswith(('_codex_backup_', 'archive-'))]
    manager.progress('Creating complete server snapshot on the host…')
    record = manager.api('/files/compress', {'root': '/', 'files': files})
    filename = record['attributes']['name']
    url = manager.api('/files/download?file=' + urllib.parse.quote('/' + filename, safe=''))['attributes']['url']
    output = Path(manager.config['backup_path']); output.mkdir(parents=True, exist_ok=True)
    target = output / filename
    partial = target.with_suffix(target.suffix + '.partial')
    manager.progress('Downloading complete server snapshot…')
    with urllib.request.urlopen(url, timeout=60) as source, partial.open('wb') as dest:
        while chunk := source.read(1024 * 1024):
            manager.check_cancel(); dest.write(chunk)
    manager.progress('Verifying complete server snapshot…')
    count = 0
    with tarfile.open(partial, 'r:gz') as archive:
        for entry in archive:
            if entry.isfile():
                with archive.extractfile(entry) as source:
                    while source.read(1024 * 1024): pass
                count += 1
    partial.replace(target)
    return {'backup': str(target), 'files': count, 'host_copy': filename}


def backup_sftp(manager):
    """Independent local snapshot of every active server file, including all worlds."""
    manager.require_offline()
    output = Path(manager.config['backup_path'])
    output.mkdir(parents=True, exist_ok=True)
    target = output / ('full-server-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.zip')
    temporary = target.with_suffix('.partial')
    hashes = {}
    with manager.sftp() as sftp, zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
        def walk(folder):
            for entry in sftp.listdir_attr(folder):
                if folder == '.' and (entry.filename in RESERVED or entry.filename.startswith('_codex_backup_')):
                    continue
                path = entry.filename if folder == '.' else folder + '/' + entry.filename
                if stat.S_ISDIR(entry.st_mode):
                    walk(path)
                elif stat.S_ISREG(entry.st_mode):
                    manager.check_cancel()
                    manager.progress('Full server backup: ' + path)
                    digest = hashlib.sha256()
                    with sftp.open(path, 'rb') as source, archive.open(path, 'w', force_zip64=True) as dest:
                        source.prefetch(file_size=sftp.stat(path).st_size, max_concurrent_requests=16)
                        while chunk := source.read(1024 * 1024):
                            digest.update(chunk); dest.write(chunk)
                    hashes[path] = digest.hexdigest()
                else:
                    raise ValueError('Unsupported file in backup: ' + path)
        walk('.')
        archive.writestr('session-backup-manifest.json', json.dumps(hashes, indent=2))
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip(): raise RuntimeError('Backup verification failed.')
    temporary.replace(target)
    return {'backup': str(target), 'files': len(hashes)}


def name(value):
    if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', value):
        raise ValueError('Session ID: use lowercase letters, numbers, underscores and hyphens.')
    return value


def exists(sftp, path):
    try:
        sftp.lstat(path)
        return True
    except FileNotFoundError:
        return False


def read(sftp, path):
    source = path if exists(sftp, path) else path + '.previous'
    with sftp.open(source, 'rb') as stream:
        return json.loads(stream.read())


def write(sftp, path, value):
    with sftp.open(path + '.tmp', 'wb') as stream:
        stream.write(json.dumps(value, indent=2).encode())
    try:
        sftp.posix_rename(path + '.tmp', path)
    except OSError:
        if not exists(sftp, path + '.tmp'):
            raise
        # Some Wings SFTP servers reject overwrite even for posix-rename.
        # Under the session lock, retain a readable previous generation.
        if exists(sftp, path):
            if exists(sftp, path + '.previous'): sftp.remove(path + '.previous')
            sftp.rename(path, path + '.previous')
        sftp.rename(path + '.tmp', path)


def has_record(sftp, path):
    return exists(sftp, path) or exists(sftp, path + '.previous')


def blocked(manager):
    with manager.sftp() as sftp:
        return (exists(sftp, ROOT + '/lock') or has_record(sftp, ROOT + '/pending.json')
                or (exists(sftp, ROOT + '/operation') and not getattr(manager, '_session_operation', False)))


def inventory(manager):
    with manager.sftp() as sftp:
        if not has_record(sftp, ROOT + '/state.json'):
            return {'active': None, 'sessions': {}, 'note': 'Register the current server first.'}
        result = read(sftp, ROOT + '/state.json')
        result['recovery_required'] = exists(sftp, ROOT + '/lock') or has_record(sftp, ROOT + '/pending.json')
        result['operation_active'] = exists(sftp, ROOT + '/operation')
        return result


def register(manager, session_id, label):
    name(session_id)
    manager.require_offline()
    with manager.sftp() as sftp:
        manager.mkdirs(sftp, ROOT)
        if has_record(sftp, ROOT + '/state.json') or exists(sftp, ROOT + '/lock'):
            raise RuntimeError('Sessions are already registered or require recovery.')
        write(sftp, ROOT + '/state.json', {'active': session_id, 'sessions': {
            session_id: {'label': label, 'client_path': manager.config['client_path']}}})
    return inventory(manager)


def import_folder(manager, session_id, label, folder, client_path=''):
    name(session_id)
    folder = Path(folder).resolve()
    for required in ('mods', 'server.properties'):
        if not (folder / required).exists():
            raise ValueError('Prepared server folder must contain mods and server.properties.')
    paths = list(folder.rglob('*'))
    if any(p.is_symlink() for p in paths) or any(p.name in RESERVED for p in folder.iterdir()):
        raise ValueError('Server folder contains reserved names or symbolic links.')
    # One verified archive avoids thousands of round trips over SFTP.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='bec-session-') as temporary:
        package = Path(temporary) / 'server.zip'
        with zipfile.ZipFile(package, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path in paths:
                manager.check_cancel()
                if path.is_file(): archive.write(path, path.relative_to(folder).as_posix())
        return import_archive(manager, session_id, label, package, client_path)


def upload_archive(manager, sftp, package, remote):
    """Stream a multipart upload directly to Wings without buffering the ZIP."""
    url = manager.api('/files/upload')['attributes']['url']
    separator = '&' if '?' in url else '?'
    directory, filename = remote.rsplit('/', 1)
    url += separator + urllib.parse.urlencode({'directory': '/' + directory})
    boundary = 'bec-session-upload-boundary'
    header = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
              f'filename="{filename}"\r\nContent-Type: application/zip\r\n\r\n').encode()
    trailer = f'\r\n--{boundary}--\r\n'.encode()
    size = package.stat().st_size
    def chunks():
        yield header
        done, previous = 0, -1
        with package.open('rb') as source:
            while chunk := source.read(1024 * 1024):
                manager.check_cancel(); yield chunk
                done += len(chunk)
                percent = int(100 * done / max(1, size))
                if percent != previous:
                    previous = percent; manager.progress(f'Uploading session: {percent}%')
        yield trailer
    request = urllib.request.Request(url, data=chunks(), method='POST', headers={
        'Content-Type': 'multipart/form-data; boundary=' + boundary,
        'Content-Length': str(len(header) + size + len(trailer))})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            response.read()
    except urllib.error.HTTPError as error:
        detail = error.read(2000).decode(errors='replace')
        raise RuntimeError(f'Host upload failed (HTTP {error.code}): {detail}') from None


@contextlib.contextmanager
def upload_parts(package):
    """Keep requests below common hosting upload limits without losing ZIP paths."""
    with tempfile.TemporaryDirectory(prefix='bec-parts-') as temporary, zipfile.ZipFile(package) as source:
        groups, current, size = [], [], 0
        for entry in source.infolist():
            if entry.is_dir(): continue
            if entry.compress_size > 90 * 1024 * 1024:
                raise ValueError('One server file exceeds the supported upload size; use SFTP for this pack.')
            if current and size + entry.compress_size > 48 * 1024 * 1024:
                groups.append(current); current, size = [], 0
            current.append(entry); size += entry.compress_size
        if current: groups.append(current)
        parts = []
        for index, group in enumerate(groups):
            part = Path(temporary) / f'part-{index:03d}.zip'
            with zipfile.ZipFile(part, 'w', zipfile.ZIP_DEFLATED) as archive:
                for entry in group:
                    with source.open(entry) as src, archive.open(entry.filename, 'w') as dst:
                        while chunk := src.read(1024 * 1024): dst.write(chunk)
            parts.append(part)
        yield parts


def import_archive(manager, session_id, label, package, client_path=''):
    name(session_id)
    package = Path(package)
    with zipfile.ZipFile(package) as archive:
        names = archive.namelist()
        if archive.testzip(): raise ValueError('Server ZIP failed integrity verification.')
        if 'server.properties' not in names or not any(n.startswith('mods/') and n.endswith('.jar') for n in names):
            raise ValueError('ZIP must contain server files at its root, including mods and server.properties.')
        for entry in archive.infolist():
            parts = entry.filename.replace('\\', '/').split('/')
            if entry.filename.startswith(('/', '\\')) or '\\' in entry.filename or ':' in entry.filename or '..' in parts or parts[0] in RESERVED or parts[0] == '_session_upload.zip':
                raise ValueError('Unsafe path in ZIP.')
            if stat.S_ISLNK(entry.external_attr >> 16): raise ValueError('Links are not supported in server ZIPs.')
    manager.require_offline()
    with manager.sftp() as sftp:
        sftp.mkdir(ROOT + '/lock')
        try:
            state = read(sftp, ROOT + '/state.json')
            destination = ROOT + '/' + session_id
            if session_id in state['sessions'] or exists(sftp, destination):
                raise ValueError('Session already exists; incomplete imports are retained for inspection.')
            manager.mkdirs(sftp, destination)
            with upload_parts(package) as parts:
                for index, part in enumerate(parts):
                    manager.progress(f'Server archive {index + 1}/{len(parts)}')
                    remote = destination + '/_session_upload.zip'
                    upload_archive(manager, sftp, part, remote)
                    digest = hashlib.sha256()
                    with sftp.open(remote, 'rb') as stream:
                        stream.prefetch(file_size=part.stat().st_size, max_concurrent_requests=32)
                        while chunk := stream.read(1024 * 1024): digest.update(chunk)
                    with part.open('rb') as stream:
                        local = hashlib.file_digest(stream, 'sha256').hexdigest()
                    if digest.hexdigest() != local:
                        raise RuntimeError('Session upload checksum failed; active server preserved.')
                    manager.progress('Unpacking verified server archive…')
                    manager.api('/files/decompress', {'root': '/' + destination, 'file': '_session_upload.zip'})
                    sftp.remove(remote)
            for required in ('mods', 'server.properties'):
                if not exists(sftp, destination + '/' + required): raise RuntimeError('Server extraction incomplete.')
            state['sessions'][session_id] = {'label': label, 'client_path': client_path}
            write(sftp, ROOT + '/state.json', state)
        finally:
            sftp.rmdir(ROOT + '/lock')
    return inventory(manager)


def finish(manager, sftp):
    pending = read(sftp, ROOT + '/pending.json')
    for index in range(pending.get('next', 0), len(pending['moves'])):
        source, destination = pending['moves'][index]
        src, dst = exists(sftp, source), exists(sftp, destination)
        if src and not dst:
            manager.progress('Switching session: ' + source)
            sftp.rename(source, destination)
        elif src or not dst:
            raise RuntimeError('Ambiguous session recovery; preserved both locations: ' + source)
        pending['next'] = index + 1
        write(sftp, ROOT + '/pending.json', pending)
    write(sftp, ROOT + '/state.json', pending['state'])
    for suffix in ('.previous', '.tmp', ''):
        path = ROOT + '/pending.json' + suffix
        if exists(sftp, path): sftp.remove(path)
    sftp.rmdir(ROOT + '/lock')


def switch(manager, session_id, start=False):
    name(session_id)
    with manager.sftp() as sftp:
        if not has_record(sftp, ROOT + '/state.json'): raise ValueError('Register the current server session first.')
        try: sftp.mkdir(ROOT + '/operation')
        except OSError: raise RuntimeError('Another session operation is active or interrupted. Check its status before retrying.') from None
    manager._session_operation = True
    try:
        return _switch(manager, session_id, start)
    finally:
        manager._session_operation = False
        with manager.sftp() as sftp: sftp.rmdir(ROOT + '/operation')


def _switch(manager, session_id, start=False):
    name(session_id)
    # Validate destination before stopping players.
    initial = inventory(manager)
    if initial.get('recovery_required'):
        raise RuntimeError('Recover the interrupted session operation first.')
    if session_id not in initial['sessions']:
        raise ValueError('Unknown session.')
    if session_id == initial['active']:
        if start and manager.status()['state'] == 'offline': initial['server'] = manager.power('start')
        return initial
    manager.save_stop()
    manager.require_offline()
    with manager.sftp() as sftp:
        sftp.mkdir(ROOT + '/lock')
        journaled = False
        try:
            state = read(sftp, ROOT + '/state.json')
            previous = name(state['active'])
            target = ROOT + '/' + session_id
            old = ROOT + '/' + previous
            manager.mkdirs(sftp, old)
            if sftp.listdir_attr(old):
                raise RuntimeError('Previous session storage is not empty. Files preserved.')
            active = [e.filename for e in sftp.listdir_attr('.') if e.filename not in RESERVED
                      and not e.filename.startswith('_codex_backup_')]
            incoming = [e.filename for e in sftp.listdir_attr(target)]
            if 'server.properties' not in incoming or 'mods' not in incoming:
                raise RuntimeError('Destination is incomplete; active server preserved.')
            if any(v in RESERVED or '/' in v or v in ('.', '..') for v in active + incoming):
                raise ValueError('Invalid session file name.')
            moves = [[v, old + '/' + v] for v in active]
            moves += [[target + '/' + v, v] for v in incoming]
            state['active'] = session_id
            write(sftp, ROOT + '/pending.json', {'state': state, 'moves': moves})
            journaled = True
            finish(manager, sftp)
        except Exception:
            if not journaled:
                sftp.rmdir(ROOT + '/lock')
            raise
    result = inventory(manager)
    if start:
        result['server'] = manager.power('start')
    return result


def recover(manager):
    manager.require_offline(allow_session_recovery=True)
    with manager.sftp() as sftp:
        if has_record(sftp, ROOT + '/pending.json'):
            finish(manager, sftp)
        elif exists(sftp, ROOT + '/lock'):
            raise RuntimeError('Unjournaled operation lock: inspect incomplete import before removing lock.')
        if exists(sftp, ROOT + '/operation'): sftp.rmdir(ROOT + '/operation')
    return inventory(manager)
