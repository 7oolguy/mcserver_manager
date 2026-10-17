"""Sign release metadata; private key comes from CI secrets or Credential Manager."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PrivateFormat, PublicFormat, NoEncryption
from shared import VERSION
from updates import canonical


def signing_key(create=False):
    value = os.environ.get('RELEASE_SIGNING_KEY')
    if value: return Ed25519PrivateKey.from_private_bytes(base64.b64decode(value, validate=True))
    import keyring
    value = keyring.get_password('Build Explore Control Releases', 'Ed25519 signing key')
    if not value and create:
        key = Ed25519PrivateKey.generate()
        value = base64.b64encode(key.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption())).decode()
        keyring.set_password('Build Explore Control Releases', 'Ed25519 signing key', value)
    if not value: raise ValueError('Configure the release signing key before publishing updates.')
    return Ed25519PrivateKey.from_private_bytes(base64.b64decode(value, validate=True))


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--init', action='store_true'); parser.add_argument('--installer'); parser.add_argument('--url'); parser.add_argument('--output', default='release.json'); args = parser.parse_args()
    key = signing_key(create=args.init)
    public = base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
    if args.init:
        print(json.dumps({'public_key': public})); return
    installer = Path(args.installer)
    with installer.open('rb') as stream: digest = hashlib.file_digest(stream, 'sha256').hexdigest()
    release = {'version': VERSION, 'url': args.url, 'size': installer.stat().st_size, 'sha256': digest, 'format': 'msi'}
    envelope = {'release': release, 'signature': base64.b64encode(key.sign(canonical(release))).decode()}
    Path(args.output).write_text(json.dumps(envelope, indent=2))


if __name__ == '__main__': main()
