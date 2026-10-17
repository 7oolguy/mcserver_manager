"""Public server catalogue, local profile mappings, and authenticated shared clients."""
import hashlib
import json
import os
from pathlib import Path
import socket
import sys
import struct
import time
from urllib.parse import urlparse
import urllib.request
import uuid
from urllib.parse import quote

from core import APP_HOME, CONFIG_PATH, Manager
import sessions
import launcher

VERSION = '2.2.0'
LOCAL_SETTINGS = APP_HOME / 'player-settings.json'
CATALOG = APP_HOME / 'shared-catalog.json'


def installed_app():
    return Path(sys.executable).resolve().parent == Path(__file__).resolve().parent.parent / 'runtime'


def load_local():
    defaults = {'service_url': '', 'profiles': {}, 'instances_root': str(Path.home() / 'curseforge/minecraft/Instances'),
                'auto_update': True, 'update_url': '', 'launcher': 'curseforge',
                'modrinth_root': str(Path(os.environ.get('APPDATA', str(Path.home()))) / 'ModrinthApp')}
    if LOCAL_SETTINGS.exists(): defaults.update(json.loads(LOCAL_SETTINGS.read_text(encoding='utf-8')))
    return defaults


def save_local(data):
    APP_HOME.mkdir(parents=True, exist_ok=True)
    temp = LOCAL_SETTINGS.with_suffix('.tmp')
    temp.write_text(json.dumps(data, indent=2), encoding='utf-8'); temp.replace(LOCAL_SETTINGS)


def validate_url(url):
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError('Use a service address without credentials, query parameters, or fragments.')
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and parsed.hostname in ('127.0.0.1', 'localhost', '::1')):
        raise ValueError('Shared connections require HTTPS. HTTP is allowed only on this computer.')
    if not parsed.hostname: raise ValueError('Enter a valid service address.')
    return url.rstrip('/')


def connect(invite):
    if invite.get('type') == 'reis-direct':
        raise ValueError('Enter your own Reis API key to connect directly.')
    url = validate_url(invite['service_url'])
    token = invite.get('token', '')
    if not isinstance(token, str) or len(token) < 32: raise ValueError('Invalid invitation.')
    # Verify the invitation before replacing a working connection.
    RemoteClient(url, token).request('/v1/me')
    import keyring
    keyring.set_password('Build Explore Control Shared', url, token)
    config = load_local(); config.pop('reis', None); config['service_url'] = url; save_local(config)


def jar_manifest(folder):
    result = []
    for path in sorted((Path(folder) / 'mods').glob('*.jar')):
        with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        result.append({'name': path.name, 'size': path.stat().st_size, 'sha256': digest})
    return result


def export_catalog(manager):
    state = sessions.inventory(manager); result = {}
    for key, item in state['sessions'].items():
        folder = Path(item.get('client_path', ''))
        metadata = folder / 'minecraftinstance.json'
        if not metadata.is_file(): raise ValueError('Missing owner client profile: ' + item['label'])
        data = json.loads(metadata.read_text(encoding='utf-8-sig'))
        result[key] = {'label': item['label'], 'minecraft': data.get('gameVersion'),
                       'loader': (data.get('baseModLoader') or {}).get('name'),
                       'install_url': (data.get('installedModpack') or {}).get('webSiteURL', ''),
                       'mods': jar_manifest(folder)}
    APP_HOME.mkdir(parents=True, exist_ok=True)
    temporary = CATALOG.with_suffix('.tmp'); temporary.write_text(json.dumps(result, indent=2)); temporary.replace(CATALOG)
    return {'sessions': len(result), 'catalog': str(CATALOG)}


def public_catalog(state):
    catalogue = json.loads(CATALOG.read_text()) if CATALOG.exists() else {}
    return {key: dict(catalogue.get(key, {}), label=item['label']) for key, item in state['sessions'].items()}


def find_profile(item, root=None, mapped=None, verify=False):
    expected = item.get('mods') or []
    if not expected: raise ValueError('The owner needs to publish this pack’s client requirements first.')
    expected_sizes = {entry['name']: entry['size'] for entry in expected}
    local = load_local(); preference = local.get('launcher', 'curseforge')
    root = Path(root or local['instances_root'])
    candidates = [Path(mapped)] if mapped else []
    if preference == 'modrinth':
        candidates.extend(Path(info['client_path']) for info in launcher.modrinth_profiles(local.get('modrinth_root')))
    elif root.is_dir(): candidates.extend(p for p in root.iterdir() if p.is_dir() and p not in candidates)
    for folder in candidates:
        if preference == 'curseforge' and not (folder / 'minecraftinstance.json').is_file(): continue
        found = {p.name: p.stat().st_size for p in (folder / 'mods').glob('*.jar')}
        if found != expected_sizes: continue
        if verify and jar_manifest(folder) != expected: continue
        try: launcher.profile_info(str(folder), launcher=preference, modrinth_root=local.get('modrinth_root'))
        except ValueError: continue
        return folder
    raise ValueError('Import the matching modpack in your selected launcher, then locate its folder here.')


def player_info(host, port):
    """Read the public Minecraft status protocol; never issue console commands."""
    def varint(number):
        output = bytearray()
        while True:
            part = number & 127; number >>= 7; output.append(part | (128 if number else 0))
            if not number: return bytes(output)
    def receive(stream, count):
        output = b''
        while len(output) < count:
            part = stream.recv(count - len(output))
            if not part: raise OSError('Game connection closed.')
            output += part
        return output
    def read_varint(stream):
        result = 0
        for shift in range(0, 35, 7):
            byte = receive(stream, 1)[0]; result |= (byte & 127) << shift
            if not byte & 128: return result
        raise ValueError('Invalid game status response.')
    try:
        with socket.create_connection((host, int(port)), timeout=3) as stream:
            encoded = host.encode(); body = b'\0' + varint(767) + varint(len(encoded)) + encoded + struct.pack('>H', int(port)) + b'\1'
            stream.sendall(varint(len(body)) + body + b'\1\0')
            length = read_varint(stream)
            if length > 1024 * 1024 or read_varint(stream) != 0: raise ValueError('Invalid game status.')
            size = read_varint(stream)
            if size > 1024 * 1024: raise ValueError('Game status too large.')
            players = json.loads(receive(stream, size))['players']
            return {'online': int(players['online']), 'maximum': int(players['max']),
                    'names': [p['name'] for p in players.get('sample', []) if 'name' in p]}
    except (OSError, ValueError, KeyError): return {'online': None, 'names': []}


class LocalClient:
    role = 'owner'
    def __init__(self, manager=None): self.manager = manager or Manager()
    def snapshot(self):
        state = sessions.inventory(self.manager); status = self.manager.status()
        return {'active': state.get('active'), 'sessions': public_catalog(state), 'server': status,
                'players': player_info(self.manager.config['game_host'], self.manager.config['game_port']) if status['state'] == 'running' else {'online': 0, 'names': []},
                'recovery_required': state.get('recovery_required', False), 'role': self.role,
                'address': f"{self.manager.config['game_host']}:{self.manager.config['game_port']}",
                'operation': {'by': 'Another client'} if state.get('operation_active') else None}
    def play(self, session_id, allow_disconnect=False):
        state = sessions.inventory(self.manager)
        if session_id != state['active'] and not allow_disconnect:
            players = player_info(self.manager.config['game_host'], self.manager.config['game_port'])
            if players['online'] is None or players['online'] > 0:
                if self.manager.status()['state'] != 'offline': raise ValueError('Confirm the switch: players are online or their status is unavailable.')
        return sessions.switch(self.manager, session_id, start=True)


class RemoteClient:
    def __init__(self, url=None, token=None):
        self.url = validate_url(url or load_local()['service_url'])
        if token is None:
            import keyring
            token = keyring.get_password('Build Explore Control Shared', self.url)
        if not token: raise ValueError('Open an invitation from the owner to connect.')
        self.token = token
    def request(self, route, payload=None):
        request = urllib.request.Request(self.url + route,
            data=None if payload is None else json.dumps(payload).encode(),
            headers={'Authorization': 'Bearer ' + self.token, 'Content-Type': 'application/json'})
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs): return None
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
                return json.loads(response.read(2 * 1024 * 1024))
        except urllib.error.HTTPError as error:
            try: message = json.loads(error.read(4096)).get('error', 'Shared service rejected the request.')
            except (ValueError, AttributeError): message = 'Shared service rejected the request.'
            raise RuntimeError(message) from None
    def snapshot(self): return self.request('/v1/snapshot')
    def play(self, session_id, allow_disconnect=False):
        result = self.request('/v1/actions', {'action': 'play', 'session': session_id, 'allow_disconnect': allow_disconnect})
        deadline = time.monotonic() + 420
        while time.monotonic() < deadline:
            job = self.request('/v1/jobs/' + result['job'])
            if job['state'] == 'complete': return job
            if job['state'] == 'failed': raise RuntimeError(job['error'])
            time.sleep(2)
        raise TimeoutError('Server operation is still running. Check the Home screen before trying again.')


def default_client():
    if load_local().get('reis'): return ReisClient()
    if load_local()['service_url']: return RemoteClient()
    if CONFIG_PATH.exists(): return LocalClient()
    return None


REIS_FIELDS = ('panel_url', 'server_id', 'game_host', 'game_port')


def reis_config(invite):
    from core import DEFAULTS
    if invite.get('type') != 'reis-direct' or invite.get('version') != 1:
        raise ValueError('Unsupported Reis connection file.')
    config = dict(DEFAULTS)
    config.update({key: invite[key] for key in REIS_FIELDS})
    parsed = urlparse(config['panel_url'])
    if parsed.scheme != 'https' or parsed.path not in ('', '/'):
        raise ValueError('Use the HTTPS address of your Reis panel.')
    config['panel_url'] = validate_url(config['panel_url'])
    sessions.name(config['server_id'])
    config['game_port'] = int(config['game_port'])
    if not 1 <= config['game_port'] <= 65535: raise ValueError('Invalid game port.')
    if not isinstance(config['game_host'], str) or not config['game_host'] or any(c.isspace() for c in config['game_host']):
        raise ValueError('Invalid game address.')
    return config


def reis_key_name(config):
    return config['panel_url'].rstrip('/') + '/' + config['server_id']


def connect_reis(invite, token):
    import keyring
    config = reis_config(invite)
    if not isinstance(token, str) or not token.strip(): raise ValueError('Enter your own panel API key.')
    # Verify both server access and required file permissions before saving.
    ReisClient(config, token).snapshot()
    keyring.set_password('Build Explore Control Reis', reis_key_name(config), token)
    local = load_local(); local['reis'] = {key: config[key] for key in REIS_FIELDS}
    local['service_url'] = ''; save_local(local)


def export_reis_connection(manager):
    export_catalog(manager)
    catalog = json.loads(CATALOG.read_text())
    # Publish requirements only; never owner paths, profile GUIDs, or credentials.
    with manager.sftp() as sftp:
        sessions.write(sftp, sessions.ROOT + '/catalog.json', catalog)
    return dict(type='reis-direct', version=1, **{key: manager.config[key] for key in REIS_FIELDS})


class ReisClient:
    """Direct client with read and start access; Reis enforces user permissions."""
    role = 'player'

    def __init__(self, config=None, token=None):
        import keyring
        self.config = config or reis_config(dict(type='reis-direct', version=1, **load_local()['reis']))
        token = token or keyring.get_password('Build Explore Control Reis', reis_key_name(self.config))
        if not token: raise ValueError('Connect using your own Reis API key.')
        self.manager = Manager(self.config, api_key=token)

    def read(self, filename):
        return self.manager.api('/files/contents?file=' + quote('/' + sessions.ROOT + '/' + filename, safe=''))

    def snapshot(self):
        listing = self.manager.api('/files/list?directory=' + quote('/' + sessions.ROOT, safe=''))
        names = {item['attributes']['name'] for item in listing['data']}
        recovery = bool({'lock', 'pending.json', 'pending.json.previous'} & names)
        operation = 'operation' in names
        state = self.read('state.json' if 'state.json' in names else 'state.json.previous')
        catalog = self.read('catalog.json' if 'catalog.json' in names else 'catalog.json.previous')
        status = self.manager.status()
        # A second listing/state read rejects a switch that overlapped this snapshot.
        after = self.manager.api('/files/list?directory=' + quote('/' + sessions.ROOT, safe=''))
        after_names = {item['attributes']['name'] for item in after['data']}
        again = self.read('state.json' if 'state.json' in after_names else 'state.json.previous')
        operation = operation or 'operation' in after_names or state != again
        recovery = recovery or bool({'lock', 'pending.json', 'pending.json.previous'} & after_names)
        packs = {key: dict(catalog.get(key, {}), label=value['label']) for key, value in state['sessions'].items()}
        return {'active': state.get('active'), 'sessions': packs, 'server': status,
                'players': player_info(self.config['game_host'], self.config['game_port']) if status['state'] == 'running' else {'online': 0, 'names': []},
                'recovery_required': recovery, 'role': self.role, 'can_start_active': True,
                'address': f"{self.config['game_host']}:{self.config['game_port']}",
                'operation': {'by': 'Another client'} if operation else None}

    def play(self, session_id, allow_disconnect=False):
        state = self.snapshot()
        if state['operation'] or state['recovery_required']: raise ValueError('A session operation is in progress. Refresh before joining.')
        if session_id != state['active']:
            raise ValueError('Player access can join or start only the active session. Ask the owner to switch it.')
        if state['server']['state'] == 'offline':
            # Recheck immediately before the only permitted mutation. Reis enforces
            # control.start; no stop, restart, console, or file-write permission.
            fresh = self.snapshot()
            if fresh['active'] != session_id or fresh['operation'] or fresh['recovery_required']:
                raise ValueError('The active session changed. Refresh before starting.')
            if fresh['server']['state'] == 'offline': self.manager.api('/power', {'signal': 'start'})
            self.manager.wait_state('running')
            ready = self.snapshot()
            if ready['active'] != session_id or ready['operation'] or ready['recovery_required']:
                raise ValueError('The session changed while starting. Refresh before joining.')
        elif state['server']['state'] != 'running':
            raise ValueError('The server is starting or stopping. Wait and refresh before joining.')
        return {'state': 'complete'}
