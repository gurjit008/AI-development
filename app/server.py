"""Bounded, same-origin local HTTP server. Not production ingress."""
import argparse
import base64
import binascii
import getpass
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sqlite3
import threading
import time
import uuid
from http.cookies import SimpleCookie, CookieError
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs
from . import auth, database, importer, service
from .schema import get_schema


class Application(ThreadingHTTPServer):
    daemon_threads = True
    PREVIEW_BUDGET = 64 * 1024 * 1024

    def stage_preview(self, proposal, session_hash):
        """Caller holds lock; bound aggregate staged logical JSON size."""
        current = time.time()
        self.previews = {key: value for key, value in self.previews.items() if value['expires'] > current}
        logical_size = len(json.dumps(proposal, ensure_ascii=False, allow_nan=False).encode('utf-8'))
        if len(self.previews) >= 8 or logical_size + sum(value['logical_size'] for value in self.previews.values()) > self.PREVIEW_BUDGET:
            service.fail('Import preview staging limit reached; wait for previews to expire', 413, 'preview_limit')
        token = auth.credential()
        self.previews[token] = {'proposal': proposal, 'session': session_hash, 'expires': current + 600, 'logical_size': logical_size}
        return token

    def __init__(self, address, db_path):
        database.initialize(db_path)
        self.db_path = str(db_path)
        self.preauth = {}
        self.previews = {}
        self.attempts = {}
        self.lock = threading.RLock()
        self.dummy_password = auth.password_hash(secrets.token_urlsafe(24))
        super().__init__(address, Handler)
        self.origin = f'http://127.0.0.1:{self.server_port}'


class Handler(BaseHTTPRequestHandler):
    server_version = 'LocalDemo/1'

    def log_message(self, format, *args):
        # Never log paths, query strings, bodies or supplied credentials.
        pass

    def do_GET(self):
        self.handle_request()

    def do_POST(self):
        self.handle_request()

    def do_PUT(self):
        self.handle_request()

    def send(self, status, payload, content_type='application/json; charset=utf-8', cookie=None):
        try:
            self.write_response(status, payload, content_type, cookie)
        except (OSError, TimeoutError):
            # Disconnects during success or error responses are normal client
            # termination, including Windows ConnectionAbortedError.
            self.close_connection = True

    def write_response(self, status, payload, content_type, cookie):
        data = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode() if not isinstance(payload, bytes) else payload
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if cookie:
            self.send_header('Set-Cookie', 'session=' + cookie + '; HttpOnly; SameSite=Strict; Path=/')
        self.end_headers()
        self.wfile.write(data)

    def token(self):
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get('Cookie', ''))
            return cookies['session'].value if 'session' in cookies else ''
        except CookieError:
            return ''

    def session(self, conn):
        token = self.token()
        if not token:
            return None
        row = conn.execute('SELECT s.*,u.username,u.role,u.active FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id_hash=? AND s.expires_at>? AND u.active=1', (auth.session_digest(token), auth.now())).fetchone()
        return dict(row) if row else None

    def csrf(self, session):
        if self.headers.get('Origin') != self.server.origin:
            service.fail('Origin denied', 403, 'csrf')
        expected = session['csrf_token'] if session else self.server.preauth.get(auth.session_digest(self.token()), {}).get('csrf_token', '')
        if not expected or not secrets.compare_digest(expected, self.headers.get('X-CSRF-Token', '')):
            service.fail('CSRF token denied', 403, 'csrf')

    def body(self, path):
        if self.headers.get('Transfer-Encoding'):
            service.fail('Transfer encoding unsupported')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            service.fail('Invalid Content-Length')
        limit = 24 * 1024 * 1024 if path == '/api/imports/preview' else 1024 * 1024
        if length < 0 or length > limit:
            service.fail('Request too large', 413, 'request_limit')
        if self.headers.get_content_type() != 'application/json':
            service.fail('JSON content type required')
        try:
            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise ValueError('Duplicate JSON key')
                    result[key] = value
                return result
            result = json.loads(self.rfile.read(length).decode('utf-8'), object_pairs_hook=pairs, parse_constant=lambda value: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))
            if not isinstance(result, dict):
                service.fail('JSON object required')
            return result
        except (UnicodeDecodeError, ValueError, RecursionError):
            service.fail('Malformed JSON')

    def handle_request(self):
        conn = None
        try:
            self.connection.settimeout(15)
            if self.headers.get('Host') != urlsplit(self.server.origin).netloc:
                service.fail('Host denied', 403, 'host_denied')
            parsed = urlsplit(self.path)
            path = parsed.path
            query = {key: values[-1] for key, values in parse_qs(parsed.query).items()}
            if not path.startswith('/api/'):
                if self.command != 'GET':
                    service.fail('Not found', 404, 'not_found')
                files = {'/': ('index.html', 'text/html; charset=utf-8'), '/index.html': ('index.html', 'text/html; charset=utf-8'), '/app.js': ('app.js', 'text/javascript; charset=utf-8'), '/styles.css': ('styles.css', 'text/css; charset=utf-8')}
                if path not in files:
                    service.fail('Not found', 404, 'not_found')
                name, kind = files[path]
                self.send(200, (Path(__file__).parent.parent / 'web' / name).read_bytes(), kind)
                return
            # Client-controlled network reads must never hold the application lock.
            # Resolve identity and recheck CSRF after reading to catch invalidation.
            body = {}
            if self.command in ('POST', 'PUT'):
                if self.headers.get('Origin') != self.server.origin:
                    service.fail('Origin denied', 403, 'csrf')
                body = self.body(path)
            conn = database.connect(self.server.db_path)
            with self.server.lock:
                status, result, cookie = self.resolve_request(conn, path, query, body)
            content_type = 'text/csv; charset=utf-8' if isinstance(result, bytes) else 'application/json; charset=utf-8'
            self.send(status, result, content_type, cookie)
        except service.APIError as error:
            value = {'code': error.code, 'message': error.message}
            if error.fields:
                value['fields'] = error.fields
            self.send(error.status, {'error': value})
        except sqlite3.IntegrityError:
            self.send(400, {'error': {'code': 'constraint', 'message': 'Data violates a uniqueness or integrity constraint'}})
        except (ValueError, binascii.Error, KeyError) as error:
            self.send(400, {'error': {'code': 'invalid_request', 'message': 'Invalid request or workbook'}})
        except (BrokenPipeError, TimeoutError, ConnectionResetError):
            self.close_connection = True
        except Exception:
            self.send(500, {'error': {'code': 'internal', 'message': 'Operation failed'}})
        finally:
            if conn:
                conn.close()

    def resolve_request(self, conn, path, query, body):
        """Resolve state under lock; return detached data for unlocked transmission."""
        session = self.session(conn)
        current = time.time()
        self.server.preauth = {key: value for key, value in self.server.preauth.items() if value['expires'] > current}
        self.server.previews = {key: value for key, value in self.server.previews.items() if value['expires'] > current}
        if self.command == 'GET' and path == '/api/session':
            if session:
                return 200, {'user': {'id': session['user_id'], 'username': session['username'], 'role': session['role']}, 'csrf_token': session['csrf_token']}, None
            else:
                token, csrf = auth.credential(), auth.credential()
                if len(self.server.preauth) >= 1000:
                    service.fail('Too many sessions', 429, 'session_limit')
                self.server.preauth[auth.session_digest(token)] = {'csrf_token': csrf, 'expires': current + 600}
                return 200, {'user': None, 'csrf_token': csrf}, token
        if self.command in ('POST', 'PUT'):
            self.csrf(session)
        if path == '/api/login' and self.command == 'POST':
            service.object_body(body, ('username', 'password'))
            username = service.text(body.get('username'), 100, True)
            password = service.text(body.get('password'), 256)
            attempts = [stamp for stamp in self.server.attempts.get(self.client_address[0], []) if stamp > current - 60]
            if len(attempts) >= 8:
                service.fail('Login temporarily throttled', 429, 'login_throttle')
            attempts.append(current)
            self.server.attempts[self.client_address[0]] = attempts
            user = conn.execute('SELECT * FROM users WHERE username=? AND active=1', (username,)).fetchone()
            valid = auth.verify_password(password, user['password_hash'] if user else self.server.dummy_password)
            if not valid or not user:
                service.fail('Invalid credentials', 401, 'invalid_credentials')
            token, csrf = auth.credential(), auth.credential()
            with conn:
                conn.execute('DELETE FROM sessions WHERE id_hash=?', (auth.session_digest(self.token()),))
                conn.execute('INSERT INTO sessions(id_hash,user_id,csrf_token,expires_at,created_at) VALUES(?,?,?,?,?)', (auth.session_digest(token), user['id'], csrf, auth.expiry(), auth.now()))
                service.audit(conn, user['id'], 'login', 'user', user['id'], [])
            self.server.preauth.pop(auth.session_digest(self.token()), None)
            return 200, {'user': {'id': user['id'], 'username': user['username'], 'role': user['role']}, 'csrf_token': csrf}, token
        if not session:
            service.fail('Authentication required', 401, 'unauthenticated')
        user = {'id': session['user_id'], 'username': session['username'], 'role': session['role']}
        if path == '/api/logout' and self.command == 'POST':
            with conn:
                conn.execute('DELETE FROM sessions WHERE id_hash=?', (session['id_hash'],))
            return 200, {'ok': True}, '; Max-Age=0'
        status, result = self.dispatch(conn, user, session, path, query, body)
        return status, result, None

    def dispatch(self, conn, user, session, path, query, body):
        method = self.command
        parts = path.strip('/').split('/')
        if path == '/api/schema' and method == 'GET':
            return 200, get_schema()
        if path == '/api/reports/export.csv' and method == 'GET':
            return 200, service.export_reports(conn, user, query)
        if path == '/api/reports':
            if method == 'GET':
                return 200, service.list_reports(conn, user, query)
            if method == 'POST':
                return 201, {'report': service.create_report(conn, user, body)}
        if len(parts) >= 3 and parts[1] == 'reports':
            identifier = parts[2]
            if len(parts) == 3 and method == 'GET':
                row = service.accessible_report(conn, identifier, user)
                return 200, {'report': service.report(row), 'comments': [dict(row) for row in conn.execute('SELECT * FROM comments WHERE report_id=? ORDER BY created_at,id', (identifier,))]}
            if len(parts) == 3 and method == 'PUT':
                return 200, {'report': service.update_report(conn, user, identifier, body)}
            if len(parts) == 4 and parts[3] == 'comments' and method == 'POST':
                service.require_role(user, 'reviewer', 'admin')
                service.accessible_report(conn, identifier, user)
                service.object_body(body, ('body',))
                comment = {'id': str(uuid.uuid4()), 'report_id': identifier, 'author_id': user['id'], 'body': service.text(body.get('body'), 4000, True), 'created_at': auth.now()}
                with conn:
                    conn.execute('INSERT INTO comments(id,report_id,author_id,body,created_at) VALUES(?,?,?,?,?)', tuple(comment.values()))
                    service.audit(conn, user['id'], 'create', 'comment', comment['id'], ['body'])
                return 201, {'comment': comment}
        if path == '/api/systems':
            if method == 'GET':
                return 200, {'items': [dict(row) for row in conn.execute('SELECT * FROM systems ORDER BY identity')]}
            if method == 'POST':
                return 201, {'system': service.mutate_resource(conn, user, 'systems', body)}
        if len(parts) == 3 and parts[1] == 'systems' and method == 'PUT':
            return 200, {'system': service.mutate_resource(conn, user, 'systems', body, identifier=parts[2])}
        if len(parts) == 4 and parts[1] == 'systems':
            identifier = parts[2]
            if parts[3] == 'inventory' and method == 'GET':
                if not conn.execute('SELECT id FROM systems WHERE id=?', (identifier,)).fetchone():
                    service.fail('System not found', 404, 'not_found')
                return 200, {'items': [dict(row) for row in conn.execute('SELECT * FROM inventory_items WHERE system_id=? ORDER BY category,name', (identifier,))], 'movements': [dict(row) for row in conn.execute('SELECT * FROM inventory_movements WHERE system_id=? ORDER BY created_at', (identifier,))]}
            if parts[3] in ('inventory', 'movements') and method == 'POST':
                table, envelope = ('inventory_items', 'item') if parts[3] == 'inventory' else ('inventory_movements', 'movement')
                return 201, {envelope: service.mutate_resource(conn, user, table, body, system_id=identifier)}
        if len(parts) == 3 and parts[1] == 'inventory' and method == 'PUT':
            return 200, {'item': service.mutate_resource(conn, user, 'inventory_items', body, identifier=parts[2])}
        if path == '/api/users':
            service.require_role(user, 'admin')
            if method == 'GET':
                return 200, {'items': [service.public_user(row) for row in conn.execute('SELECT * FROM users ORDER BY username')]}
            if method == 'POST':
                return 201, {'user': service.mutate_user(conn, user, body)}
        if len(parts) == 3 and parts[1] == 'users' and method == 'PUT':
            return 200, {'user': service.mutate_user(conn, user, body, parts[2])}
        if path == '/api/audit' and method == 'GET':
            service.require_role(user, 'admin')
            page, size = service.pagination(query)
            rows = [dict(row) for row in conn.execute('SELECT * FROM audit_events ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?', (size, (page - 1) * size))]
            return 200, {'items': rows, 'total': conn.execute('SELECT count(*) FROM audit_events').fetchone()[0], 'page': page, 'page_size': size}
        if path == '/api/imports/preview' and method == 'POST':
            service.require_role(user, 'admin')
            service.object_body(body, ('filename', 'content_base64'))
            filename = service.text(body.get('filename'), 250, True)
            content = service.text(body.get('content_base64'), 24 * 1024 * 1024, True)
            data = base64.b64decode(content, validate=True)
            if len(data) > 16 * 1024 * 1024:
                service.fail('Workbook too large', 413, 'import_limit')
            try:
                proposal = importer.preview_workbook(data, filename)
            except ValueError as error:
                safe_messages = {
                    'Unsafe XML declaration or encoding', 'Workbook exceeds 16 MiB limit',
                    'Invalid workbook filename', 'Expanded workbook limit exceeded',
                    'Duplicate ZIP member names', 'Encrypted, macro, or unsafe workbook member',
                    'Suspicious compression ratio', 'Unsupported sheet relationship',
                    'Workbook exceeds 10000 rows', 'Invalid cell address', 'Invalid shared string',
                    'Workbook logical cell/text budget exceeded', 'Unsupported or modified report header layout',
                    'Mixed report versions unsupported', 'No supported V1/V2/V3 report sheet found',
                    'Unsupported inventory layout', 'Import proposal memory budget exceeded',
                    'Invalid or unsupported XLSX workbook',
                }
                reason = str(error)
                cell_limit = re.fullmatch(r'Cell text exceeds (\d{1,6}) characters in sheet .+ at ([A-Z]{1,3}[1-9][0-9]{0,6})', reason)
                if cell_limit:
                    reason = f'Cell text exceeds {cell_limit[1]} characters at {cell_limit[2]}'
                elif reason not in safe_messages:
                    reason = 'Invalid or unsupported XLSX workbook'
                service.fail(reason, 400, 'import_validation')
            candidates = list(proposal.get('reports', []))
            for inventory in proposal.get('inventories', []):
                candidates.extend(inventory.get('items', []))
                candidates.extend(inventory.get('movements', []))
            duplicates = sum(bool(conn.execute('SELECT 1 FROM imported_rows WHERE fingerprint=?', (row['fingerprint'],)).fetchone()) for row in candidates)
            proposal['summary']['skipped_duplicates'] = duplicates
            if duplicates:
                proposal['diagnostics'].append({'severity': 'warning', 'message': f'{duplicates} previously imported rows will be skipped'})
            token = self.server.stage_preview(proposal, session['id_hash'])
            expires = __import__('datetime').datetime.fromtimestamp(time.time() + 600, __import__('datetime').timezone.utc).isoformat()
            return 200, {'token': token, 'summary': proposal['summary'], 'diagnostics': proposal['diagnostics'], 'expires_at': expires}
        if path == '/api/imports/commit' and method == 'POST':
            service.require_role(user, 'admin')
            service.object_body(body, ('token', 'owner_id'))
            token = service.text(body.get('token'), 100, True)
            owner = service.text(body.get('owner_id'), 100, True)
            preview = self.server.previews.get(token)
            if not preview or preview['session'] != session['id_hash'] or preview['expires'] < time.time():
                service.fail('Preview expired or consumed')
            if not conn.execute("SELECT id FROM users WHERE id=? AND role='engineer' AND active=1", (owner,)).fetchone():
                service.fail('Active engineer owner required')
            for imported_report in preview['proposal'].get('reports', []):
                service.validate_fields(imported_report['form_version'], imported_report['fields'], 'draft', allow_raw=True)
            result = importer.commit_import(conn, preview['proposal'], owner, user['id'])
            del self.server.previews[token]
            return 200, result
        service.fail('Route not found', 404, 'not_found')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', default='.local/demo.sqlite3')
    parser.add_argument('--port', type=int, default=8000)
    parser.add_argument('--create-user')
    parser.add_argument('--role', choices=('engineer', 'reviewer', 'admin'), default='admin')
    parser.add_argument('--password-env', help='Read bootstrap password from this environment variable')
    args = parser.parse_args()
    db_path = Path(args.db).resolve()
    web_root = (Path(__file__).parent.parent / 'web').resolve()
    if db_path == web_root or web_root in db_path.parents:
        parser.error('Database must be outside the static web directory')
    db_path.parent.mkdir(parents=True, exist_ok=True)
    database.initialize(str(db_path))
    if args.create_user:
        password = os.environ.get(args.password_env) if args.password_env else getpass.getpass('New user password (minimum 12 characters): ')
        digest = auth.password_hash(password)
        username = service.text(args.create_user, 100, True)
        with database.connect(str(db_path)) as conn:
            identifier = str(__import__('uuid').uuid4())
            conn.execute('INSERT INTO users(id,username,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)', (identifier, username, digest, args.role, auth.now()))
            service.audit(conn, identifier, 'bootstrap', 'user', identifier, ['username', 'role'])
        print('User created.')
        return
    server = Application(('127.0.0.1', args.port), str(db_path))
    print('Local application: ' + server.origin)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == '__main__':
    main()
