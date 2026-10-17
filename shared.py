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

from core import APP_HOME, CONFIG_PATH, Manager
import sessions
import launcher

VERSION = '2.0.0'
LOCAL_SETTINGS = APP_HOME / 'player-settings.json'
CATALOG = APP_HOME / 'shared-catalog.json'


def installed_app():
    return Path(sys.executable).resolve().parent == Path(__file__).resolve().parent.parent / 'runtime'


def load_local():
    defaults = {'service_url': '', 'profiles': {}, 'instances_root': str(Path.home() / 'curseforge/minecraft/Instances'),
                'auto_update': True, 'update_url': ''}
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
    url = validate_url(invite['service_url'])
    token = invite.get('token', '')
    if not isinstance(token, str) or len(token) < 32: raise ValueError('Invalid invitation.')
    # Verify the invitation before replacing a working connection.
    RemoteClient(url, token).request('/v1/me')
    import keyring
    keyring.set_password('Build Explore Control Shared', url, token)
    config = load_local(); config['service_url'] = url; save_local(config)


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
    root = Path(root or load_local()['instances_root'])
    candidates = [Path(mapped)] if mapped else []
    if root.is_dir(): candidates.extend(p for p in root.iterdir() if p.is_dir() and p not in candidates)
    for folder in candidates:
        if not (folder / 'minecraftinstance.json').is_file(): continue
        found = {p.name: p.stat().st_size for p in (folder / 'mods').glob('*.jar')}
        if found != expected_sizes: continue
        if verify and jar_manifest(folder) != expected: continue
        launcher.profile_info(str(folder))  # Validate the local GUID, never the owner's GUID.
        return folder
    raise ValueError('Install the matching modpack in CurseForge, then locate its folder here.')


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
    if load_local()['service_url']: return RemoteClient()
    if CONFIG_PATH.exists(): return LocalClient()
    return None
