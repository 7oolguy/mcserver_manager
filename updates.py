"""Signed release checks. Never run an unverified downloaded installer."""
import base64
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlparse
import urllib.request

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from core import APP_HOME
from shared import VERSION


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d+\.\d+\.\d+', value): raise ValueError('Invalid release version.')
    return tuple(int(part) for part in value.split('.'))


def canonical(release): return json.dumps(release, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def release_config():
    root = Path(getattr(sys, '_MEIPASS', Path(__file__).parent))
    path = root / 'release-config.json'
    return json.loads(path.read_text()) if path.exists() else {'manifest_url': '', 'public_key': ''}


def verify_manifest(envelope, public_key, current=VERSION):
    release = envelope['release']
    Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key, validate=True)).verify(
        base64.b64decode(envelope['signature'], validate=True), canonical(release))
    version_tuple(release['version'])
    if not re.fullmatch(r'[0-9a-f]{64}', release['sha256']): raise ValueError('Invalid release checksum.')
    if not isinstance(release['size'], int) or not 0 < release['size'] < 250 * 1024 * 1024: raise ValueError('Invalid installer size.')
    url = urlparse(release['url'])
    if url.scheme != 'https' or not url.hostname or url.username or url.password: raise ValueError('Installer must use HTTPS.')
    if release.get('format') != 'msi': raise ValueError('Unsupported installer format.')
    return release if version_tuple(release['version']) > version_tuple(current) else None


def check():
    config = release_config()
    if not config.get('manifest_url') or not config.get('public_key'):
        return {'state': 'not_configured', 'message': 'Automatic updates will be enabled after the first signed release is published.'}
    url = urlparse(config['manifest_url'])
    if url.scheme != 'https': raise ValueError('Update checks require HTTPS.')
    with urllib.request.urlopen(config['manifest_url'], timeout=15) as response:
        raw = response.read(65537)
    if len(raw) > 65536: raise ValueError('Release manifest too large.')
    release = verify_manifest(json.loads(raw), config['public_key'])
    return {'state': 'available' if release else 'current', 'release': release}


def download(release):
    # Caller must obtain this from check(), never from an untrusted invitation.
    target_dir = APP_HOME / 'updates'; target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / ('BuildExploreControl-' + release['version'] + '.msi')
    partial = target.with_suffix('.partial'); digest = hashlib.sha256(); size = 0
    try:
        with urllib.request.urlopen(release['url'], timeout=60) as source, partial.open('wb') as dest:
            while chunk := source.read(1024 * 1024):
                size += len(chunk)
                if size > release['size']: raise ValueError('Installer exceeds signed size.')
                digest.update(chunk); dest.write(chunk)
        if size != release['size'] or digest.hexdigest() != release['sha256']:
            raise ValueError('Installer verification failed. The installed app was not changed.')
        partial.replace(target)
    finally:
        if partial.exists(): partial.unlink()
    return target


def install(path, release):
    path = Path(path).resolve()
    expected = (APP_HOME / 'updates' / ('BuildExploreControl-' + release['version'] + '.msi')).resolve()
    if path != expected: raise ValueError('Invalid staged update path.')
    with path.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    if digest != release['sha256'] or path.stat().st_size != release['size']: raise ValueError('Staged update was changed.')
    return subprocess.Popen(['msiexec.exe', '/i', str(path), '/passive', '/norestart'], shell=False,
                            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
