"""Launch installed client profiles through CurseForge's own shortcut protocol."""
import json
import os
from pathlib import Path
from urllib.parse import urlencode, quote
import sqlite3
from contextlib import closing
from uuid import UUID

import sessions


def modrinth_profiles(root=None):
    root = Path(root or Path(os.environ.get('APPDATA', str(Path.home()))) / 'ModrinthApp').resolve()
    database = root / 'app.db'
    if not database.is_file(): return []
    try:
        # Select only public instance metadata. Never read account/auth tables.
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2)) as db:
            rows = db.execute('SELECT id, path, name FROM instances').fetchall()
    except sqlite3.Error:
        return []
    result = []
    for instance_id, relative, name in rows:
        if not isinstance(relative, str) or not relative or Path(relative).is_absolute(): continue
        folder = (root / 'instances' / relative).resolve()
        if root not in folder.parents: continue
        if not (folder / 'mods').is_dir(): continue
        result.append({'profile': name, 'client_path': str(folder),
                       'uri': 'modrinth://launch/instance/' + quote(str(instance_id), safe=''), 'launcher': 'modrinth'})
    return result


def profile_info(client_path, launcher='curseforge', modrinth_root=None):
    if not client_path:
        raise ValueError('This session has no matching client profile configured.')
    folder = Path(client_path).expanduser().resolve()
    if launcher == 'modrinth':
        roots = [modrinth_root] if modrinth_root else []
        roots.extend([folder.parent.parent, None])
        for root in roots:
            for info in modrinth_profiles(root):
                if Path(info['client_path']) == folder: return info
        raise ValueError('Import this pack into a current Modrinth App, then select its registered instance folder.')
    if launcher != 'curseforge': raise ValueError('Choose CurseForge or Modrinth.')
    metadata = folder / 'minecraftinstance.json'
    if not metadata.is_file() or not (folder / 'mods').is_dir():
        raise ValueError('The matching CurseForge client profile is missing: ' + str(folder))
    data = json.loads(metadata.read_text(encoding='utf-8-sig'))
    try:
        guid = str(UUID(str(data.get('guid', ''))))
    except ValueError:
        raise ValueError('The client profile has no valid CurseForge instance ID.') from None
    return {'profile': data.get('name') or folder.name, 'client_path': str(folder),
            'uri': 'curseforge://launch-game?' + urlencode({'instanceId': guid, 'gameId': 432})}


def request_launch(info, manager, opener=None):
    (opener or os.startfile)(info['uri'])
    return {'launch_requested': True, 'profile': info['profile'],
            'server_address': f"{manager.config['game_host']}:{manager.config['game_port']}",
            'note': 'Your selected launcher opens the matching modpack and handles Microsoft sign-in if needed. Join through Multiplayer.'}


def session_profile(state, session_id):
    import shared
    local = shared.load_local()
    if local.get('launcher', 'curseforge') == 'curseforge':
        return profile_info(state['sessions'][session_id].get('client_path'))
    requirements = shared.public_catalog(state)[session_id]
    folder = shared.find_profile(requirements, mapped=local['profiles'].get(session_id), verify=True)
    return profile_info(folder, launcher='modrinth', modrinth_root=local.get('modrinth_root'))


def launch_active(manager, opener=None):
    state = sessions.inventory(manager)
    if state.get('recovery_required'):
        raise RuntimeError('Recover the interrupted session operation before launching Minecraft.')
    active = state.get('active')
    if not active or active not in state['sessions']:
        raise ValueError('Register or load a server session first.')
    if manager.status()['state'] != 'running':
        raise RuntimeError('Start the active server first, or select a session and use Load & play.')
    info = session_profile(state, active)
    return request_launch(info, manager, opener)


def play_session(manager, session_id, opener=None):
    state = sessions.inventory(manager)
    if state.get('recovery_required'):
        raise RuntimeError('Recover the interrupted session operation first.')
    if session_id not in state['sessions']:
        raise ValueError('Unknown server session.')
    # Validate the local client before stopping or changing the server.
    info = session_profile(state, session_id)
    result = sessions.switch(manager, session_id, start=True)
    try:
        result['minecraft'] = request_launch(info, manager, opener)
    except OSError as error:
        raise RuntimeError('The server session is running, but CurseForge could not open. '
                           'Launch the matching profile in CurseForge. ' + str(error)) from None
    return result
