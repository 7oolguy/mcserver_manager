# Build Explore Control

A Windows app for playing Minecraft modpacks on a shared server. Choose a pack,
click **Play Minecraft**, and launch the matching installed CurseForge profile.
Worlds and progress stay with their server session.

## Install and play

1. Download the `.msi` from [Releases](https://github.com/7oolguy/mcserver_manager/releases).
2. Install it for your Windows account. Python is bundled; no separate Python setup
   is required. Desktop and Start menu shortcuts are included.
3. Open the app, select **Connection → Open invitation**, and choose the private
   `.becinvite` file supplied by the server owner.
4. Install the requested modpack in CurseForge. The app detects matching profiles;
   use **Locate installed pack** if your instances are in another folder.
5. Click **Play Minecraft**, then join the displayed address through Multiplayer.

CurseForge handles Microsoft sign-in. Enable **Use CurseForge launcher** there if
prompted. This app does not copy Minecraft accounts or launch tokens.

The owner can use **Advanced server controls**. Player invitations can view the
shared server/session information and join the running pack. They cannot switch
packs, stop the server, edit files, or issue console commands. Operator access can
be granted explicitly through the service CLI.

## What is shared

The service synchronizes active session, session catalogue, required mod files,
server status, online player information when available, and active operations.
Clients refresh every five seconds and disable Play if the connection becomes
stale. A server-side operation lock prevents competing session switches.

Installation paths, CurseForge profile IDs, preferences, and Minecraft accounts
remain local. The shared service keeps hosting API/SFTP credentials; player apps
receive only a revocable invitation token stored in Windows Credential Manager.
Invitations expire after 30 days by default. Send invitation files privately.

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

## Host the shared service

The shared service is implemented but needs a persistent HTTPS host. GitHub hosts
source and installer releases; GitHub Pages/Actions do not run this private backend
continuously. See [DEPLOYMENT.md](DEPLOYMENT.md) for the owner setup and Docker/TLS
configuration. Do not distribute hosting credentials to friends.

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
