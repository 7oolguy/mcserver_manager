"""Simple player-first desktop UI; hosting administration stays in Advanced."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import webbrowser

from core import CONFIG_PATH
import launcher
import shared
import updates


class Desktop:
    def __init__(self, root, client=None, start_workers=True):
        self.root = root; self.client = client
        self.events = queue.Queue(); self.busy = False; self.refreshing = False; self.closed = False
        self.snapshot = None; self.fresh_at = 0; self.ids = []; self.update = None
        root.title('Build Explore Control ' + shared.VERSION); root.geometry('820x800'); root.minsize(760, 740)
        style = ttk.Style(root); style.theme_use('clam')
        style.configure('.', font=('Segoe UI', 11)); style.configure('TButton', padding=(14, 9))
        style.configure('Title.TLabel', font=('Segoe UI', 25, 'bold')); style.configure('State.TLabel', font=('Segoe UI', 18, 'bold'))
        style.configure('Play.TButton', font=('Segoe UI', 14, 'bold'), padding=(24, 14))
        outer = ttk.Frame(root, padding=24); outer.pack(fill='both', expand=True)
        ttk.Label(outer, text='Let’s play Minecraft', style='Title.TLabel').pack(anchor='w')
        ttk.Label(outer, text='Your modpacks, worlds, and friends in one place.').pack(anchor='w', pady=(4, 16))
        notebook = ttk.Notebook(outer); notebook.pack(fill='both', expand=True)
        self.play_tab = ttk.Frame(notebook, padding=20); notebook.add(self.play_tab, text='Play')
        self.connection_tab = ttk.Frame(notebook, padding=20); notebook.add(self.connection_tab, text='Connection')
        update_tab = ttk.Frame(notebook, padding=20); notebook.add(update_tab, text='Updates')
        self.state = tk.StringVar(value='Connecting…'); self.details = tk.StringVar(value='Checking the server and shared sessions.')
        self.players = tk.StringVar(); self.message = tk.StringVar(value='')
        ttk.Label(self.play_tab, textvariable=self.state, style='State.TLabel').pack(anchor='w')
        ttk.Label(self.play_tab, textvariable=self.details, wraplength=690).pack(anchor='w', pady=6)
        ttk.Label(self.play_tab, textvariable=self.players, wraplength=690).pack(anchor='w', pady=(0, 20))
        ttk.Label(self.play_tab, text='Choose your modpack').pack(anchor='w')
        self.selection = ttk.Combobox(self.play_tab, state='readonly'); self.selection.pack(fill='x', pady=8)
        self.selection.bind('<<ComboboxSelected>>', lambda event: self.render_permissions())
        self.play_button = ttk.Button(self.play_tab, text='Play Minecraft', style='Play.TButton', command=self.play, state='disabled')
        self.play_button.pack(fill='x', pady=(8, 12))
        actions = ttk.Frame(self.play_tab); actions.pack(fill='x')
        ttk.Button(actions, text='Locate installed pack', command=self.locate).pack(side='left', padx=(0, 8))
        ttk.Button(actions, text='Get this modpack', command=self.get_pack).pack(side='left')
        ttk.Button(actions, text='Refresh', command=self.refresh).pack(side='right')
        ttk.Label(self.play_tab, textvariable=self.message, wraplength=680).pack(anchor='w', pady=12)
        setup_actions = ttk.Frame(self.play_tab); setup_actions.pack(fill='x')
        ttk.Button(setup_actions, text='Import friend setup ZIP…', command=self.import_setup).pack(side='left')
        ttk.Button(setup_actions, text='Finish launcher import', command=self.finish_import).pack(side='left', padx=8)
        self.setup_text = tk.StringVar()
        ttk.Label(self.play_tab, textvariable=self.setup_text, wraplength=680).pack(anchor='w', pady=6)
        ttk.Label(self.connection_tab, text='Connect directly to Reis', style='State.TLabel').pack(anchor='w')
        ttk.Label(self.connection_tab, text='Import the server config exported by the admin controller.\nServer details and sessions configure automatically; authenticate with your own Reis key once.', wraplength=680).pack(anchor='w', pady=12)
        ttk.Button(self.connection_tab, text='Import server config…', command=self.open_invite).pack(anchor='w')
        self.connection = tk.StringVar(value=(shared.load_local().get('reis') or {}).get('panel_url') or shared.load_local()['service_url'] or ('This computer · owner access' if CONFIG_PATH.exists() else 'Not connected yet'))
        ttk.Label(self.connection_tab, textvariable=self.connection, wraplength=680).pack(anchor='w', pady=12)
        ttk.Button(self.connection_tab, text='Choose CurseForge instances folder…', command=self.choose_root).pack(anchor='w', pady=8)
        ttk.Label(self.connection_tab, text='Minecraft launcher').pack(anchor='w')
        self.launcher_choice = ttk.Combobox(self.connection_tab, state='readonly', values=['CurseForge', 'Modrinth'])
        self.launcher_choice.set('Modrinth' if shared.load_local().get('launcher') == 'modrinth' else 'CurseForge')
        self.launcher_choice.pack(anchor='w', pady=4)
        self.launcher_choice.bind('<<ComboboxSelected>>', self.choose_launcher)
        ttk.Button(self.connection_tab, text='Choose Modrinth data folder…', command=self.choose_modrinth_root).pack(anchor='w', pady=4)
        if CONFIG_PATH.exists():
            ttk.Button(self.connection_tab, text='Export player configuration…', command=self.invite).pack(anchor='w', pady=8)
            ttk.Button(self.connection_tab, text='Export selected pack for a friend…', command=self.export_setup).pack(anchor='w', pady=4)
            ttk.Button(self.connection_tab, text='Advanced server controls', command=self.advanced).pack(anchor='w', pady=8)
        self.update_text = tk.StringVar(value='Version ' + shared.VERSION + '\nUpdates are installed when the app is idle. Settings and worlds are preserved.')
        ttk.Label(update_tab, textvariable=self.update_text, wraplength=680).pack(anchor='w', pady=12)
        self.auto_update = tk.BooleanVar(value=shared.load_local()['auto_update'])
        ttk.Checkbutton(update_tab, text='Keep this app up to date automatically', variable=self.auto_update, command=self.save_update_preference).pack(anchor='w')
        ttk.Button(update_tab, text='Check for updates', command=self.check_updates).pack(anchor='w', pady=12)
        self.install_button = ttk.Button(update_tab, text='Install verified update', command=self.install_update, state='disabled'); self.install_button.pack(anchor='w')
        self.progress = ttk.Progressbar(outer, mode='indeterminate'); self.progress.pack(fill='x', pady=(12, 4))
        ttk.Label(outer, text='Your selected launcher handles Minecraft sign-in. Join through Multiplayer after loading.', wraplength=720).pack(anchor='w')
        root.protocol('WM_DELETE_WINDOW', self.close)
        if start_workers:
            root.after(100, self.refresh); root.after(5000, self.periodic_refresh)
            root.after(200, self.poll); root.after(2500, self.check_updates)

    def run(self, function, success=None):
        if self.busy: return
        self.busy = True; self.progress.start(12); self.render_permissions()
        def worker():
            try: self.events.put(('done', function(), success))
            except Exception as error: self.events.put(('error', str(error), None))
        threading.Thread(target=worker, daemon=True).start()

    def poll(self):
        if self.closed: return
        try:
            while True:
                kind, value, callback = self.events.get_nowait()
                if kind == 'snapshot':
                    self.refreshing = False; self.snapshot = value; self.fresh_at = time.monotonic(); self.render()
                elif kind == 'offline':
                    self.refreshing = False; self.state.set('Connection unavailable'); self.details.set(value); self.fresh_at = 0; self.render_permissions()
                elif kind == 'update': self.update_ready(value)
                else:
                    self.busy = False; self.progress.stop()
                    if kind == 'error': self.message.set(value); messagebox.showerror('Could not complete this action', value)
                    elif callback: callback(value)
                    self.render_permissions()
        except queue.Empty: pass
        self.root.after(150, self.poll)

    def refresh(self):
        if self.closed: return
        if not self.refreshing:
            self.refreshing = True
            def worker():
                try:
                    if self.client is None: self.client = shared.default_client()
                    if self.client is None: raise ValueError('Open an invitation in Connection to get started.')
                    if isinstance(self.client, shared.LocalClient) and not shared.CATALOG.exists(): shared.export_catalog(self.client.manager)
                    if shared.load_local().get('pending_imports'):
                        import friendpack
                        friendpack.finish_pending()
                    self.events.put(('snapshot', self.client.snapshot(), None))
                except Exception as error: self.events.put(('offline', str(error), None))
            threading.Thread(target=worker, daemon=True).start()
    def periodic_refresh(self):
        if self.closed: return
        self.refresh(); self.root.after(5000, self.periodic_refresh)

    def selected(self):
        index = self.selection.current()
        return self.ids[index] if 0 <= index < len(self.ids) else None

    def render(self):
        data = self.snapshot; previous = self.selected(); self.ids = list(data['sessions'])
        self.selection['values'] = [data['sessions'][key]['label'] for key in self.ids]
        selected = previous if previous in self.ids else data.get('active')
        if selected in self.ids: self.selection.current(self.ids.index(selected))
        active = data['sessions'].get(data.get('active'), {}).get('label', 'No session selected')
        self.state.set('Server ' + data['server']['state'].replace('_', ' '))
        self.details.set(active + '\n' + data['address'])
        players = data.get('players', {}); count = players.get('online')
        self.players.set(('Players online: ' + str(count) if count is not None else 'Player count unavailable') + (' · ' + ', '.join(players.get('names', [])) if players.get('names') else ''))
        if data.get('operation'): self.message.set('A server operation is in progress. Everyone will see the result here.')
        self.setup_text.set('Finish the import in your launcher. The app will detect and verify it automatically.' if shared.load_local().get('pending_imports') else '')
        self.render_permissions()

    def render_permissions(self):
        allowed = bool(self.snapshot and self.selected() and self.fresh_at and time.monotonic() - self.fresh_at < 20 and not self.busy)
        if self.snapshot:
            data = self.snapshot
            allowed = allowed and not data.get('operation') and not data.get('recovery_required')
            allowed = allowed and self.selected() not in shared.load_local().get('pending_imports', {})
            if data['role'] == 'player':
                allowed = allowed and self.selected() == data['active'] and (data['server']['state'] == 'running' or (data.get('can_start_active') and data['server']['state'] == 'offline'))
        self.play_button.configure(state='normal' if allowed else 'disabled')

    def play(self):
        key = self.selected()
        if not key or not self.snapshot: return
        data = self.snapshot; item = data['sessions'][key]; config = shared.load_local()
        switching = key != data['active']
        confirm = False
        if switching and data['server']['state'] != 'offline':
            confirm = messagebox.askyesno('Switch modpack?', 'This saves and stops the current session. Anyone online will disconnect. Continue?')
            if not confirm: return
        self.message.set('Checking your installed modpack, then preparing the server…')
        def operation():
            folder = shared.find_profile(item, config['instances_root'], config['profiles'].get(key), verify=True)
            info = launcher.profile_info(str(folder), launcher=config.get('launcher', 'curseforge'), modrinth_root=config.get('modrinth_root'))
            self.client.play(key, allow_disconnect=confirm)
            manager = type('Connection', (), {'config': {'game_host': data['address'].rsplit(':', 1)[0], 'game_port': data['address'].rsplit(':', 1)[1]}})()
            return launcher.request_launch(info, manager)
        self.run(operation, lambda result: self.message.set('Minecraft is launching. Join ' + result['server_address'] + ' through Multiplayer.'))

    def locate(self):
        key = self.selected()
        if not key: return
        folder = filedialog.askdirectory(title='Choose this modpack’s installed profile folder')
        if not folder: return
        def operation():
            found = shared.find_profile(self.snapshot['sessions'][key], root=Path(folder).parent, mapped=folder, verify=True)
            if found.resolve() != Path(folder).resolve(): raise ValueError('That folder does not match this modpack. Select ' + str(found))
            config = shared.load_local(); config['profiles'][key] = folder; shared.save_local(config)
        self.run(operation, lambda result: self.message.set('Matching modpack found. You can play now.'))

    def get_pack(self):
        key = self.selected()
        if not key: return
        url = self.snapshot['sessions'][key].get('install_url', '')
        from urllib.parse import urlparse
        parsed = urlparse(url)
        if parsed.scheme == 'https' and parsed.hostname in ('www.curseforge.com', 'curseforge.com'): webbrowser.open(url)
        else: messagebox.showinfo('Get this modpack', 'Ask the owner for a friend setup ZIP, then use Import friend setup ZIP here.')

    def open_invite(self):
        filename = filedialog.askopenfilename(filetypes=[('Server configuration', '*.becconfig'), ('Older connection file', '*.becinvite'), ('JSON', '*.json')])
        if not filename: return
        try:
            path = Path(filename)
            if path.stat().st_size > 32768: raise ValueError('Invalid invitation file.')
            invitation = json.loads(path.read_text(encoding='utf-8-sig'))
            token = None
            if invitation.get('type') == 'reis-direct':
                config = shared.reis_config(invitation)
                import keyring
                token = keyring.get_password('Build Explore Control Reis', shared.reis_key_name(config))
                if not token:
                    token = simpledialog.askstring('Your Reis API key', 'Panel: ' + config['panel_url'] + '\nServer: ' + config['server_id'] + '\nVerify this address. Enter YOUR API key from this panel’s Account → API credentials.\nThe owner must grant your account read/start access first. This is saved securely for next time.', show='*')
                if not token: return
        except Exception as error:
            messagebox.showerror('Invalid connection file', str(error)); return
        def operation():
            if invitation.get('type') == 'reis-direct':
                shared.connect_reis(invitation, token); self.client = shared.ReisClient()
            else:
                shared.connect(invitation); self.client = shared.RemoteClient()
        self.run(operation, lambda result: (self.connection.set((shared.load_local().get('reis') or {}).get('panel_url') or shared.load_local()['service_url']), self.refresh()))

    def choose_root(self):
        folder = filedialog.askdirectory(title='Choose CurseForge’s Instances folder')
        if folder:
            config = shared.load_local(); config['instances_root'] = folder; shared.save_local(config)

    def choose_launcher(self, event=None):
        config = shared.load_local(); config['launcher'] = self.launcher_choice.get().lower(); shared.save_local(config)
        self.message.set('Using ' + self.launcher_choice.get() + '. Import or locate this pack in that launcher.')

    def choose_modrinth_root(self):
        folder = filedialog.askdirectory(title='Choose Modrinth data folder (contains app.db and instances)')
        if folder:
            config = shared.load_local(); config['modrinth_root'] = folder; shared.save_local(config)

    def export_setup(self):
        key = self.selected()
        if not key: return
        filename = filedialog.asksaveasfilename(defaultextension='.zip', initialfile=key + '-friend-setup.zip')
        if not filename: return
        def operation():
            import friendpack
            from core import Manager
            return friendpack.export(Manager(), key, filename)
        self.run(operation, lambda result: messagebox.showinfo('Friend setup ready', 'Send this ZIP to your friend. They choose their launcher, then use Play → Import friend setup ZIP.\nTheir own Reis API key and Minecraft login are still required.'))

    def import_setup(self):
        filename = filedialog.askopenfilename(filetypes=[('Friend setup ZIP', '*.zip'), ('Modrinth pack', '*.mrpack')])
        if not filename: return
        import friendpack
        def prepared(setup):
            config = shared.reis_config(setup['connection'])
            token = simpledialog.askstring('Connect your Reis account', 'Pack: ' + setup['requirements']['label'] + '\nPanel: ' + config['panel_url'] + '\nVerify this address, then enter YOUR Reis API key.\nThe owner must add you as a subuser first.', show='*')
            if not token: return
            def installed(result):
                self.client = shared.ReisClient(); self.connection.set(config['panel_url']); self.refresh()
                if result['ready']: self.message.set('Setup verified. This pack is ready to play when its server session is active.')
                else: self.finish_import()
            self.run(lambda: friendpack.import_pack(filename, token), installed)
        self.message.set('Checking the setup ZIP and its mod files…')
        self.run(lambda: friendpack.inspect(filename), prepared)

    def finish_import(self):
        pending = shared.load_local().get('pending_imports', {})
        if not pending:
            messagebox.showinfo('Launcher setup', 'No unfinished setup. Choose Import friend setup ZIP to add a pack.'); return
        item = pending.get(self.selected()) or next(reversed(pending.values()))
        path = item['zip']
        self.root.clipboard_clear(); self.root.clipboard_append(path)
        def operation():
            import friendpack
            friendpack.open_import(path)
        def opened(result):
            name = self.launcher_choice.get()
            text = ('In CurseForge, choose Minecraft → Import → Choose ZIP and paste the copied file path.' if name == 'CurseForge' else 'Confirm the pack import in Modrinth. If needed, use Import from file and select the copied pack path.')
            self.message.set('Finish the import in ' + name + '. This app will detect the completed profile.')
            messagebox.showinfo('Finish setup in ' + name, text + '\n\n' + path + '\n\nYour launcher installs Minecraft and the loader and handles sign-in. Keep this app open; it verifies the imported files automatically.')
        self.run(operation, opened)

    def invite(self):
        filename = filedialog.asksaveasfilename(defaultextension='.becconfig', initialfile='Minecraft-server.becconfig')
        if not filename: return
        def operation():
            from core import Manager
            invite = shared.export_reis_connection(Manager())
            with Path(filename).open('x', encoding='utf-8') as stream: json.dump(invite, stream, indent=2)
        self.run(operation, lambda result: messagebox.showinfo('Connection file created', 'Send this file to your friend. It contains no passwords.\nIn Reis → Users, add their own account with file.read, file.read-content, and control.start only. They enter their own panel API key in the app.\nThey can start the active session, but cannot switch packs, stop, or restart. Read access covers ALL server files. Do not grant write, console, SFTP, or backup access.'))

    def advanced(self):
        entry = [] if getattr(sys, 'frozen', False) else [str(Path(__file__).with_name('app.py'))]
        subprocess.Popen([sys.executable, *entry, '--advanced'], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))

    def save_update_preference(self):
        config = shared.load_local(); config['auto_update'] = self.auto_update.get(); shared.save_local(config)

    def check_updates(self):
        def worker():
            try:
                result = updates.check()
                if result['state'] == 'available': result['path'] = str(updates.download(result['release']))
                self.events.put(('update', result, None))
            except Exception: self.events.put(('update', {'state': 'error'}, None))
        threading.Thread(target=worker, daemon=True).start()

    def update_ready(self, result):
        if result['state'] == 'available':
            self.update = result; self.update_text.set('A verified update is ready: ' + result['release']['version'])
            self.install_button.configure(state='normal')
            if self.auto_update.get() and (getattr(sys, 'frozen', False) or shared.installed_app()): self.root.after(1000, self.install_update)
        elif result['state'] == 'not_configured': self.update_text.set('Version ' + shared.VERSION + '\n' + result['message'])
        elif result['state'] == 'current': self.update_text.set('You have the latest verified release: ' + shared.VERSION)
        else: self.update_text.set('Could not check updates. Your current app is unchanged; try again later.')

    def install_update(self):
        if not self.update: return
        if self.busy or (self.snapshot and self.snapshot.get('operation')):
            self.root.after(5000, self.install_update); return
        try:
            updates.install(self.update['path'], self.update['release']); self.closed = True; self.root.destroy()
        except Exception as error: messagebox.showerror('Update not installed', str(error))

    def close(self):
        if self.busy: messagebox.showinfo('Working', 'Please let the current action finish before closing.'); return
        self.closed = True; self.root.destroy()


def gui():
    root = tk.Tk(); Desktop(root); root.mainloop()
