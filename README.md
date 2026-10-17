# Build Explore Control

A Windows app for playing Minecraft modpacks on a shared server. Choose a pack,
click **Play Minecraft**, and launch the matching installed CurseForge profile.
Worlds and progress stay with their server session.

## Install and play without extra hosting

Install the Windows MSI from Releases. Python is bundled. Each PC runs its own app;
Reis stores the shared server sessions and GitHub supplies signed app updates.
No Oracle account, VPS, or always-on owner computer is needed.

1. Owner: choose Connection / Create a friend invitation in the app. This publishes
   client pack requirements to `_control_sessions/catalog.json` without restarting
   Minecraft. The connection file contains addresses only, never credentials.
2. In Reis / Users, add your friend's own email with `file.read` and
   `file.read-content`, and `control.start` only. Grant no stop, restart, write,
   console, SFTP, backup, user,
   or settings permissions. Read access covers ALL server files, including configs,
   logs, and worlds; it cannot be restricted to app metadata. Grant it only to a
   trusted friend. Never share your own API key or SFTP password.
3. Your friend creates their OWN API key in Reis Account / API credentials. They
   open your `.becinvite` file in the app, verify the panel address, and enter that
   key. Access is verified before saving it in Windows Credential Manager.
4. Install the matching modpack in CurseForge, then click Play Minecraft. The app
   finds and verifies the local profile. Join through Multiplayer after loading.

The friend sees active sessions, pack requirements, server status, and online players.
The app refreshes every five seconds and disables Play when information is stale,
a switch is in progress, or recovery is needed. Friends can join or start the active
pack; switching sessions remains an owner operation.
The existing server lock protects owner switches. The app checks this lock before
starting, but Reis does not provide an atomic start-if-session-unchanged operation.
Avoid starting from another app or the panel while the owner is switching packs.
No separate service is required.

Minecraft login, preferences, local paths, and CurseForge profile IDs stay on each
PC. CurseForge handles Microsoft sign-in. Regenerate the connection file after
changing client pack requirements to refresh the shared catalogue. Revoke a
friend's server access in Reis / Users; they can revoke their own API key too.

## Server sessions and backups

Every server session preserves the complete runtime, mods, scripts, configs, all
worlds, player data, quests, and claims. Switching confirms a flush save, gracefully
stops the current server, moves its files into session storage, and restores the
selected session. Interrupted operations block unsafe starts and file edits.

Sessions share one hosting account, so they are not independent backups. Use
**Back up entire active session** under Advanced and keep copies outside the host.
Panel Java image, startup command, memory allocation, and ports remain shared host
settings. Use compatible settings for the packs you configure.

## Automatic updates

The app checks the repository’s latest release. It accepts update metadata only
when its Ed25519 signature matches the publisher key pinned in the app. Installer
size and SHA-256 are verified before installation and checked again immediately
before launching Windows Installer. Updates wait while an app/server operation is
active. Settings, credentials, and worlds are outside the installation payload.

The Updates tab lets you disable automatic installation or manually check/install
updates. Failed checks or invalid signatures leave the installed app unchanged.
MSI upgrades use the Windows Installer transaction and retain the previous package
in its installer cache for rollback/repair. There is no separate in-app downgrade UI.

Ed25519 release signing authenticates updates. It is separate from Windows
Authenticode signing. The initial MSI is not Authenticode-signed; devices enforcing
trusted-publisher installation policies may require an organization-approved code
signing certificate. Do not disable Windows protections to install it.

## Optional legacy shared service

The earlier HTTPS service remains available for existing installations. Direct
Reis syncing is the default and needs no extra host. The legacy service's isolated
invitation roles need separate hosting; see DEPLOYMENT.md if intentionally using it.

## Develop and release

Windows and Python 3.12 (64-bit) are required to build the native MSI. Python 3.12 is
pinned because its native `msilib` module is not available in Python 3.13+.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
.\.venv\Scripts\python.exe app.py
.\.venv\Scripts\python.exe build_release.py --output dist/v2
```

The build bundles the existing Python runtime, Tcl/Tk, dependencies, and only the
application source. It does not package settings, accounts, invitations, worlds,
logs, hosting credentials, or the release signing key. Python’s and dependencies’
license files are retained with the runtime.

Pushes and pull requests run the Windows checks and produce an installer artifact.
A tag matching `shared.VERSION`, such as `v2.0.0`, publishes an MSI and signed
`release.json` after all checks pass. `RELEASE_SIGNING_KEY` must be configured as a
GitHub Actions secret; it is never committed. The corresponding public key is in
`release-config.json`. Keep a secure backup of the signing key before changing CI.

CLI compatibility remains available: `app.py status`, `sessions`,
`session-switch ID --start` (server only), `launch-minecraft`, `play-session ID`,
`session-backup`, and `session-recover`. `app.py --advanced` opens the administration
interface. Run `service.py --help` for invitation and service commands.
