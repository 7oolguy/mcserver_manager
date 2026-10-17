"""Shared server operations for the desktop app and JSON command-line tool."""
import contextlib
import codecs
import hashlib
import json
import os
import re
import socket
import stat
import time
import urllib.error
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path, PurePosixPath

APP_HOME = Path(os.environ.get('APPDATA', str(Path.home()))) / 'Build Explore Control'
CONFIG_PATH = APP_HOME / 'settings.json'
DEFAULTS = {
    'panel_url': 'https://panel.example.invalid', 'server_id': 'configure-me',
    'game_host': 'localhost', 'game_port': 25565,
    'sftp_host': 'localhost', 'sftp_port': 2022,
    'sftp_user': '',
    'host_sha256': '0' * 64,
    'client_path': str(Path.home() / 'curseforge/minecraft/Instances/Build Explore and Create'),
    'backup_path': str(Path.home() / 'Documents/Build Explore Backups'),
}


def settings():
    result = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        result.update(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
    for key in DEFAULTS:
        value = os.environ.get('BEC_' + key.upper())
        if value is not None: result[key] = int(value) if key in ('game_port', 'sftp_port') else value
    return result


def save_settings(values):
    result = dict(DEFAULTS)
    result.update({k: v for k, v in values.items() if k in DEFAULTS})
    for k in ('game_port', 'sftp_port'):
        result[k] = int(result[k])
        if not 1 <= result[k] <= 65535:
            raise ValueError('Ports must be between 1 and 65535.')
    if not re.fullmatch(r'[a-zA-Z0-9-]+', result['server_id']):
        raise ValueError('Invalid panel server ID.')
    if not result['panel_url'].startswith('https://'):
        raise ValueError('The panel URL must use HTTPS.')
    if not re.fullmatch(r'[0-9a-fA-F]{64}', result['host_sha256']):
        raise ValueError('SFTP host fingerprint must be 64 hexadecimal characters.')
    APP_HOME.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix('.tmp')
    temporary.write_text(json.dumps(result, indent=2), encoding='utf-8')
    temporary.replace(CONFIG_PATH)
    return result


def credential_name(config, kind):
    return config['panel_url'].rstrip('/') + '/' + config['server_id'] + '/' + kind


def secret(config, kind):
    value = os.environ.get('BEC_API_KEY' if kind == 'api_key' else 'BEC_SFTP_PASSWORD')
    if value:
        return value
    import keyring
    value = keyring.get_password('Build Explore Control', credential_name(config, kind))
    if not value:
        raise ValueError('Add your panel API key in Settings.' if kind == 'api_key'
                         else 'Add your SFTP password in Settings.')
    return value


def store_secret(config, kind, value):
    import keyring
    keyring.set_password('Build Explore Control', credential_name(config, kind), value)


class LogCursor:
    """Incremental UTF-8 log tail with restart/truncation detection."""
    def __init__(self):
        self.offset = None
        self.prefix = None
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.pending = ''

    def read(self, stream):
        size = stream.stat().st_size
        stream.seek(0)
        prefix = stream.read(min(128, size))
        reset = self.offset is None or size < self.offset or (
            self.prefix is not None and not prefix.startswith(self.prefix))
        if reset:
            self.decoder.reset(); self.pending = ''
            self.offset = max(0, size - 128000)
        self.prefix = prefix
        stream.seek(self.offset)
        chunk = stream.read(min(128000, max(0, size - self.offset)))
        self.offset += len(chunk)
        text = self.pending + self.decoder.decode(chunk)
        if reset and size > 128000:
            text = text.partition('\n')[2]
        last = text.rfind('\n')
        if last < 0:
            self.pending = text
            return {'reset': reset, 'text': '', 'offset': self.offset}
        self.pending = text[last + 1:]
        return {'reset': reset, 'text': text[:last + 1], 'offset': self.offset}


def redact(text):
    return re.sub(r'ptlc_[A-Za-z0-9]+', '[API KEY REDACTED]', text)


def safe_remote(path):
    path = str(path).replace('\\', '/')
    if path in ('', '.'):
        return '.'
    if path.startswith('/') or any(p in ('..', '') for p in path.split('/')) or ':' in path:
        raise ValueError('Use a relative server path without .. or drive letters.')
    return str(PurePosixPath(path))


def properties(text, updates=None):
    data = {}
    for line in text.splitlines():
        if line.strip() and not line.lstrip().startswith(('#', '!')) and '=' in line:
            key, value = line.split('=', 1)
            data[key.strip()] = value.strip()
    if updates is None:
        return data
    for key, value in updates.items():
        if any(c in str(key) + str(value) for c in '\r\n'):
            raise ValueError('Property values cannot contain line breaks.')
    lines, seen = [], set()
    for line in text.splitlines():
        key = line.split('=', 1)[0].strip()
        if not line.lstrip().startswith(('#', '!')) and key in updates:
            if key not in seen:
                lines.append(key + '=' + str(updates[key]))
                seen.add(key)
        else:
            lines.append(line)
    lines += [k + '=' + str(v) for k, v in updates.items() if k not in seen]
    return '\n'.join(lines) + '\n'


class Manager:
    def __init__(self, config=None, progress=None, cancel=None):
        self.config = config or settings()
        self.progress = progress or (lambda message: None)
        self.cancel = cancel

    def check_cancel(self):
        if self.cancel is not None and self.cancel.is_set():
            raise RuntimeError('Operation cancelled. Existing files were preserved; an incomplete .partial file may remain.')

    def api(self, route='', payload=None):
        base = self.config['panel_url'].rstrip('/')
        if not base.startswith('https://'):
            raise ValueError('Panel connection requires HTTPS.')
        key = secret(self.config, 'api_key')
        url = base + '/api/client/servers/' + self.config['server_id'] + route
        request = urllib.request.Request(url, data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + key, 'Accept': 'Application/vnd.pterodactyl.v1+json',
                     'Content-Type': 'application/json', 'User-Agent': 'BuildExploreControl/1.0'})
        # Never forward credentials to a redirect destination.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {'ok': True}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f'Panel returned HTTP {exc.code}. Check the API key, permissions, and panel URL.') from None

    def status(self):
        result = self.api('/resources')['attributes']
        return {'state': result['current_state'], 'resources': result.get('resources', {})}

    def ping(self):
        try:
            with socket.create_connection((self.config['game_host'], int(self.config['game_port'])), timeout=5):
                return {'reachable': True, 'note': 'Game port is reachable; use panel status for confirmed state.'}
        except OSError:
            return {'reachable': False, 'note': 'Game port did not answer.'}

    def wait_state(self, wanted, timeout=300):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            current = self.status()['state']
            self.progress('Server: ' + current)
            if current == wanted:
                return {'state': current}
            time.sleep(2)
        raise TimeoutError(f'Server did not reach {wanted} within {timeout} seconds. Check Logs.')

    def power(self, signal):
        if signal not in ('start', 'stop', 'restart'):
            raise ValueError('Only graceful start, stop, and restart are supported.')
        if signal in ('start', 'restart'):
            from sessions import blocked
            if blocked(self):
                raise RuntimeError('Session operation unfinished. Recover it before starting the server.')
        self.api('/power', {'signal': signal})
        if signal == 'restart':
            deadline = time.monotonic() + 60
            while True:
                current = self.status()['state']
                self.progress('Server: ' + current)
                if current != 'running':
                    break
                if time.monotonic() >= deadline:
                    raise TimeoutError('Restart transition was not confirmed. Check Logs.')
                time.sleep(2)
        return self.wait_state('offline' if signal == 'stop' else 'running')

    def command(self, command):
        if not command.strip():
            raise ValueError('Enter a console command.')
        self.api('/command', {'command': command})
        return {'sent': command}

    @contextlib.contextmanager
    def sftp(self):
        import paramiko
        transport = paramiko.Transport(socket.create_connection(
            (self.config['sftp_host'], int(self.config['sftp_port'])), timeout=20))
        client = None
        try:
            transport.start_client(timeout=20)
            fingerprint = hashlib.sha256(transport.get_remote_server_key().asbytes()).hexdigest()
            if fingerprint.lower() != self.config['host_sha256'].lower():
                raise RuntimeError('SFTP host fingerprint changed. Connection refused before sending the password.')
            transport.auth_password(self.config['sftp_user'], secret(self.config, 'sftp_password'))
            client = paramiko.SFTPClient.from_transport(transport)
            client.get_channel().settimeout(90)
            yield client
        finally:
            if client:
                client.close()
            transport.close()

    def logs(self, lines=150):
        with self.sftp() as sftp:
            with sftp.open('logs/latest.log', 'rb') as stream:
                size = stream.stat().st_size
                stream.seek(max(0, size - 512000))
                text = stream.read().decode('utf-8', errors='replace')
        return '\n'.join(text.splitlines()[-lines:])

    def save(self):
        marker = 'Saved the game'
        with self.sftp() as sftp:
            old_size = sftp.stat('logs/latest.log').st_size
        self.command('save-all flush')
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with self.sftp() as sftp:
                with sftp.open('logs/latest.log', 'rb') as stream:
                    stream.seek(old_size)
                    text = stream.read().decode('utf-8', errors='replace')
            if marker in text:
                return {'saved': True}
            time.sleep(2)
        raise TimeoutError('Save was requested, but completion was not confirmed in the log. Check Logs.')

    def save_stop(self):
        if self.status()['state'] == 'offline':
            return {'state': 'offline', 'note': 'Server was already stopped.'}
        self.save()
        return self.power('stop')

    def require_offline(self, allow_session_recovery=False):
        if self.status()['state'] != 'offline':
            raise RuntimeError('Save & Stop the server before changing files or worlds.')
        if not allow_session_recovery:
            from sessions import blocked
            if blocked(self):
                raise RuntimeError('Recover the unfinished session operation before changing server files.')

    def list_files(self, path='.'):
        path = safe_remote(path)
        with self.sftp() as sftp:
            return [{'name': x.filename, 'directory': stat.S_ISDIR(x.st_mode), 'size': x.st_size,
                     'symlink': stat.S_ISLNK(x.st_mode)} for x in sftp.listdir_attr(path)]

    def read_file(self, path):
        with self.sftp() as sftp:
            path = safe_remote(path)
            if sftp.stat(path).st_size > 2_000_000:
                raise ValueError('File is too large for the text editor. Download it instead.')
            with sftp.open(path, 'rb') as stream:
                return stream.read().decode('utf-8')

    def download(self, remote, local):
        target = Path(local)
        partial = target.with_name(target.name + '.partial')
        with self.sftp() as sftp:
            def progress(done, total):
                self.check_cancel()
                self.progress(f'Download: {done / max(1,total):.0%} ({done:,}/{total:,} bytes)')
            sftp.get(safe_remote(remote), str(partial), callback=progress)
        partial.replace(target)
        return {'downloaded': str(local)}

    @staticmethod
    def mkdirs(sftp, path):
        current = ''
        for part in safe_remote(path).split('/'):
            current = part if not current else current + '/' + part
            try:
                entry = sftp.lstat(current)
                if not stat.S_ISDIR(entry.st_mode):
                    raise ValueError('Destination contains a non-directory: ' + current)
            except FileNotFoundError:
                sftp.mkdir(current)

    def write_bytes(self, path, data):
        self.require_offline()
        path = safe_remote(path)
        if path == '.':
            raise ValueError('Select a file destination.')
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        archive = '_control_backups/' + stamp + '/' + path
        temporary = path + '.control-' + stamp
        with self.sftp() as sftp:
            self.mkdirs(sftp, str(PurePosixPath(path).parent))
            try:
                old = sftp.lstat(path)
                if not stat.S_ISREG(old.st_mode):
                    raise ValueError('Only regular files can be replaced.')
                self.mkdirs(sftp, str(PurePosixPath(archive).parent))
                with sftp.open(path, 'rb') as source, sftp.open(archive, 'wb') as target:
                    while chunk := source.read(1024 * 1024):
                        target.write(chunk)
            except FileNotFoundError:
                archive = None
            with sftp.open(temporary, 'wb') as stream:
                stream.write(data)
            with sftp.open(temporary, 'rb') as stream:
                digest = hashlib.sha256(stream.read()).hexdigest()
            if digest != hashlib.sha256(data).hexdigest():
                raise RuntimeError('Upload verification failed; original file preserved.')
            sftp.posix_rename(temporary, path)
        return {'written': path, 'previous_file_backup': archive, 'sha256': digest}

    def upload(self, local, remote):
        return self.write_bytes(remote, Path(local).read_bytes())

    def archive_mod(self, name):
        if Path(name).name != name or not name.endswith('.jar'):
            raise ValueError('Choose a mod JAR filename.')
        self.require_offline()
        target = '_control_backups/disabled-mods/' + datetime.now().strftime('%Y%m%d-%H%M%S-%f')
        with self.sftp() as sftp:
            self.mkdirs(sftp, target)
            sftp.rename('mods/' + name, target + '/' + name)
        return {'disabled': name, 'recoverable_at': target + '/' + name}

    def new_world(self, name, seed):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
            raise ValueError('World name: use letters, numbers, underscores, or hyphens.')
        self.require_offline()
        with self.sftp() as sftp:
            try:
                sftp.stat(name)
            except FileNotFoundError:
                pass
            else:
                raise ValueError('That world folder already exists. Choose a new name.')
        text = self.read_file('server.properties')
        previous = properties(text).get('level-name', 'world')
        result = self.write_bytes('server.properties', properties(text,
            {'level-name': name, 'level-seed': seed}).encode('utf-8'))
        result.update({'new_world': name, 'seed': seed, 'previous_world_preserved': previous,
                       'note': 'Start the server to generate the new world.'})
        return result

    def world_info(self):
        text = self.read_file('server.properties')
        config = properties(text)
        worlds = []
        with self.sftp() as sftp:
            for entry in sftp.listdir_attr('.'):
                if not stat.S_ISDIR(entry.st_mode) or entry.filename.startswith('_'):
                    continue
                try:
                    if stat.S_ISREG(sftp.lstat(safe_remote(entry.filename + '/level.dat')).st_mode):
                        worlds.append(entry.filename)
                except FileNotFoundError:
                    pass
        return {'active': config.get('level-name','world'), 'configured_seed': config.get('level-seed',''),
                'worlds': sorted(worlds), 'view_distance': config.get('view-distance'),
                'simulation_distance': config.get('simulation-distance')}

    def switch_world(self, name):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,80}', name):
            raise ValueError('Choose a valid world folder.')
        self.require_offline()
        with self.sftp() as sftp:
            if not stat.S_ISREG(sftp.lstat(name + '/level.dat').st_mode):
                raise ValueError('World does not contain a regular level.dat file.')
        text = self.read_file('server.properties')
        result = self.write_bytes('server.properties', properties(text, {'level-name': name}).encode())
        result['selected_world'] = name
        result['note'] = 'Start the server to load this existing world. Other worlds are preserved.'
        return result

    def backup_world(self):
        self.require_offline()
        text = self.read_file('server.properties')
        world = safe_remote(properties(text).get('level-name', 'world'))
        if world == '.':
            raise ValueError('Invalid world folder in server.properties.')
        output = Path(self.config['backup_path']).expanduser()
        output.mkdir(parents=True, exist_ok=True)
        target = output / (world.replace('/', '_') + '-' + datetime.now().strftime('%Y%m%d-%H%M%S-%f') + '.zip')
        temporary = target.with_suffix('.partial')
        checksums = {}
        with self.sftp() as sftp, zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            def walk(folder):
                for entry in sftp.listdir_attr(folder):
                    self.check_cancel()
                    path = safe_remote(folder + '/' + entry.filename)
                    if stat.S_ISDIR(entry.st_mode):
                        walk(path)
                    elif stat.S_ISREG(entry.st_mode):
                        self.progress('Backing up ' + path)
                        digest = hashlib.sha256()
                        with sftp.open(path, 'rb') as src, archive.open(path, 'w', force_zip64=True) as dst:
                            while chunk := src.read(1024 * 1024):
                                self.check_cancel()
                                digest.update(chunk)
                                dst.write(chunk)
                        checksums[path] = digest.hexdigest()
                    else:
                        raise ValueError('Backup stopped at unsupported link or file: ' + path)
            walk(world)
            archive.writestr('server.properties', text)
            archive.writestr('backup-manifest.json', json.dumps({'world': world, 'sha256': checksums}, indent=2))
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise RuntimeError('Backup verification failed.')
        temporary.replace(target)
        return {'backup': str(target), 'files': len(checksums)}

    def export_pack(self, output):
        profile = Path(self.config['client_path']).expanduser().resolve()
        if not (profile / 'mods').is_dir():
            raise ValueError('Select the installed client profile in Settings.')
        output = Path(output).resolve()
        if output == profile or profile in output.parents:
            raise ValueError('Save the export outside the client profile.')
        metadata_path = profile / 'minecraftinstance.json'
        metadata = json.loads(metadata_path.read_text(encoding='utf-8-sig')) if metadata_path.exists() else {}
        manifest = {'minecraft': {'version': metadata.get('gameVersion', '1.21.1'), 'modLoaders':
            [{'id': (metadata.get('baseModLoader') or {}).get('name', 'neoforge-21.1.257'), 'primary': True}]}, 'manifestType': 'minecraftModpack',
            'manifestVersion': 1, 'name': metadata.get('name', profile.name), 'version': 'local-export',
            'author': 'Local export', 'files': [], 'overrides': 'overrides'}
        temporary = output.with_suffix('.partial')
        with zipfile.ZipFile(temporary, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr('manifest.json', json.dumps(manifest, indent=2))
            for folder in ('mods', 'config', 'defaultconfigs', 'resourcepacks', 'shaderpacks', 'kubejs', 'datapacks'):
                for path in (profile / folder).rglob('*'):
                    self.check_cancel()
                    if path.is_file() and not path.is_symlink():
                        self.progress('Packing ' + path.name)
                        archive.write(path, 'overrides/' + path.relative_to(profile).as_posix())
        with zipfile.ZipFile(temporary) as check:
            if check.testzip() is not None:
                raise RuntimeError('Export verification failed.')
        temporary.replace(output)
        return {'exported': str(output), 'note': 'Import this ZIP in CurseForge using Create Custom Profile → Import.'}
