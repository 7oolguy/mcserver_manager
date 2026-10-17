import argparse
import json
import os
import queue
import sys
import threading
import time
import webbrowser
from collections import deque
from datetime import datetime
from pathlib import Path

from core import Manager, LogCursor, redact, secret, settings, save_settings, store_secret, CONFIG_PATH


def cli(argv):
    parser = argparse.ArgumentParser(description='Build Explore Control: desktop and agent server tool')
    parser.add_argument('--output', help='Write JSON result to this file')
    subs = parser.add_subparsers(dest='action', required=True)
    for name in ('status', 'ping', 'start', 'stop', 'restart', 'save', 'save-stop', 'backup', 'settings', 'world-info'):
        subs.add_parser(name)
    command = subs.add_parser('command'); command.add_argument('command')
    logs = subs.add_parser('logs'); logs.add_argument('--lines', type=int, default=150)
    listing = subs.add_parser('files'); listing.add_argument('path', nargs='?', default='.')
    read = subs.add_parser('read'); read.add_argument('path')
    download = subs.add_parser('download'); download.add_argument('remote'); download.add_argument('local')
    upload = subs.add_parser('upload'); upload.add_argument('local'); upload.add_argument('remote')
    disable = subs.add_parser('disable-mod'); disable.add_argument('name')
    world = subs.add_parser('new-world'); world.add_argument('name'); world.add_argument('seed')
    switch = subs.add_parser('switch-world'); switch.add_argument('name')
    export = subs.add_parser('export-pack'); export.add_argument('output_path')
    subs.add_parser('sessions')
    subs.add_parser('session-recover')
    subs.add_parser('session-backup')
    subs.add_parser('launch-minecraft')
    play = subs.add_parser('play-session'); play.add_argument('id')
    register = subs.add_parser('session-register'); register.add_argument('id'); register.add_argument('label')
    importing = subs.add_parser('session-import'); importing.add_argument('id'); importing.add_argument('label'); importing.add_argument('folder'); importing.add_argument('--client-path', default='')
    session_switch = subs.add_parser('session-switch'); session_switch.add_argument('id'); session_switch.add_argument('--start', action='store_true')
    args = parser.parse_args(argv)
    manager = Manager()
    try:
        action = args.action
        if action in ('launch-minecraft', 'play-session'):
            import launcher
            result = launcher.launch_active(manager) if action == 'launch-minecraft' else launcher.play_session(manager, args.id)
        elif action.startswith('session'):
            import sessions
            if action == 'sessions': result = sessions.inventory(manager)
            elif action == 'session-register': result = sessions.register(manager, args.id, args.label)
            elif action == 'session-import': result = sessions.import_folder(manager, args.id, args.label, args.folder, args.client_path)
            elif action == 'session-switch': result = sessions.switch(manager, args.id, args.start)
            elif action == 'session-recover': result = sessions.recover(manager)
            elif action == 'session-backup': result = sessions.backup(manager)
        elif action in ('start', 'stop', 'restart'):
            result = manager.power(action)
        elif action == 'save-stop': result = manager.save_stop()
        elif action == 'command': result = manager.command(args.command)
        elif action == 'logs': result = manager.logs(args.lines)
        elif action == 'files': result = manager.list_files(args.path)
        elif action == 'read': result = manager.read_file(args.path)
        elif action == 'download': result = manager.download(args.remote, args.local)
        elif action == 'upload': result = manager.upload(args.local, args.remote)
        elif action == 'disable-mod': result = manager.archive_mod(args.name)
        elif action == 'new-world': result = manager.new_world(args.name, args.seed)
        elif action == 'world-info': result = manager.world_info()
        elif action == 'switch-world': result = manager.switch_world(args.name)
        elif action == 'export-pack': result = manager.export_pack(args.output_path)
        elif action == 'backup': result = manager.backup_world()
        elif action == 'settings': result = settings()
        else: result = getattr(manager, action)()
        record, code = {'ok': True, 'result': result}, 0
    except Exception as exc:
        record, code = {'ok': False, 'error': str(exc)}, 1
    output = json.dumps(record, indent=2, ensure_ascii=False)
    if args.output:
        Path(args.output).write_text(output, encoding='utf-8')
    if sys.stdout is not None:
        print(output)
    return code


def gui():
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox, simpledialog
    from tkinter.scrolledtext import ScrolledText

    root = tk.Tk()
    from shared import VERSION
    root.title('Build Explore Control ' + VERSION + ' · Admin · ' + settings()['server_id'])
    root.geometry('1080x850')
    root.minsize(1000, 820)
    style = ttk.Style(root)
    style.theme_use('clam')
    style.configure('.', font=('Segoe UI', 10))
    style.configure('Header.TLabel', font=('Segoe UI', 21, 'bold'))
    style.configure('TButton', padding=(12, 7))
    style.configure('TNotebook.Tab', padding=(15, 8))
    outer = ttk.Frame(root, padding=18); outer.pack(fill='both', expand=True)
    ttk.Label(outer, text='Build Explore Control', style='Header.TLabel').pack(anchor='w')
    ttk.Label(outer, text='Your Minecraft server and modpack, in one place.').pack(anchor='w', pady=(2, 14))
    notebook = ttk.Notebook(outer); notebook.pack(fill='both', expand=True)
    events, busy = queue.Queue(), False
    status_pending = False
    stop_event, pause_logs, cancel_event = threading.Event(), threading.Event(), threading.Event()
    controls_by_name = {}
    latest_state = 'unknown'
    status_text = tk.StringVar(value='Connecting to your server…')
    operation_bar = ttk.Progressbar(outer, mode='indeterminate')
    operation_bar.pack(fill='x', pady=(8, 0))
    ttk.Label(outer, textvariable=status_text, wraplength=950).pack(anchor='w', pady=(12, 0))
    activity = []

    def manager():
        return Manager(progress=lambda text: events.put(('progress', text)), cancel=cancel_event)

    def run(label, operation, success=None):
        nonlocal busy
        if busy:
            messagebox.showinfo('Working', 'Please wait for the current operation to finish.'); return
        busy = True; cancel_event.clear(); status_text.set(label + '…'); operation_bar.start(15)
        cancel_button.configure(state='normal' if label in ('World backup', 'Export client pack', 'Download') else 'disabled')
        update_controls()
        def worker():
            try: events.put(('success', (operation(), success, label)))
            except Exception as exc: events.put(('error', (str(exc), label)))
        threading.Thread(target=worker, daemon=True).start()

    def poll():
        nonlocal busy, status_pending
        try:
            while True:
                kind, value = events.get_nowait()
                if kind == 'progress':
                    status_text.set(value)
                    if value.startswith('Server: '):
                        server_state.set(value.removeprefix('Server: ').upper())
                elif kind == 'live_status':
                    status_pending = False
                    render_status(value)
                elif kind == 'status_error':
                    status_pending = False
                    if not busy:
                        server_state.set('Status unavailable — check connection settings')
                    connection_text.set('Panel disconnected; retrying automatically')
                elif kind == 'log_chunk':
                    if value['reset']:
                        log_lines.clear()
                    if value['text']:
                        log_lines.extend(redact(value['text']).splitlines())
                        render_logs()
                    log_connection.set('Live • updated ' + datetime.now().strftime('%H:%M:%S'))
                elif kind == 'log_error':
                    log_connection.set('Disconnected • retrying: ' + value)
                elif kind == 'error':
                    busy = False; operation_bar.stop(); cancel_button.configure(state='disabled'); update_controls()
                    status_text.set('Failed: ' + value[0]); activity.append(value[1] + ': ' + value[0])
                    messagebox.showerror(value[1], value[0])
                else:
                    busy = False
                    operation_bar.stop(); cancel_button.configure(state='disabled'); update_controls()
                    result, callback, label = value
                    status_text.set(label + ' complete.')
                    activity.append(label + ': ' + (result if isinstance(result, str) else json.dumps(result)))
                    if callback:
                        try: callback(result)
                        except Exception as exc: messagebox.showerror(label, str(exc))
                    elif result: messagebox.showinfo(label, result if isinstance(result, str) else json.dumps(result, indent=2))
                if kind in ('success', 'error'):
                    show_text(activity_widget, '\n\n'.join(activity[-100:]))
        except queue.Empty: pass
        root.after(100, poll)

    def tab(name):
        frame = ttk.Frame(notebook, padding=16); notebook.add(frame, text=name); return frame

    def row(parent):
        frame = ttk.Frame(parent); frame.pack(fill='x', pady=6); return frame

    def button(parent, label, command):
        widget = ttk.Button(parent, text=label, command=command)
        widget.pack(side='left', padx=(0, 8))
        controls_by_name[label] = widget
        return widget

    def show_text(widget, text):
        widget.delete('1.0', 'end'); widget.insert('1.0', text)

    server = tab('Server')
    server_state = tk.StringVar(value='Status: not checked')
    ttk.Label(server, textvariable=server_state, font=('Segoe UI', 14, 'bold')).pack(anchor='w', pady=8)
    ttk.Label(server, text=settings()['game_host'] + ':' + str(settings()['game_port'])).pack(anchor='w')
    active_pack_text = tk.StringVar(value='Loading saved modpack sessions…')
    ttk.Label(server, textvariable=active_pack_text).pack(anchor='w', pady=4)
    connection_text = tk.StringVar(value='Connecting…')
    ttk.Label(server, textvariable=connection_text).pack(anchor='w', pady=(4, 8))
    controls = row(server)
    def render_status(result):
        nonlocal latest_state
        latest_state = result['state']
        resources = result.get('resources', {})
        memory = resources.get('memory_bytes', 0) / 1024**3
        server_state.set(f"{result['state'].upper()}  •  RAM {memory:.2f} GB  •  CPU {resources.get('cpu_absolute', 0):.0f}%")
        uptime = resources.get('uptime', 0) // 1000
        connection_text.set(f"Connected • updated {datetime.now():%H:%M:%S} • Uptime {uptime//3600:.0f}h {(uptime%3600)//60:.0f}m • Disk {resources.get('disk_bytes',0)/1024**3:.2f} GB")
        update_controls()
    def update_controls():
        for name, allowed in {'Start': latest_state == 'offline', 'Save world': latest_state == 'running',
                'Save & Stop': latest_state == 'running', 'Restart': latest_state == 'running',
                'Send command': latest_state == 'running', 'Upload / install JAR': latest_state == 'offline',
                'Disable mod': latest_state == 'offline', 'Create new world': latest_state == 'offline',
                'Back up world': latest_state == 'offline', 'Switch world': latest_state == 'offline'}.items():
            if name in controls_by_name:
                controls_by_name[name].configure(state='normal' if allowed and not busy else 'disabled')
    def refresh_live_status():
        nonlocal status_pending
        if not status_pending:
            status_pending = True
            def worker():
                try: events.put(('live_status', manager().status()))
                except Exception: events.put(('status_error', None))
            threading.Thread(target=worker, daemon=True).start()
        root.after(5000, refresh_live_status)
    button(controls, 'Refresh status', lambda: run('Status', lambda: manager().status(), render_status))
    button(controls, 'Start', lambda: run('Start server', lambda: manager().power('start'), render_status))
    button(controls, 'Save world', lambda: run('Save world', lambda: manager().save()))
    button(controls, 'Save & Stop', lambda: run('Save & Stop', lambda: manager().save_stop(), render_status))
    def restart():
        if messagebox.askyesno('Restart', 'Save and restart the server? Connected players will disconnect.'):
            run('Restart server', lambda: (manager().save(), manager().power('restart'))[1], render_status)
    button(row(server), 'Restart', restart)
    button(row(server), 'Open hosting panel', lambda: webbrowser.open(settings()['panel_url'] + '/server/' + settings()['server_id']))
    ttk.Label(server, text='Console command', font=('Segoe UI', 11, 'bold')).pack(anchor='w', pady=(20, 4))
    command_text = tk.StringVar(); command_entry = ttk.Entry(server, textvariable=command_text)
    command_history = []; history_index = 0
    command_entry.pack(fill='x')
    def send():
        nonlocal history_index
        command = command_text.get().strip()
        if command and not busy and latest_state == 'running':
            command_history.append(command); history_index = len(command_history)
            def sent(result):
                command_text.set(''); status_text.set('Command sent. See Live Logs for the response.')
                notebook.select(logtab)
            run('Send command', lambda: manager().command(command), sent)
    def history(direction):
        nonlocal history_index
        if command_history:
            history_index = max(0, min(len(command_history), history_index + direction))
            command_text.set(command_history[history_index] if history_index < len(command_history) else '')
        return 'break'
    button(row(server), 'Send command', send)
    command_entry.bind('<Return>', lambda event: send())
    command_entry.bind('<Up>', lambda event: history(-1))
    command_entry.bind('<Down>', lambda event: history(1))
    ttk.Label(server, text='Examples: list  •  weather clear  •  time set day  •  dh debug\nPower controls need a panel Client API key. Logs and downloads use SFTP.',
              wraplength=850).pack(anchor='w', pady=14)

    logtab = tab('Live Logs')
    log_lines = deque(maxlen=3000)
    log_connection = tk.StringVar(value='Connecting to live log…')
    ttk.Label(logtab, textvariable=log_connection).pack(anchor='w')
    log_search, log_level = tk.StringVar(), tk.StringVar(value='All')
    follow, paused = tk.BooleanVar(value=True), tk.BooleanVar(value=False)
    filters = row(logtab)
    ttk.Label(filters, text='Search').pack(side='left', padx=(0, 5))
    ttk.Entry(filters, textvariable=log_search).pack(side='left', fill='x', expand=True, padx=(0, 8))
    ttk.Combobox(filters, textvariable=log_level, values=['All', 'Warnings & errors', 'Errors'], state='readonly', width=18).pack(side='left')
    log_widget = ScrolledText(logtab, font=('Consolas', 10), wrap='none', state='disabled')
    log_widget.pack(fill='both', expand=True)
    log_horizontal = ttk.Scrollbar(logtab, orient='horizontal', command=log_widget.xview)
    log_horizontal.pack(fill='x')
    log_widget.configure(xscrollcommand=log_horizontal.set)
    for name, color in [('error', '#b91c1c'), ('warning', '#956300'), ('info', '#174f7a')]: log_widget.tag_config(name, foreground=color)
    def render_logs(*args):
        position = log_widget.yview()
        query, level = log_search.get().casefold(), log_level.get()
        log_widget.configure(state='normal'); log_widget.delete('1.0', 'end')
        for line in log_lines:
            folded = line.casefold()
            error = any(x in folded for x in ('/error]', '/fatal]', 'exception', 'error:'))
            warning = '/warn]' in folded or 'warning:' in folded
            if query and query not in folded: continue
            if level == 'Errors' and not error: continue
            if level == 'Warnings & errors' and not (error or warning): continue
            log_widget.insert('end', line + '\n', 'error' if error else 'warning' if warning else 'info')
        log_widget.configure(state='disabled')
        if follow.get(): log_widget.see('end')
        elif position: log_widget.yview_moveto(position[0])
    log_search.trace_add('write', render_logs); log_level.trace_add('write', render_logs)
    log_controls = row(logtab)
    ttk.Checkbutton(log_controls, text='Auto-scroll', variable=follow).pack(side='left', padx=(0, 12))
    def pause():
        if paused.get(): pause_logs.set(); log_connection.set('Paused')
        else: pause_logs.clear(); log_connection.set('Resuming…')
    ttk.Checkbutton(log_controls, text='Pause', variable=paused, command=pause).pack(side='left', padx=(0, 12))
    def save_log():
        output = filedialog.asksaveasfilename(initialfile='server-log.txt', defaultextension='.txt')
        if output:
            Path(output).write_text('\n'.join(log_lines), encoding='utf-8'); status_text.set('Log exported: ' + output)
    button(log_controls, 'Export log', save_log)
    button(log_controls, 'Copy visible log', lambda: (root.clipboard_clear(), root.clipboard_append(log_widget.get('1.0','end-1c'))))
    def log_worker():
        cursor = LogCursor()
        while not stop_event.is_set():
            try:
                with Manager().sftp() as sftp:
                    while not stop_event.is_set():
                        if not pause_logs.is_set():
                            with sftp.open('logs/latest.log', 'rb') as stream:
                                events.put(('log_chunk', cursor.read(stream)))
                        if stop_event.wait(2): return
            except Exception as exc:
                events.put(('log_error', redact(str(exc))))
                if stop_event.wait(8): return
    activity_tab = tab('Activity')
    activity_widget = ScrolledText(activity_tab, font=('Consolas', 10), wrap='word')
    activity_widget.pack(fill='both', expand=True)
    cancel_button = ttk.Button(outer, text='Cancel transfer / export', state='disabled', command=cancel_event.set)
    cancel_button.pack(anchor='e', pady=(4, 0))

    files = tab('Files & Mods')
    remote_path = tk.StringVar(value='mods')
    nav = row(files); ttk.Entry(nav, textvariable=remote_path).pack(side='left', fill='x', expand=True, padx=(0, 8))
    tree = ttk.Treeview(files, columns=('type', 'size'), selectmode='browse')
    tree.heading('#0', text='Name'); tree.heading('type', text='Type'); tree.heading('size', text='Bytes')
    tree.column('type', width=100); tree.column('size', width=100); tree.pack(fill='both', expand=True)
    def populate(entries):
        tree.delete(*tree.get_children())
        for entry in sorted(entries, key=lambda e: (not e['directory'], e['name'].lower())):
            tree.insert('', 'end', text=entry['name'], values=('Folder' if entry['directory'] else 'File', entry['size']))
    def refresh_files():
        path = remote_path.get(); run('List files', lambda: manager().list_files(path), populate)
    button(nav, 'Open folder', refresh_files)
    def up_folder():
        remote_path.set(str(Path(remote_path.get()).parent).replace('\\','/')); refresh_files()
    button(nav, 'Up', up_folder)
    def selected():
        selection = tree.selection()
        if not selection: raise ValueError('Select a file first.')
        name = tree.item(selection[0], 'text')
        return remote_path.get().rstrip('/') + '/' + name if remote_path.get() not in ('', '.') else name
    def ui_action(action):
        try: action()
        except Exception as exc: messagebox.showerror('Files', str(exc))
    def open_selected(event=None):
        path = selected()
        if tree.item(tree.selection()[0], 'values')[0] == 'Folder':
            remote_path.set(path); refresh_files()
        else:
            run('Read file', lambda: manager().read_file(path), lambda text: editor(path, text))
    tree.bind('<Double-1>', lambda event: ui_action(open_selected))
    def editor(path, text):
        window = tk.Toplevel(root); window.title(path); window.geometry('850x600')
        content = ScrolledText(window, font=('Consolas', 10), wrap='none'); content.pack(fill='both', expand=True)
        content.insert('1.0', text)
        def save_file():
            data = content.get('1.0', 'end-1c').encode('utf-8')
            if messagebox.askyesno('Save server file', f'Save {path}? The server must be offline. The previous file will be backed up.'):
                run('Save file', lambda: manager().write_bytes(path, data))
        ttk.Button(window, text='Save with backup', command=save_file).pack(pady=8)
    def upload():
        local = filedialog.askopenfilename(title='Choose a mod JAR or server file')
        if not local: return
        destination = simpledialog.askstring('Upload', 'Server file path:', initialvalue=remote_path.get().rstrip('/') + '/' + Path(local).name)
        if destination: run('Upload file', lambda: manager().upload(local, destination))
    def download():
        path = selected(); local = filedialog.asksaveasfilename(initialfile=Path(path).name)
        if local: run('Download', lambda: manager().download(path, local))
    def disable():
        path = selected()
        if not path.startswith('mods/') or path.count('/') != 1: raise ValueError('Select a JAR in the mods folder.')
        if messagebox.askyesno('Disable mod', f'Move {Path(path).name} into the backup folder? The server must be offline. Client mods may also need updating.'):
            run('Disable mod', lambda: manager().archive_mod(Path(path).name))
    actions = row(files)
    button(actions, 'Edit text', lambda: ui_action(open_selected))
    button(actions, 'Upload / install JAR', upload)
    button(actions, 'Download', lambda: ui_action(download))
    button(actions, 'Disable mod', lambda: ui_action(disable))
    ttk.Label(files, text='Mod dependencies and Minecraft/loader compatibility must match. Restore disabled JARs by downloading them from _control_backups and uploading them to mods.', wraplength=880).pack(anchor='w', pady=6)

    worlds = tab('World & Backups')
    world_summary = tk.StringVar(value='Load world details to see the active world and available worlds.')
    ttk.Label(worlds, textvariable=world_summary, wraplength=880).pack(anchor='w')
    world_choice = tk.StringVar()
    world_navigation = row(worlds)
    world_select = ttk.Combobox(world_navigation, textvariable=world_choice, state='readonly', width=34)
    world_select.pack(side='left', padx=(0,8))
    def show_world_info(result):
        world_summary.set(f"Active: {result['active']} • Configured seed: {result['configured_seed']} • View: {result['view_distance']} • Simulation: {result['simulation_distance']}")
        world_select.configure(values=result['worlds']); world_choice.set(result['active'])
    button(world_navigation, 'Load worlds', lambda: run('World information', lambda: manager().world_info(), show_world_info))
    def switch_world():
        name = world_choice.get()
        if name and messagebox.askyesno('Switch world', f'Load {name} next time the server starts? All existing worlds are preserved.'):
            run('Switch world', lambda: manager().switch_world(name))
    button(world_navigation, 'Switch world', switch_world)
    ttk.Label(worlds, text='Create a fresh world without deleting the current one.', font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=8)
    world_name, world_seed = tk.StringVar(), tk.StringVar()
    for label, value in (('New world folder', world_name), ('Seed', world_seed)):
        ttk.Label(worlds, text=label).pack(anchor='w', pady=(10, 3)); ttk.Entry(worlds, textvariable=value).pack(fill='x')
    def create_world():
        name, seed = world_name.get(), world_seed.get()
        if messagebox.askyesno('New world', f'Switch to a new world named {name}? The current world stays on the server. Start the server afterwards to generate it.'):
            run('Create new world', lambda: manager().new_world(name, seed))
    button(row(worlds), 'Create new world', create_world)
    ttk.Separator(worlds).pack(fill='x', pady=20)
    ttk.Label(worlds, text='Local world backup', font=('Segoe UI', 12, 'bold')).pack(anchor='w')
    ttk.Label(worlds, text='Save & Stop first. Downloads the active world, player data, and server.properties into a verified ZIP.\nKeep the app open until the backup finishes.', wraplength=880).pack(anchor='w', pady=8)
    button(row(worlds), 'Back up world', lambda: run('World backup', lambda: manager().backup_world()))
    def open_backups():
        path = Path(settings()['backup_path']); path.mkdir(parents=True, exist_ok=True); os.startfile(path)
    button(row(worlds), 'Open backup folder', open_backups)
    button(row(worlds), 'Edit server.properties', lambda: run('Read properties', lambda: manager().read_file('server.properties'), lambda text: editor('server.properties', text)))

    sessions_tab = tab('Sessions')
    ttk.Label(sessions_tab, text='Keep each modpack and all its worlds together.', font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=8)
    ttk.Label(sessions_tab, text='Load & play saves and stops the current server, loads the selected session, then launches its Minecraft modpack.\nLaunch Minecraft opens the client for the active server. Join through Multiplayer once the game loads.', wraplength=880).pack(anchor='w', pady=8)
    session_list = ttk.Treeview(sessions_tab, columns=('label', 'state'), show='headings', height=8)
    session_list.heading('label', text='Modpack'); session_list.heading('state', text='Status')
    session_list.column('label', width=550); session_list.column('state', width=180)
    session_list.pack(fill='x', pady=8)
    session_metadata = {}
    def display_sessions(data):
        session_metadata.clear(); session_metadata.update(data.get('sessions', {}))
        session_list.delete(*session_list.get_children())
        for key, item in data.get('sessions', {}).items():
            session_list.insert('', 'end', iid=key, values=(item['label'], 'Active' if key == data['active'] else 'Saved'))
            if key == data['active']: session_list.selection_set(key)
        active = data.get('sessions', {}).get(data.get('active'), {})
        active_pack_text.set('Active modpack: ' + active.get('label', 'No session registered'))
        if active.get('client_path'): client_path.set(active['client_path'])
        if data.get('recovery_required'):
            status_text.set('Session operation interrupted. Use Recover interrupted switch before starting.')
    def refresh_sessions():
        import sessions
        run('Load sessions', lambda: sessions.inventory(manager()), display_sessions)
    def register_session():
        import sessions
        label = simpledialog.askstring('Current server', 'Name this existing modpack session:')
        if label:
            run('Register current session', lambda: sessions.register(manager(), 'original', label), display_sessions)
    def import_session():
        import sessions
        folder = filedialog.askdirectory(title='Choose a prepared, runnable server folder')
        if not folder: return
        key = simpledialog.askstring('New session', 'Session ID (lowercase letters, numbers, hyphens):')
        if not key: return
        label = simpledialog.askstring('New session', 'Modpack name:')
        if label:
            run('Import session', lambda: sessions.import_folder(manager(), key, label, folder), display_sessions)
    def select_session(start):
        import sessions
        import launcher
        selected = session_list.selection()
        if selected:
            if start:
                run('Start session and Minecraft', lambda: launcher.play_session(manager(), selected[0]), display_sessions)
            else:
                run('Switch session', lambda: sessions.switch(manager(), selected[0], False), display_sessions)
    session_buttons = row(sessions_tab)
    button(session_buttons, 'Refresh sessions', refresh_sessions)
    button(session_buttons, 'Register current server', register_session)
    button(session_buttons, 'Import server folder', import_session)
    session_buttons = row(sessions_tab)
    button(session_buttons, 'Load session', lambda: select_session(False))
    button(session_buttons, 'Load & play', lambda: select_session(True))
    def launch_minecraft():
        import launcher
        run('Launch Minecraft', lambda: launcher.launch_active(manager()))
    button(session_buttons, 'Launch Minecraft', launch_minecraft)
    def recover_session():
        import sessions
        run('Recover session', lambda: sessions.recover(manager()), display_sessions)
    button(session_buttons, 'Recover interrupted switch', recover_session)
    def backup_session():
        import sessions
        run('Full server backup', lambda: sessions.backup(manager()))
    button(row(sessions_tab), 'Back up entire active session', backup_session)
    def open_session_client():
        selected = session_list.selection()
        if selected:
            path = session_metadata.get(selected[0], {}).get('client_path')
            if path and Path(path).is_dir(): os.startfile(path)
            else: messagebox.showinfo('Client profile', 'Launch the matching modpack from your CurseForge library.')
    button(row(sessions_tab), 'Open matching client folder', lambda: ui_action(open_session_client))
    def session_tab_changed(event):
        if notebook.select() == str(sessions_tab) and not busy:
            refresh_sessions()
    notebook.bind('<<NotebookTabChanged>>', session_tab_changed)

    client = tab('Client Pack')
    client_path = tk.StringVar(value=settings()['client_path'])
    ttk.Label(client, text='Installed client profile', font=('Segoe UI', 12, 'bold')).pack(anchor='w', pady=8)
    ttk.Label(client, textvariable=client_path, wraplength=880).pack(anchor='w', pady=8)
    def open_profile(): os.startfile(client_path.get())
    button(row(client), 'Open client folder', lambda: ui_action(open_profile))
    button(row(client), 'Open client mods', lambda: os.startfile(Path(client_path.get()) / 'mods'))
    button(row(client), 'Open client config', lambda: os.startfile(Path(client_path.get()) / 'config'))
    def export():
        output = filedialog.asksaveasfilename(initialfile='Build-Explore-and-Create.zip', defaultextension='.zip', filetypes=[('CurseForge pack', '*.zip')])
        if output:
            def export_active():
                connection = manager(); connection.config = dict(connection.config, client_path=client_path.get())
                return connection.export_pack(output)
            run('Export client pack', export_active)
    button(row(client), 'Export CurseForge pack', export)
    ttk.Label(client, text='To import: open CurseForge → Minecraft → Create Custom Profile → Import, then choose the exported ZIP.\nUse the Play screen to launch the matching profile and see the server address.\nClose Minecraft before replacing its mods or editing client settings.', wraplength=880).pack(anchor='w', pady=20)

    setup = tab('Settings')
    fields = {}
    labels = {'panel_url': 'Panel URL', 'server_id': 'Server ID', 'game_host': 'Game host', 'game_port': 'Game port',
        'sftp_host': 'SFTP host', 'sftp_port': 'SFTP port', 'sftp_user': 'SFTP username',
        'host_sha256': 'SFTP host fingerprint (SHA-256 hex)', 'client_path': 'Client profile folder', 'backup_path': 'Backup folder'}
    for index, (key, label) in enumerate(labels.items()):
        ttk.Label(setup, text=label).grid(row=index, column=0, sticky='w', pady=3, padx=(0, 10))
        fields[key] = tk.StringVar(value=str(settings()[key]))
        ttk.Entry(setup, textvariable=fields[key]).grid(row=index, column=1, sticky='ew', pady=3)
    setup.columnconfigure(1, weight=1)
    passwords = {}
    for index, (key, label) in enumerate((('api_key', 'Panel Client API key'), ('sftp_password', 'SFTP password')), start=len(labels)):
        passwords[key] = tk.StringVar()
        ttk.Label(setup, text=label).grid(row=index, column=0, sticky='w', pady=3)
        ttk.Entry(setup, textvariable=passwords[key], show='•').grid(row=index, column=1, sticky='ew', pady=3)
    def persist():
        try:
            config = save_settings({k: v.get() for k, v in fields.items()})
            for key, value in passwords.items():
                if value.get(): store_secret(config, key, value.get()); value.set('')
            credential_summary()
            messagebox.showinfo('Settings', 'Saved. Blank password fields keep existing credentials. Secrets are stored in Windows Credential Manager.')
        except Exception as exc: messagebox.showerror('Settings', str(exc))
    ttk.Button(setup, text='Save settings & credentials', command=persist).grid(row=12, column=1, sticky='w', pady=8)
    ttk.Label(setup, text='One-time setup: in the hosting panel, open Account → API Credentials and create a Client API key.\nPaste it here with your SFTP password. Leave secret fields blank to keep saved values.\nThe app never stores credentials inside the executable or settings JSON.', wraplength=730).grid(row=13, column=0, columnspan=2, sticky='w', pady=8)
    setup_actions = ttk.Frame(setup); setup_actions.grid(row=14, column=1, sticky='w')
    ttk.Button(setup_actions, text='Open API credentials page', command=lambda: webbrowser.open(settings()['panel_url'] + '/account/api')).pack(side='left')
    def test_connection():
        def test():
            connection = Manager(); result = connection.status(); connection.list_files('.')
            return 'Panel API and SFTP connected. Server: ' + result['state']
        run('Test connection', test)
    ttk.Button(setup_actions, text='Test connection', command=test_connection).pack(side='left', padx=8)
    credential_text = tk.StringVar()
    ttk.Label(setup, textvariable=credential_text).grid(row=15, column=0, columnspan=2, sticky='w', pady=6)
    ttk.Label(setup, text='Settings file: ' + str(CONFIG_PATH), wraplength=900).grid(row=16, column=0, columnspan=2, sticky='w', pady=6)
    def credential_summary():
        parts = []
        for key, name in [('api_key','API key'), ('sftp_password','SFTP password')]:
            try: secret(settings(),key); present = True
            except Exception: present = False
            parts.append(name + ': ' + ('saved in Windows Credential Manager' if present else 'missing'))
        credential_text.set(' • '.join(parts) + ' • Blank fields keep saved credentials.')
    def close():
        if busy:
            messagebox.showinfo('Operation in progress', 'Please wait for the operation to finish before closing the app.'); return
        stop_event.set(); root.destroy()
    root.protocol('WM_DELETE_WINDOW', close)
    credential_summary(); update_controls()
    threading.Thread(target=log_worker, daemon=True).start()
    poll(); root.after(200, refresh_live_status); root.after(350, refresh_sessions); root.mainloop()


if __name__ == '__main__':
    if sys.argv[1:2] == ['--advanced']:
        gui()
    elif sys.argv[1:2] == ['--serve']:
        import service
        service.main(sys.argv[2:])
    elif sys.argv[1:2] == ['--version']:
        from shared import VERSION
        print(VERSION)
    elif len(sys.argv) > 1:
        raise SystemExit(cli(sys.argv[1:]))
    else:
        import desktop
        desktop.gui()
