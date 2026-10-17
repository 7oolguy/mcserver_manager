# Direct Reis setup (recommended)

No additional hosting is needed. Follow the direct connection steps in README.md.
The owner creates a connection file in the app and adds a separate read-only Reis
subuser. Never share owner credentials. Read permission covers all server files.
Switching remains owner-only. The instructions below are for the OPTIONAL legacy
hosted service, which is not needed for direct syncing.

# Owner deployment

Do not send your hosting panel API key, SFTP password, settings, service database,
or signing key to other players or commit them to GitHub.

## Prepare the catalogue

On the owner’s existing computer, run:

```powershell
python service.py catalog
```

This reads the configured server sessions and owner client profiles and writes a
public requirements catalogue to `%APPDATA%\Build Explore Control\shared-catalog.json`.
It contains pack labels, mod filenames/sizes/hashes, and installation URLs. It does
not include local profile paths or account identifiers. Regenerate it after an
intentional pack/server update so the catalogue describes the deployed pack.

## Choose hosting

Use a small persistent VPS/container host with outbound access to your Minecraft
hosting panel and SFTP endpoint. Give it a domain. The app requires HTTPS for remote
connections. Do not try to run this service inside the Minecraft process/container:
it must remain available when Minecraft is stopped.

The included Compose configuration exposes only Caddy on ports 80/443. The service
port is private inside Docker. Caddy obtains a certificate for the chosen domain.
Set DNS before starting it. No hosting is purchased or deployed automatically.

1. Copy `service.env.example` to `service.env` on the host and fill the owner’s
   panel/server/SFTP values. Use the verified SFTP host fingerprint.
2. Set `BEC_DOMAIN` for Compose, for example `play-control.example.com`.
3. Start `docker compose up -d --build`.
4. Copy `shared-catalog.json` into the service volume at
   `/data/Build Explore Control/shared-catalog.json`. Keep this volume backed up.
5. Create invitations using the service’s CLI **inside this deployed service**, or
   transfer the owner-created service database securely before serving invitations.
   Tokens are valid only against the database that issued them.

```sh
docker compose exec app python service.py invite https://play-control.example.com Friend /data/friend.becinvite
docker compose cp app:/data/friend.becinvite ./friend.becinvite
```

Send the invitation file privately. Delete unnecessary delivery copies after it is
imported. The app stores its token in Windows Credential Manager. The service stores
only a token hash, expiry, role, and revocation state.

## Permissions and revocation

`player` is the default and can join only the currently running pack. Use
`--role operator` explicitly if that person may start/switch the server. Switching
while players may be online requires a confirmation. There is no arbitrary console
or file-management API for remote users.

```sh
docker compose exec app python service.py users
docker compose exec app python service.py revoke USER_ID
```

Jobs are serialized, and a server-side lock protects switches across the shared
service and owner app. An interrupted session transaction must be recovered by the
owner while offline. Do not use the hosting panel to start or alter files during a
session operation. Do not run multiple service replicas against separate copies of
the same database. A process restart marks in-flight API jobs failed and leaves the
server journal for inspection; it does not blindly repeat destructive work.

## Local development

`python service.py serve` binds only to `127.0.0.1:8765`. Loopback HTTP invitations
are supported for testing on the same computer. This is not a remote invitation
address and does not require opening a firewall port. Production must use HTTPS.

Existing owner settings and Credential Manager entries still work locally. For
deployment, the service uses `BEC_*` configuration/environment variables and owner
credentials from `BEC_API_KEY` / `BEC_SFTP_PASSWORD`. The desktop installer includes
neither of these credentials.
