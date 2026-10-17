"""Launch installed client profiles through CurseForge's own shortcut protocol."""
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from uuid import UUID

import sessions


def profile_info(client_path):
    if not client_path:
        raise ValueError('This session has no matching client profile configured.')
    folder = Path(client_path).expanduser().resolve()
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
            'note': 'CurseForge launches the matching modpack and handles Microsoft sign-in if needed. Join through Multiplayer.'}


def launch_active(manager, opener=None):
    state = sessions.inventory(manager)
    if state.get('recovery_required'):
        raise RuntimeError('Recover the interrupted session operation before launching Minecraft.')
    active = state.get('active')
    if not active or active not in state['sessions']:
        raise ValueError('Register or load a server session first.')
    if manager.status()['state'] != 'running':
        raise RuntimeError('Start the active server first, or select a session and use Load & play.')
    info = profile_info(state['sessions'][active].get('client_path'))
    return request_launch(info, manager, opener)


def play_session(manager, session_id, opener=None):
    state = sessions.inventory(manager)
    if state.get('recovery_required'):
        raise RuntimeError('Recover the interrupted session operation first.')
    if session_id not in state['sessions']:
        raise ValueError('Unknown server session.')
    # Validate the local client before stopping or changing the server.
    info = profile_info(state['sessions'][session_id].get('client_path'))
    result = sessions.switch(manager, session_id, start=True)
    try:
        result['minecraft'] = request_launch(info, manager, opener)
    except OSError as error:
        raise RuntimeError('The server session is running, but CurseForge could not open. '
                           'Launch the matching profile in CurseForge. ' + str(error)) from None
    return result
