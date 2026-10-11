"""Independent acceptance checks against a real ephemeral HTTP/SQLite application.

All accounts and workbook rows below are synthetic. No reference rows are loaded.
"""
import base64
import csv
from contextlib import contextmanager
import http.client
import io
import json
import socket
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile
import warnings
from xml.sax.saxutils import escape

from app import auth, database, importer
from app.schema import get_schema
from app.server import Application, Handler


PASSWORD = 'Synthetic-password-123!'


@contextmanager
def managed_connection(path):
    connection = database.connect(path)
    try:
        with connection:
            yield connection
    finally:
        connection.close()


class Client:
    def __init__(self, server):
        self.server = server
        self.cookie = ''
        self.csrf = ''

    def request(self, method, path, data=None, headers=None):
        request_headers = {'Cookie': self.cookie}
        if method != 'GET':
            request_headers.update({'Origin': self.server.origin,
                                    'X-CSRF-Token': self.csrf,
                                    'Content-Type': 'application/json'})
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=60)
        try:
            connection.request(method, path, json.dumps(data) if data is not None else None,
                               request_headers)
            response = connection.getresponse()
            body = response.read()
            result_headers = dict(response.getheaders())
            if 'Set-Cookie' in result_headers:
                self.cookie = result_headers['Set-Cookie'].split(';')[0]
            parsed = json.loads(body) if 'application/json' in result_headers.get('Content-Type', '') else body
            if isinstance(parsed, dict) and 'csrf_token' in parsed:
                self.csrf = parsed['csrf_token']
            return response.status, parsed, result_headers
        finally:
            connection.close()

    def login(self, username):
        self.request('GET', '/api/session')
        status, body, _ = self.request('POST', '/api/login', {'username': username, 'password': PASSWORD})
        if status != 200:
            raise AssertionError((status, body))
        return body['user']


def value(data):
    return {'state': 'value', 'value': data}


def submission():
    return {key: value(val) for key, val in {
        'surgery_date': '2026-10-01', 'hospital_name': 'Synthetic Hospital',
        'city': 'Synthetic City', 'surgeon_name': 'Synthetic Surgeon',
        'application_engineers': 'Synthetic Engineer', 'surgery_type': 'Cranial',
        'surgery_success': True, 'navigation_usage': 'Completely',
        'system_base': 'Optical', 'software_version': 'synthetic-1',
        'system_identity': 'SYN-001'}.items()}


def synthetic_workbook(fields, version):
    """Build sparse XLSX using published schema headers; expectations are independent."""
    stream = io.BytesIO()
    def cell(column, row, data):
        return f'<c r="{column}{row}" t="inlineStr"><is><t>{escape(str(data))}</t></is></c>'
    columns = {field['id']: field['source_column'] for field in fields}
    hospital_column = columns['hospital_name']
    rows = '<row r="1">' + ''.join(cell(f['source_column'], 1, f['label']) for f in fields) + '</row>'
    rows += '<row r="2">' + cell(hospital_column, 2, 'Synthetic Imported ' + version) + '</row>'
    with zipfile.ZipFile(stream, 'w') as archive:
        archive.writestr('xl/workbook.xml', '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Form responses 1" sheetId="1" r:id="r1"/></sheets></workbook>')
        archive.writestr('xl/_rels/workbook.xml.rels', '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr('xl/worksheets/sheet1.xml', '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>' + rows + '</sheetData></worksheet>')
    return stream.getvalue()


class HTTPAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'qa.sqlite3'
        self.server = Application(('127.0.0.1', 0), self.path)
        digest = auth.password_hash(PASSWORD)
        with managed_connection(self.path) as connection:
            for name, role in [('engineer', 'engineer'), ('other', 'engineer'),
                               ('reviewer', 'reviewer'), ('admin', 'admin')]:
                connection.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',
                                   (name, name, digest, role, 1, '2026-10-01T00:00:00+00:00'))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.stop_server)
        self.clients = {}
        for name in ('engineer', 'other', 'reviewer', 'admin'):
            client = Client(self.server)
            client.login(name)
            self.clients[name] = client

    def stop_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    def call(self, actor, method, path, body=None, expected=200, headers=None):
        status, result, response_headers = self.clients[actor].request(method, path, body, headers)
        self.assertEqual(status, expected, result)
        self.assertEqual(response_headers['Cache-Control'], 'no-store')
        return result

    def create(self, actor='engineer', fields=None, status='draft'):
        return self.call(actor, 'POST', '/api/reports',
                         {'fields': fields or {}, 'status': status, 'form_version': 'V3'}, 201)['report']

    def test_role_object_comment_and_mass_assignment_boundaries(self):
        report = self.create()
        path = '/api/reports/' + report['id']
        self.call('other', 'GET', path, expected=404)
        self.call('other', 'PUT', path, {'version': 1, 'fields': {}}, 404)
        self.call('reviewer', 'GET', path)
        self.call('reviewer', 'PUT', path, {'version': 1, 'fields': {}}, 403)
        self.call('reviewer', 'POST', '/api/reports', {}, 403)
        self.call('engineer', 'POST', path + '/comments', {'body': 'No privilege'}, 403)
        comment = self.call('reviewer', 'POST', path + '/comments', {'body': 'Synthetic review'}, 201)['comment']
        self.assertEqual(comment['author_id'], 'reviewer')
        self.call('reviewer', 'PUT', path + '/comments/' + comment['id'], {'body': 'Overwrite'}, 404)
        self.call('admin', 'PUT', path, {'version': 1, 'fields': {'legacy_column_47': value('opaque')} })
        self.call('engineer', 'POST', '/api/reports', {'owner_id': 'other', 'fields': {}}, 400)
        self.call('engineer', 'POST', '/api/reports', {'fields': {'surgery_summary': {'state': 'value', 'value': 'Synthetic', 'raw': 'Forged source'}}}, 400)
        self.call('engineer', 'PUT', path, {'version': 2, 'fields': {}, 'form_version': 'V1'}, 400)
        self.assertEqual(self.call('engineer', 'GET', path)['comments'][0]['body'], 'Synthetic review')
        self.call('engineer', 'GET', '/api/users', expected=403)
        self.call('reviewer', 'GET', '/api/audit', expected=403)

    def test_login_csrf_origin_logout_and_cookie_rotation(self):
        unauth = Client(self.server)
        self.assertEqual(unauth.request('GET', '/api/schema')[0], 401)
        self.assertEqual(unauth.request('POST', '/api/login', {'username': 'engineer', 'password': PASSWORD})[0], 403)
        _, _, headers = unauth.request('GET', '/api/session')
        before = unauth.cookie
        self.assertIn('HttpOnly', headers['Set-Cookie'])
        self.assertIn('SameSite=Strict', headers['Set-Cookie'])
        credentials = {'username': 'engineer', 'password': PASSWORD}
        self.assertEqual(unauth.request('POST', '/api/login', credentials, {'Origin': 'https://attacker.invalid'})[0], 403)
        self.assertEqual(unauth.request('POST', '/api/login', credentials, {'X-CSRF-Token': 'bad'})[0], 403)
        self.assertEqual(unauth.request('POST', '/api/login', credentials)[0], 200)
        self.assertNotEqual(before, unauth.cookie)
        replay = Client(self.server)
        replay.cookie = before
        self.assertEqual(replay.request('GET', '/api/schema')[0], 401)
        current_cookie = unauth.cookie
        self.assertEqual(unauth.request('POST', '/api/logout', {})[0], 200)
        replay.cookie = current_cookie
        self.assertEqual(replay.request('GET', '/api/schema')[0], 401)
        self.call('engineer', 'POST', '/api/reports', {'fields': {}}, 403, {'Origin': ''})
        self.call('engineer', 'GET', '/api/reports', expected=403, headers={'Host': 'attacker.invalid'})
        self.call('engineer', 'GET', '/app/database.py', expected=404)
        # Synchronize on the actual HTTP body read; no timing sleep or bypass.
        stalled = Client(self.server)
        stalled.request('GET', '/api/session')
        entered_body = threading.Event()
        completed_get = threading.Event()
        result = []
        original_body = Handler.body
        def observe_body(handler, path):
            entered_body.set()
            return original_body(handler, path)
        def independent_get():
            result.append(Client(self.server).request('GET', '/api/session')[0])
            completed_get.set()
        slow = socket.create_connection(('127.0.0.1', self.server.server_port), timeout=60)
        getter = None
        try:
            with patch.object(Handler, 'body', observe_body):
                headers = f'POST /api/login HTTP/1.1\r\nHost: 127.0.0.1:{self.server.server_port}\r\nOrigin: {self.server.origin}\r\nCookie: {stalled.cookie}\r\nX-CSRF-Token: {stalled.csrf}\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n'
                slow.sendall(headers.encode() + b'{')
                self.assertTrue(entered_body.wait(timeout=5), 'Slow request never reached body read')
                getter = threading.Thread(target=independent_get)
                getter.start()
                self.assertTrue(completed_get.wait(timeout=5), 'Incomplete request serialized unrelated API access')
                self.assertEqual(result, [200])
        finally:
            try:
                # Finish and drain the synthetic request so cleanup is graceful.
                slow.sendall(b'}')
                response = http.client.HTTPResponse(slow)
                response.begin()
                response.read()
                response.close()
            finally:
                slow.close()
            if getter:
                getter.join(timeout=60)
        entered_send = threading.Event()
        release_send = threading.Event()
        concurrent_get = threading.Event()
        sent_statuses = []
        original_send = Handler.send
        def stalled_send(handler, *args, **kwargs):
            if handler.path == '/api/schema':
                entered_send.set()
                if not release_send.wait(timeout=15):
                    raise AssertionError('Test did not release the synthetic slow receiver')
            return original_send(handler, *args, **kwargs)
        def receive_schema():
            client = Client(self.server)
            client.cookie = self.clients['engineer'].cookie
            sent_statuses.append(client.request('GET', '/api/schema')[0])
        def receive_independent_session():
            sent_statuses.append(Client(self.server).request('GET', '/api/session')[0])
            concurrent_get.set()
        with patch.object(Handler, 'send', stalled_send):
            receiver = threading.Thread(target=receive_schema)
            receiver.start()
            getter = None
            try:
                self.assertTrue(entered_send.wait(timeout=5), 'Schema never reached response send')
                getter = threading.Thread(target=receive_independent_session)
                getter.start()
                self.assertTrue(concurrent_get.wait(timeout=5), 'Slow receiving client serialized unrelated API access')
            finally:
                release_send.set()
                receiver.join(timeout=60)
                if getter:
                    getter.join(timeout=60)
        self.assertEqual(sent_statuses, [200, 200])

    def test_all_v3_fields_round_trip_conditional_validation(self):
        schema = self.call('engineer', 'GET', '/api/schema')['versions']
        self.assertEqual([len(schema[v]) for v in ('V1', 'V2', 'V3')], [33, 46, 49])
        by_column = {field['source_column']: field['id'] for field in schema['V3']}
        self.assertEqual([by_column[c] for c in ('R', 'X', 'AV')],
                         ['software_issues', 'software_issues_secondary', 'legacy_column_47'])
        fields = submission()
        for field in schema['V3']:
            if field['id'] in fields:
                continue
            sample = {'text': 'Synthetic text ' + field['source_column'], 'short_text': 'Synthetic',
                      'integer': 1000, 'decimal': 100, 'boolean': False,
                      'date': '2026-10-01', 'datetime': '2026-10-01T12:00:00',
                      'links': ['https://example.invalid/synthetic'],
                      'enum': (field.get('options') or [''])[0]}[field['type']]
            fields[field['id']] = value(sample)
        report = self.create(fields=fields, status='submitted')
        self.assertEqual(self.call('engineer', 'GET', '/api/reports/' + report['id'])['report']['fields'], fields)
        self.assertEqual(report['fields']['xray_count'], value(1000))
        for key, replacement, required in [
            ('surgery_type', 'Other', 'other_surgery_type'),
            ('system_identity', 'Other', 'other_system_identity'),
            ('surgery_success', False, 'failure_description'),
            ('navigation_usage', 'Partially', 'navigation_limitation_reason')]:
            missing = submission()
            missing[key] = value(replacement)
            result = self.call('engineer', 'POST', '/api/reports', {'fields': missing, 'status': 'submitted'}, 400)
            self.assertIn(required, result['error']['fields'])
            missing[required] = value('Synthetic explanation')
            self.create(fields=missing, status='submitted')
        for invalid in [{'invented': value('x')}, {'surgery_date': value('2026-02-30')},
                        {'xray_count': value(-1)}, {'xray_count': value(1.5)},
                        {'system_photo_links': value(['javascript:alert(1)'])},
                        {'hospital_name': {'state': 'unknown', 'value': 'not null'}}]:
            self.call('engineer', 'POST', '/api/reports', {'fields': invalid}, 400)

    def test_stale_concurrent_write_and_audit_atomicity(self):
        report = self.create()
        path = '/api/reports/' + report['id']
        barrier = threading.Barrier(2)
        outcomes = []
        def write(text):
            client = Client(self.server)
            client.cookie = self.clients['engineer'].cookie
            client.csrf = self.clients['engineer'].csrf
            barrier.wait(timeout=5)
            outcomes.append(client.request('PUT', path, {'version': 1, 'fields': {'surgery_summary': value(text)}})[0])
        threads = [threading.Thread(target=write, args=(text,)) for text in ('Synthetic first', 'Synthetic second')]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(sorted(outcomes), [200, 409])
        self.assertEqual(self.call('engineer', 'GET', path)['report']['version'], 2)
        with managed_connection(self.path) as conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM audit_events WHERE entity_id=? AND action='update'", (report['id'],)).fetchone()[0], 1)
            conn.execute("CREATE TRIGGER qa_fail_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'qa failure'); END")
        self.call('engineer', 'POST', '/api/reports', {'fields': {}}, 400)
        self.assertEqual(self.call('engineer', 'GET', '/api/reports')['total'], 1)

    def test_filters_csv_scope_formula_safety_and_boundaries(self):
        fields = submission()
        fields['hospital_name'] = value('=Synthetic Hospital')
        fields['surgery_summary'] = value(' \t@SUM(1)')
        fields['tool_tip_accuracy_mm'] = {'state': 'unknown', 'value': None}
        own = self.create(fields=fields, status='submitted')
        self.create('other', submission(), 'submitted')
        self.create(fields={'hospital_name': value('Other Synthetic'), 'surgery_date': value('2026-10-02')})
        query = '?q=Synthetic%20Hospital&date_from=2026-10-01&date_to=2026-10-01&surgery_type=Cranial&system_identity=SYN-001&system_base=Optical&status=submitted&owner_id=engineer'
        result = self.call('engineer', 'GET', '/api/reports' + query)
        self.assertEqual([r['id'] for r in result['items']], [own['id']])
        csv_bytes = self.call('engineer', 'GET', '/api/reports/export.csv' + query)
        rows = list(csv.DictReader(io.StringIO(csv_bytes.decode('utf-8-sig'))))
        self.assertEqual([r['id'] for r in rows], [own['id']])
        self.assertEqual(rows[0]['hospital_name'], "'=Synthetic Hospital")
        self.assertEqual(rows[0]['surgery_summary'], "' \t@SUM(1)")
        self.assertEqual(rows[0]['tool_tip_accuracy_mm__state'], 'unknown')
        self.assertEqual(rows[0]['tool_tip_accuracy_mm'], '')
        self.assertEqual(self.call('engineer', 'GET', '/api/reports?owner_id=other')['total'], 0)
        self.assertEqual(len(list(csv.DictReader(io.StringIO(self.call('engineer', 'GET', '/api/reports/export.csv?owner_id=other').decode('utf-8-sig'))))), 0)
        self.assertEqual(self.call('reviewer', 'GET', '/api/reports')['total'], 3)
        self.assertEqual(len(self.call('reviewer', 'GET', '/api/reports?page_size=1')['items']), 1)
        for query in ('?page=0', '?page_size=101', '?date_from=bad', '?date_from=2026-10-02&date_to=2026-10-01'):
            self.call('engineer', 'GET', '/api/reports' + query, expected=400)
        self.assertEqual(self.call('engineer', 'GET', '/api/reports?q=%25')['total'], 0)

    def test_import_preview_token_binding_all_versions_and_deduplication(self):
        schemas = self.call('admin', 'GET', '/api/schema')['versions']
        second_admin = Client(self.server)
        second_admin.login('admin')
        for version in ('V1', 'V2', 'V3'):
            content = base64.b64encode(synthetic_workbook(schemas[version], version)).decode()
            body = {'filename': 'synthetic-' + version + '.xlsx', 'content_base64': content}
            self.call('engineer', 'POST', '/api/imports/preview', body, 403)
            with managed_connection(self.path) as conn:
                before = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0]
                          for table in ('reports', 'import_batches', 'audit_events')}
            preview = self.call('admin', 'POST', '/api/imports/preview', body)
            self.assertNotIn('Synthetic Imported', json.dumps(preview))
            with managed_connection(self.path) as conn:
                after = {table: conn.execute('SELECT count(*) FROM ' + table).fetchone()[0] for table in before}
            self.assertEqual(before, after)
            commit = {'token': preview['token'], 'owner_id': 'engineer'}
            self.assertEqual(second_admin.request('POST', '/api/imports/commit', commit)[0], 400)
            self.call('admin', 'POST', '/api/imports/commit', {'token': preview['token'], 'owner_id': 'reviewer'}, 400)
            result = self.call('admin', 'POST', '/api/imports/commit', commit)
            self.assertEqual(result['imported_reports'], 1)
            self.call('admin', 'POST', '/api/imports/commit', commit, 400)
            again = self.call('admin', 'POST', '/api/imports/preview', body)
            result = self.call('admin', 'POST', '/api/imports/commit', {'token': again['token'], 'owner_id': 'engineer'})
            self.assertEqual(result['imported_reports'], 0)
            self.assertGreater(result['skipped_duplicates'], 0)
        reports = self.call('engineer', 'GET', '/api/reports')['items']
        self.assertEqual({r['form_version'] for r in reports}, {'V1', 'V2', 'V3'})
        self.assertTrue(all(r['source']['row'] == 2 and r['status'] == 'draft' for r in reports))
        historical = next(r for r in reports if r['form_version'] == 'V3')
        path = '/api/reports/' + historical['id']
        edited = self.call('engineer', 'PUT', path, {'version': 1, 'fields': {'hospital_name': value('Synthetic Revised')}})['report']
        self.assertEqual(edited['fields']['hospital_name']['raw'], 'Synthetic Imported V3')
        self.call('engineer', 'PUT', path, {'version': 2, 'fields': {'hospital_name': {'state': 'value', 'value': 'Synthetic Revised', 'raw': 'Forged source'}}}, 400)
        omitted = self.call('engineer', 'PUT', path, {'version': 2, 'fields': {}})['report']
        self.assertEqual(omitted['fields']['hospital_name'], {'state': 'not_recorded', 'value': None, 'raw': 'Synthetic Imported V3'})
        with managed_connection(self.path) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM reports').fetchone()[0], 3)
        expired = self.call('admin', 'POST', '/api/imports/preview', body)
        with self.server.lock:
            self.server.previews[expired['token']]['expires'] = 0
        self.call('admin', 'POST', '/api/imports/commit', {'token': expired['token'], 'owner_id': 'engineer'}, 400)
        preview = self.call('admin', 'POST', '/api/imports/preview', body)
        barrier = threading.Barrier(2)
        outcomes = []
        def commit_once():
            client = Client(self.server)
            client.cookie = self.clients['admin'].cookie
            client.csrf = self.clients['admin'].csrf
            barrier.wait(timeout=5)
            outcomes.append(client.request('POST', '/api/imports/commit', {'token': preview['token'], 'owner_id': 'engineer'})[0])
        threads = [threading.Thread(target=commit_once) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        self.assertEqual(sorted(outcomes), [200, 400])

    def test_admin_inventory_permissions_states_versions_and_ledger(self):
        for role in ('engineer', 'reviewer'):
            self.call(role, 'POST', '/api/systems', {'identity': 'SYN-001', 'name': 'Synthetic'}, 403)
        system = self.call('admin', 'POST', '/api/systems', {'identity': 'SYN-001', 'name': 'Synthetic'}, 201)['system']
        path = '/api/systems/' + system['id']
        self.call('admin', 'PUT', path, {'version': 1, 'readiness': 'ready'})
        self.call('admin', 'PUT', path, {'version': 1, 'readiness': 'not_ready'}, 409)
        self.call('admin', 'PUT', path, {'version': 2, 'readiness': 'blue'}, 400)
        item = self.call('admin', 'POST', path + '/inventory', {'name': 'Synthetic instrument', 'availability': 'available', 'quantity': None}, 201)['item']
        self.assertIsNone(item['quantity'])
        self.call('engineer', 'PUT', '/api/inventory/' + item['id'], {'version': 1, 'quantity': 0}, 403)
        self.call('admin', 'PUT', '/api/inventory/' + item['id'], {'version': 1, 'quantity': 0})
        self.call('admin', 'PUT', '/api/inventory/' + item['id'], {'version': 1, 'quantity': 4}, 409)
        self.call('admin', 'POST', path + '/inventory', {'name': 'Synthetic invalid', 'quantity': -1}, 400)
        self.call('admin', 'POST', path + '/movements', {'item_name': 'Synthetic ledger', 'quantity': 5, 'reference_note': 'Synthetic reference'}, 201)
        inventory = self.call('engineer', 'GET', path + '/inventory')
        self.assertEqual(inventory['items'][0]['quantity'], 0)
        self.assertEqual(inventory['movements'][0]['quantity'], 5)
        self.assertIsNone(inventory['movements'][0]['cost'])

    def test_user_management_immediate_invalidation_last_admin_and_redacted_audit(self):
        users = self.call('admin', 'GET', '/api/users')['items']
        self.assertTrue(all(set(u) == {'id', 'username', 'role', 'active'} for u in users))
        self.call('admin', 'PUT', '/api/users/admin', {'active': False}, 400)
        self.call('admin', 'PUT', '/api/users/admin', {'role': 'engineer'}, 400)
        self.call('admin', 'PUT', '/api/users/other', {'role': 'reviewer'})
        self.call('other', 'GET', '/api/reports', expected=401)
        self.clients['other'].login('other')
        self.call('admin', 'PUT', '/api/users/other', {'password': 'New-synthetic-password!123'})
        self.call('other', 'GET', '/api/reports', expected=401)
        self.call('admin', 'PUT', '/api/users/engineer', {'active': False})
        self.call('engineer', 'GET', '/api/schema', expected=401)
        new_user = self.call('admin', 'POST', '/api/users', {'username': 'synthetic-new', 'password': PASSWORD, 'role': 'engineer'}, 201)['user']
        self.assertNotIn('password_hash', new_user)
        audit = self.call('admin', 'GET', '/api/audit?page_size=100')['items']
        serialized = json.dumps(audit)
        self.assertNotIn(PASSWORD, serialized)
        self.assertNotIn('New-synthetic-password!123', serialized)
        self.assertNotIn(self.clients['admin'].csrf, serialized)


class ParserAbuseAcceptanceTests(unittest.TestCase):
    def mutate_archive(self, replacements=None, additions=None):
        source = synthetic_workbook(get_schema()['versions']['V3'], 'V3')
        output = io.BytesIO()
        with zipfile.ZipFile(io.BytesIO(source)) as old, zipfile.ZipFile(output, 'w') as new:
            for name in old.namelist():
                new.writestr(name, (replacements or {}).get(name, old.read(name)))
            for name, data in (additions or []):
                with warnings.catch_warnings():
                    warnings.simplefilter('ignore', UserWarning)
                    new.writestr(name, data)
        return output.getvalue()

    def test_dtd_entities_ascii_and_utf16_rejected(self):
        document = '<?xml version="1.0"?><!DOCTYPE workbook [<!ENTITY qa "synthetic">]><workbook>&qa;</workbook>'
        for xml in (document.encode(), document.replace('version="1.0"', 'version="1.0" encoding="UTF-16"').encode('utf-16')):
            with self.subTest(encoding=xml[:8]):
                with self.assertRaises(ValueError):
                    importer.preview_workbook(self.mutate_archive({'xl/workbook.xml': xml}), 'synthetic.xlsx')

    def test_duplicate_macro_and_unsafe_member_rejected(self):
        for name in ('xl/workbook.xml', 'xl/vbaProject.bin', '../escape.xml', '/absolute.xml'):
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    importer.preview_workbook(self.mutate_archive(additions=[(name, 'synthetic')]), 'synthetic.xlsx')

    def test_invalid_archive_and_unsupported_layout_rejected(self):
        with self.assertRaises(ValueError):
            importer.preview_workbook(b'not a workbook', 'synthetic.xlsx')
        with self.assertRaises(ValueError):
            importer.preview_workbook(self.mutate_archive({'xl/worksheets/sheet1.xml': '<worksheet/>'}), 'synthetic.xlsx')

    def test_shared_string_expansion_count_and_byte_budgets(self):
        fields = get_schema()['versions']['V3']
        headers = ''.join(f'<c r="{f["source_column"]}1" t="inlineStr"><is><t>{escape(f["label"])}</t></is></c>' for f in fields)
        repeated = ''.join(f'<c r="{column}2" t="s"><v>0</v></c>' for column in ('D', 'E', 'F', 'G'))
        sheet = '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1">' + headers + '</row><row r="2">' + repeated + '</row></sheetData></worksheet>'
        strings = '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><si><t>' + 'S' * 1000 + '</t></si></sst>'
        data = self.mutate_archive({'xl/worksheets/sheet1.xml': sheet}, [('xl/sharedStrings.xml', strings)])
        header_bytes = sum(len(f['label'].encode('utf-8')) for f in fields)
        for name, budget in [('MAX_CELLS', len(fields) + 2),
                             ('MAX_DECODED_TEXT_BYTES', header_bytes + 2500)]:
            with self.subTest(budget=name), patch.object(importer, name, budget):
                with self.assertRaisesRegex(ValueError, 'logical cell/text budget'):
                    importer.preview_workbook(data, 'synthetic.xlsx')

    def test_proposal_budget_and_oversized_cell_fail_explicitly(self):
        data = synthetic_workbook(get_schema()['versions']['V3'], 'V3')
        with patch.object(importer, 'MAX_PROPOSAL_BYTES', 1):
            with self.assertRaisesRegex(ValueError, 'proposal memory budget'):
                importer.preview_workbook(data, 'synthetic.xlsx')
        old = zipfile.ZipFile(io.BytesIO(data))
        try:
            sheet = old.read('xl/worksheets/sheet1.xml').decode().replace('Synthetic Imported V3', 'S' * 10001)
        finally:
            old.close()
        with self.assertRaisesRegex(ValueError, r'10000 characters.*Form responses 1.*D2'):
            importer.preview_workbook(self.mutate_archive({'xl/worksheets/sheet1.xml': sheet}), 'synthetic.xlsx')


if __name__ == '__main__':
    unittest.main()
