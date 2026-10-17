"""Small authenticated service. Keep hosting credentials on this machine only."""
import argparse
import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
import uuid

from core import APP_HOME, Manager, redact
from shared import LocalClient, validate_url

DATABASE = APP_HOME / 'shared-service.sqlite3'


class Service:
    def __init__(self, database=DATABASE, client=None, recover_jobs=False):
        self.database = Path(database); self.database.parent.mkdir(parents=True, exist_ok=True)
        self.client = client or LocalClient(); self.operation = threading.Lock(); self.cache_lock = threading.Lock()
        self.cached = None; self.cached_at = 0; self.current = None
        with self.db() as db:
            db.execute('CREATE TABLE IF NOT EXISTS users (id TEXT PRIMARY KEY, name TEXT, role TEXT, digest TEXT UNIQUE, expires REAL, revoked INTEGER DEFAULT 0)')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, user_id TEXT, state TEXT, error TEXT, created REAL)')
            if recover_jobs: db.execute("UPDATE jobs SET state='failed', error='Service restarted. Check the current session before retrying.' WHERE state='running'")
    @contextlib.contextmanager
    def db(self):
        connection = sqlite3.connect(self.database, timeout=10); connection.row_factory = sqlite3.Row
        try:
            with connection: yield connection
        finally: connection.close()
    def invite(self, url, name, role='player', days=30):
        url = validate_url(url)
        if role not in ('player', 'operator'): raise ValueError('Choose player or operator access.')
        if not name.strip() or len(name) > 80 or not 1 <= days <= 365: raise ValueError('Invalid invitation details.')
        token = secrets.token_urlsafe(32); identifier = str(uuid.uuid4())
        with self.db() as db:
            db.execute('INSERT INTO users VALUES (?, ?, ?, ?, ?, 0)',
                (identifier, name, role, hashlib.sha256(token.encode()).hexdigest(), time.time() + days * 86400))
        return {'format': 1, 'service_url': url, 'token': token, 'name': name, 'role': role, 'expires_days': days, 'id': identifier}
    def authenticate(self, authorization):
        if not authorization.startswith('Bearer ') or len(authorization) > 512: return None
        digest = hashlib.sha256(authorization[7:].encode()).hexdigest()
        with self.db() as db:
            row = db.execute('SELECT id,name,role FROM users WHERE digest=? AND revoked=0 AND expires>?', (digest, time.time())).fetchone()
        return dict(row) if row else None
    def snapshot(self, role):
        with self.cache_lock:
            if self.cached is None or time.monotonic() - self.cached_at > 5:
                self.cached = self.client.snapshot(); self.cached_at = time.monotonic()
            result = dict(self.cached)
        result['role'] = role; result['operation'] = self.current or result.get('operation')
        return result
    def dispatch(self, user, payload):
        if set(payload) - {'action', 'session', 'allow_disconnect'}: raise ValueError('Unknown action fields.')
        if payload.get('action') != 'play' or not isinstance(payload.get('session'), str): raise ValueError('Unsupported server action.')
        if not isinstance(payload.get('allow_disconnect', False), bool): raise ValueError('Invalid confirmation.')
        self.cached_at = 0
        snapshot = self.snapshot(user['role']); selected = payload['session']
        if selected not in snapshot['sessions']: raise ValueError('Unknown session.')
        if snapshot.get('recovery_required'): raise ValueError('The owner needs to recover an interrupted session operation.')
        if user['role'] == 'player' and (selected != snapshot['active'] or snapshot['server']['state'] != 'running'):
            raise PermissionError('Player access can join the running pack. Ask the owner to start or switch the server.')
        if not self.operation.acquire(blocking=False): raise ValueError('Another server operation is already in progress.')
        job = str(uuid.uuid4())
        with self.db() as db:
            db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?)', (job, user['id'], 'running', '', time.time()))
        self.current = {'job': job, 'by': user['name'], 'session': selected}
        def worker():
            error, state = '', 'complete'
            try:
                if user['role'] == 'operator': self.client.play(selected, payload.get('allow_disconnect', False))
            except Exception as failure:
                error, state = redact(str(failure))[:500], 'failed'
            finally:
                with self.db() as db: db.execute('UPDATE jobs SET state=?,error=? WHERE id=?', (state, error, job))
                self.cached_at = 0; self.current = None; self.operation.release()
        threading.Thread(target=worker, daemon=True).start()
        return {'job': job}
    def __call__(self, environ, start_response):
        status, result = '200 OK', {}
        try:
            user = self.authenticate(environ.get('HTTP_AUTHORIZATION', ''))
            if user is None: status, result = '401 Unauthorized', {'error': 'Invitation is invalid, expired, or revoked.'}
            else:
                path, method = environ.get('PATH_INFO', ''), environ.get('REQUEST_METHOD', 'GET')
                if path == '/v1/me' and method == 'GET': result = user
                elif path == '/v1/snapshot' and method == 'GET': result = self.snapshot(user['role'])
                elif path == '/v1/actions' and method == 'POST':
                    size = int(environ.get('CONTENT_LENGTH') or 0)
                    if not 0 < size <= 4096: raise ValueError('Invalid request size.')
                    payload = json.loads(environ['wsgi.input'].read(size))
                    if not isinstance(payload, dict): raise ValueError('Invalid action.')
                    result = self.dispatch(user, payload); status = '202 Accepted'
                elif path.startswith('/v1/jobs/') and method == 'GET':
                    with self.db() as db: row = db.execute('SELECT id,user_id,state,error FROM jobs WHERE id=?', (path.rsplit('/', 1)[-1],)).fetchone()
                    if row is None or (row['user_id'] != user['id'] and user['role'] != 'operator'):
                        status, result = '404 Not Found', {'error': 'Operation not found.'}
                    else: result = {k: row[k] for k in ('id', 'state', 'error')}
                else: status, result = '404 Not Found', {'error': 'Not found.'}
        except PermissionError as error: status, result = '403 Forbidden', {'error': str(error)}
        except (ValueError, KeyError, TypeError): status, result = '400 Bad Request', {'error': 'Invalid request or server operation already in progress.'}
        except Exception: status, result = '503 Service Unavailable', {'error': 'Server connection unavailable. Try again shortly.'}
        body = json.dumps(result).encode()
        start_response(status, [('Content-Type', 'application/json'), ('Content-Length', str(len(body))), ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff')])
        return [body]


def main(argv=None):
    parser = argparse.ArgumentParser(); subs = parser.add_subparsers(dest='action', required=True)
    serve = subs.add_parser('serve'); serve.add_argument('--bind', default='127.0.0.1'); serve.add_argument('--port', type=int, default=8765)
    invite = subs.add_parser('invite'); invite.add_argument('url'); invite.add_argument('name'); invite.add_argument('output'); invite.add_argument('--role', choices=('player', 'operator'), default='player')
    revoke = subs.add_parser('revoke'); revoke.add_argument('id')
    subs.add_parser('users'); subs.add_parser('catalog')
    args = parser.parse_args(argv)
    if args.action == 'catalog':
        from shared import export_catalog
        print(json.dumps(export_catalog(Manager()), indent=2)); return
    app = Service(recover_jobs=args.action == 'serve')
    if args.action == 'serve':
        from waitress import serve
        serve(app, host=args.bind, port=args.port, threads=6, max_request_body_size=4096, channel_timeout=30)
    elif args.action == 'invite':
        output = Path(args.output)
        with output.open('x', encoding='utf-8') as stream: json.dump(app.invite(args.url, args.name, args.role), stream, indent=2)
        print('Invitation saved. Send it privately to this person; do not publish it.')
    elif args.action == 'revoke':
        with app.db() as db: db.execute('UPDATE users SET revoked=1 WHERE id=?', (args.id,))
        print('Access revoked.')
    elif args.action == 'users':
        with app.db() as db: print(json.dumps([dict(row) for row in db.execute('SELECT id,name,role,expires,revoked FROM users')], indent=2))


if __name__ == '__main__': main()
