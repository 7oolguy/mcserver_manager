"""Build a self-contained Windows app and MSI using Python 3.12's native MSI API."""
import argparse
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import uuid

from shared import VERSION

ROOT = Path(__file__).parent.resolve()
NAMESPACE = uuid.UUID('b50cb348-22f2-4c2c-a586-cb7ad8f91c17')
UPGRADE_CODE = '{4A6D7754-071A-49E1-9688-B32F5A360966}'
APP_FILES = ('app.py', 'core.py', 'sessions.py', 'launcher.py', 'shared.py', 'service.py', 'desktop.py', 'updates.py')


def identity(value): return '{' + str(uuid.uuid5(NAMESPACE, value)).upper() + '}'


def copy_runtime(destination):
    base = Path(sys.base_prefix)
    destination.mkdir(parents=True)
    for pattern in ('python*.exe', 'python*.dll', 'vcruntime*.dll', 'LICENSE*'):
        for path in base.glob(pattern): shutil.copy2(path, destination / path.name)
    for folder in ('DLLs', 'tcl'):
        shutil.copytree(base / folder, destination / folder,
            ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '*_test*'))
    shutil.copytree(base / 'Lib', destination / 'Lib',
        ignore=shutil.ignore_patterns('site-packages', '__pycache__', '*.pyc', 'test', 'tests', 'idlelib', 'ensurepip', 'venv'))
    packages = Path(sys.prefix) / 'Lib/site-packages'
    shutil.copytree(packages, destination / 'Lib/site-packages', ignore=shutil.ignore_patterns(
        'pip', 'pip-*', 'setuptools', 'setuptools-*', 'PyInstaller', 'pyinstaller-*', '_pyinstaller_hooks_contrib',
        'pyinstaller_hooks_contrib-*', '__pycache__', '*.pyc', 'tests', 'test'))
    (destination / 'python312._pth').write_text('Lib\nDLLs\nLib/site-packages\n../app\nimport site\n')


def build_msi(payload, output):
    import msilib
    from msilib import schema, sequence
    db = msilib.init_database(str(output), schema, 'Build Explore Control', identity('product-' + VERSION), VERSION, '7oolguy')
    msilib.add_tables(db, sequence)
    summary = db.GetSummaryInformation(20)
    summary.SetProperty(msilib.PID_TEMPLATE, 'x64;1033'); summary.SetProperty(msilib.PID_PAGECOUNT, 200)
    summary.SetProperty(msilib.PID_WORDCOUNT, 10); summary.Persist()
    msilib.add_data(db, 'Property', [('UpgradeCode', UPGRADE_CODE), ('INSTALLLEVEL', '1'),
        ('ARPCONTACT', 'https://github.com/7oolguy/mcserver_manager'), ('ARPNOMODIFY', '1'),
        ('ARPURLINFOABOUT', 'https://github.com/7oolguy/mcserver_manager'),
        ('SecureCustomProperties', 'OLDPRODUCTS;NEWERPRODUCTFOUND'), ('REINSTALLMODE', 'amus')])
    msilib.add_data(db, 'Upgrade', [(UPGRADE_CODE, None, VERSION, None, 1, None, 'OLDPRODUCTS'),
                                   (UPGRADE_CODE, VERSION, None, None, 2, None, 'NEWERPRODUCTFOUND')])
    msilib.add_data(db, 'LaunchCondition', [('VersionNT64', 'This app requires 64-bit Windows.'),
        ('NOT NEWERPRODUCTFOUND', 'A newer version is already installed.')])
    view = db.OpenView("UPDATE `InstallExecuteSequence` SET `Sequence`=6550 WHERE `Action`='RemoveExistingProducts'"); view.Execute(None); view.Close()
    msilib.add_data(db, 'InstallExecuteSequence', [('InstallExecute', None, 6500)])
    cab = msilib.CAB('payload')
    target = msilib.Directory(db, cab, None, str(payload), 'TARGETDIR', 'SourceDir')
    local = msilib.Directory(db, cab, target, str(payload), 'LocalAppDataFolder', '.')
    programs = msilib.Directory(db, cab, local, str(payload), 'ProgramsDir', 'Programs')
    appdir = msilib.Directory(db, cab, programs, str(payload), 'INSTALLDIR', 'BEC|Build Explore Control')
    msilib.add_data(db, 'Directory', [('DesktopFolder', 'TARGETDIR', '.'), ('ProgramMenuFolder', 'TARGETDIR', '.'),
                                    ('MenuFolder', 'ProgramMenuFolder', 'BEC|Build Explore Control')])
    feature = msilib.Feature(db, 'Main', 'Build Explore Control', 'Minecraft sessions and client launcher', 1, directory='INSTALLDIR'); feature.set_current()
    directories = {Path('.'): appdir}; file_keys = {}; components = {}
    for path in sorted(payload.rglob('*')):
        relative = path.relative_to(payload)
        if path.is_dir():
            parent = directories[relative.parent]
            directories[relative] = msilib.Directory(db, cab, parent, str(path), 'D_' + hashlib.sha256(str(relative).encode()).hexdigest()[:24], path.name)
        else:
            directory = directories[relative.parent]
            component = 'C_' + hashlib.sha256(relative.as_posix().encode()).hexdigest()[:24]
            directory.start_component(component, feature, flags=0, keyfile=path.name, uuid=identity(relative.as_posix()))
            file_keys[relative.as_posix()] = directory.add_file(path.name); components[relative.as_posix()] = component
    pyw = file_keys['runtime/pythonw.exe']; component = components['runtime/pythonw.exe']
    msilib.add_data(db, 'Shortcut', [
        ('DesktopShortcut', 'DesktopFolder', 'BEC|Build Explore Control', component, '[#' + pyw + ']', '-B "[INSTALLDIR]app\\app.py"', 'Play Minecraft with your friends', None, None, None, 1, 'INSTALLDIR'),
        ('MenuShortcut', 'MenuFolder', 'BEC|Build Explore Control', component, '[#' + pyw + ']', '-B "[INSTALLDIR]app\\app.py"', 'Play Minecraft with your friends', None, None, None, 1, 'INSTALLDIR')])
    msilib.add_data(db, 'RemoveFile', [('RemoveMenuFolder', component, None, 'MenuFolder', 2)])
    cab.commit(db); db.Commit(); db.Close()
    return len(file_keys)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', default='dist/v2'); args = parser.parse_args()
    output = Path(args.output).resolve(); output.mkdir(parents=True, exist_ok=True)
    payload = output / 'BuildExploreControl'
    if payload.exists(): raise ValueError('Use a fresh build output directory; previous artifacts are preserved.')
    (payload / 'app').mkdir(parents=True)
    for name in APP_FILES: shutil.copy2(ROOT / name, payload / 'app' / name)
    config = json.loads((ROOT / 'release-config.json').read_text())
    public = os.environ.get('RELEASE_PUBLIC_KEY', config.get('public_key', ''))
    manifest_url = os.environ.get('RELEASE_MANIFEST_URL', config.get('manifest_url', ''))
    (payload / 'app/release-config.json').write_text(json.dumps({'public_key': public, 'manifest_url': manifest_url}, indent=2))
    copy_runtime(payload / 'runtime')
    installer = output / ('BuildExploreControl-' + VERSION + '.msi')
    count = build_msi(payload, installer)
    print(json.dumps({'installer': str(installer), 'files': count, 'bytes': installer.stat().st_size,
                      'sha256': hashlib.sha256(installer.read_bytes()).hexdigest()}, indent=2))


if __name__ == '__main__': main()
