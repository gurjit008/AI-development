"""Synthetic backend validation and HTTP security tests."""
import csv
import base64
from contextlib import closing
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import uuid
from unittest.mock import patch
from pathlib import Path
from app import auth, database, service
from app.server import Application, Handler


class BackendTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.test_digest = auth.password_hash('Synthetic-test-password!')

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / 'test.sqlite3')
        database.initialize(self.path)
        self.conn = database.connect(self.path)
        self.users = {}
        for role in ('admin', 'engineer', 'reviewer'):
            user = {'id': str(uuid.uuid4()), 'username': 'synthetic_' + role, 'role': role}
            self.conn.execute('INSERT INTO users VALUES(?,?,?,?,1,?)', (user['id'], user['username'], self.test_digest, role, auth.now()))
            self.users[role] = user
        self.conn.commit()

    def tearDown(self):
        self.conn.close()
        self.temp.cleanup()

    def test_report_authorization_versions_persistence(self):
        engineer = self.users['engineer']
        record = service.create_report(self.conn, engineer, {'fields': {'hospital_name': {'state': 'value', 'value': 'Synthetic hospital'}}})
        outsider = dict(engineer, id=str(uuid.uuid4()))
        with self.assertRaises(service.APIError) as raised:
            service.accessible_report(self.conn, record['id'], outsider)
        self.assertEqual(raised.exception.status, 404)
        with self.assertRaises(service.APIError) as raised:
            service.update_report(self.conn, self.users['reviewer'], record['id'], {'fields': {}, 'version': 1})
        self.assertEqual(raised.exception.status, 403)
        updated = service.update_report(self.conn, engineer, record['id'], {'fields': record['fields'], 'version': 1})
        self.assertEqual(updated['version'], 2)
        with self.assertRaises(service.APIError) as raised:
            service.update_report(self.conn, engineer, record['id'], {'fields': {}, 'version': 1})
        self.assertEqual(raised.exception.status, 409)
        with closing(database.connect(self.path)) as reopened:
            self.assertEqual(service.report(service.accessible_report(reopened, record['id'], engineer))['fields'], record['fields'])
        self.assertEqual(self.conn.execute("SELECT count(*) FROM audit_events WHERE entity_type='report'").fetchone()[0], 2)

    def test_validation_and_conditional_submission(self):
        for field, value in [('xray_count', True), ('xray_count', -1), ('tool_tip_accuracy_mm', float('nan')), ('system_photo_links', ['http://unsafe.example'])]:
            with self.subTest(field=field), self.assertRaises(service.APIError):
                service.validate_fields('V3', {field: {'state': 'value', 'value': value}}, 'draft')
        service.validate_fields('V3', {'xray_count': {'state': 'unknown', 'value': None}}, 'draft')
        fields = {key: {'state': 'value', 'value': value} for key, value in {'surgery_date': '2026-01-01', 'hospital_name': 'Synthetic', 'city': 'Synthetic', 'surgeon_name': 'Synthetic', 'application_engineers': 'Synthetic', 'surgery_type': 'Other', 'surgery_success': False, 'navigation_usage': 'Partially', 'system_base': 'Optical', 'software_version': 'Test', 'system_identity': 'Other'}.items()}
        with self.assertRaises(service.APIError) as raised:
            service.validate_fields('V3', fields, 'submitted')
        self.assertEqual(set(raised.exception.fields), {'other_surgery_type', 'other_system_identity', 'failure_description', 'navigation_limitation_reason'})
        fields.update({key: {'state': 'value', 'value': 'Synthetic'} for key in raised.exception.fields})
        service.validate_fields('V3', fields, 'submitted')

    def test_csv_scope_formula_and_filters(self):
        engineer = self.users['engineer']
        service.create_report(self.conn, engineer, {'fields': {'hospital_name': {'state': 'value', 'value': ' =1+1'}, 'city': {'state': 'value', 'value': '100%_Synthetic'}}})
        service.create_report(self.conn, self.users['admin'], {'fields': {}})
        result = service.list_reports(self.conn, engineer, {'q': '100%_'})
        self.assertEqual(result['total'], 1)
        rows = list(csv.DictReader(io.StringIO(service.export_reports(self.conn, engineer, {}).decode('utf-8-sig'))))
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['hospital_name'], "' =1+1")
        self.assertEqual(rows[0]['xray_count__state'], 'not_recorded')

    def test_last_admin_and_inventory_integrity(self):
        admin = self.users['admin']
        with self.assertRaises(service.APIError):
            service.mutate_user(self.conn, admin, {'active': False}, admin['id'])
        system = service.mutate_resource(self.conn, admin, 'systems', {'identity': 'synthetic-system', 'name': 'Synthetic'})
        item = service.mutate_resource(self.conn, admin, 'inventory_items', {'name': 'Synthetic item', 'availability': 'unavailable', 'quantity': None}, system_id=system['id'])
        self.assertIsNone(item['quantity'])
        with self.assertRaises(service.APIError):
            service.mutate_resource(self.conn, admin, 'inventory_items', {'quantity': -1, 'version': 1}, identifier=item['id'])

    def test_raw_provenance_native_denial_and_import_edit_preservation(self):
        engineer = self.users['engineer']
        historical = {'xray_count': {'state': 'unknown', 'value': None, 'raw': '1000'}}
        with self.assertRaises(service.APIError):
            service.create_report(self.conn, engineer, {'fields': historical})
        service.validate_fields('V3', historical, 'draft', allow_raw=True)
        record = service.create_report(self.conn, engineer, {'fields': {}})
        # Represents the fields written by the explicit importer, not native API.
        with self.conn:
            self.conn.execute('UPDATE reports SET fields_json=? WHERE id=?', (json.dumps(historical), record['id']))
        with self.assertRaises(service.APIError):
            service.update_report(self.conn, engineer, record['id'], {'version': 1, 'fields': {'xray_count': {'state': 'value', 'value': 4, 'raw': '4'}}})
        with self.assertRaises(service.APIError):
            service.update_report(self.conn, engineer, record['id'], {'version': 1, 'fields': {'city': {'state': 'value', 'value': 'Synthetic', 'raw': 'Invented'}}})
        updated = service.update_report(self.conn, engineer, record['id'], {'version': 1, 'fields': {'xray_count': {'state': 'value', 'value': 4}}})
        self.assertEqual(updated['fields']['xray_count'], {'state': 'value', 'value': 4, 'raw': '1000'})
        cleared = service.update_report(self.conn, engineer, record['id'], {'version': 2, 'fields': {}})
        self.assertEqual(cleared['fields']['xray_count'], {'state': 'not_recorded', 'value': None, 'raw': '1000'})
        echoed = service.update_report(self.conn, engineer, record['id'], {'version': 3, 'fields': cleared['fields']})
        self.assertEqual(echoed['fields'], cleared['fields'])

    def test_preview_aggregate_budget_and_expired_reclamation(self):
        app = Application.__new__(Application)
        app.previews = {}
        proposal = {'synthetic': 'a' * 100}
        size = len(json.dumps(proposal, ensure_ascii=False).encode())
        app.PREVIEW_BUDGET = size * 2
        token = app.stage_preview(proposal, 'synthetic-session')
        app.stage_preview(proposal, 'synthetic-session')
        with self.assertRaises(service.APIError) as raised:
            app.stage_preview(proposal, 'synthetic-session')
        self.assertEqual(raised.exception.code, 'preview_limit')
        self.assertEqual(len(app.previews), 2)
        app.previews[token]['expires'] = time.time() - 1
        app.stage_preview(proposal, 'synthetic-session')
        self.assertNotIn(token, app.previews)
        self.assertEqual(len(app.previews), 2)

    def test_import_errors_expose_only_bounded_validation_details(self):
        handler = Handler.__new__(Handler)
        handler.command = 'POST'
        body = {'filename': 'synthetic.xlsx', 'content_base64': base64.b64encode(b'synthetic').decode()}
        cases = [
            ('Workbook exceeds 10000 rows', 'Workbook exceeds 10000 rows'),
            ('Cell text exceeds 10000 characters in sheet Synthetic at AB12', 'Cell text exceeds 10000 characters at AB12'),
            ('Sensitive raw data or private filesystem path', 'Invalid or unsupported XLSX workbook'),
        ]
        for supplied, expected in cases:
            with self.subTest(supplied=supplied), patch('app.server.importer.preview_workbook', side_effect=ValueError(supplied)):
                with self.assertRaises(service.APIError) as raised:
                    handler.dispatch(self.conn, self.users['admin'], {}, '/api/imports/preview', {}, body)
                self.assertEqual(raised.exception.code, 'import_validation')
                self.assertEqual(raised.exception.message, expected)

    def test_disconnected_error_response_is_closed_cleanly(self):
        handler = Handler.__new__(Handler)
        handler.close_connection = False
        with patch.object(handler, 'write_response', side_effect=ConnectionAbortedError('Disconnected')):
            handler.send(400, {'error': {'code': 'validation', 'message': 'Synthetic'}})
        self.assertTrue(handler.close_connection)

    def test_http_csrf_rotation_logout_and_host(self):
        server = Application(('127.0.0.1', 0), self.path)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        client = http.client.HTTPConnection('127.0.0.1', server.server_port)
        try:
            client.request('GET', '/api/session')
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            cookie = response.getheader('Set-Cookie').split(';')[0]
            session = json.loads(response.read())
            headers = {'Cookie': cookie, 'Content-Type': 'application/json', 'Origin': server.origin, 'X-CSRF-Token': 'wrong'}
            credentials = json.dumps({'username': self.users['engineer']['username'], 'password': 'Synthetic-test-password!'})
            client.request('POST', '/api/login', credentials, headers)
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
            headers['X-CSRF-Token'] = session['csrf_token']
            client.request('POST', '/api/login', credentials, headers)
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            new_cookie = response.getheader('Set-Cookie').split(';')[0]
            logged_in = json.loads(response.read())
            self.assertNotEqual(new_cookie, cookie)
            headers |= {'Cookie': new_cookie, 'X-CSRF-Token': logged_in['csrf_token']}
            client.request('POST', '/api/logout', '{}', headers)
            response = client.getresponse()
            self.assertEqual(response.status, 200)
            response.read()
            client.request('GET', '/api/reports', headers={'Cookie': new_cookie})
            response = client.getresponse()
            self.assertEqual(response.status, 401)
            response.read()
            client.request('GET', '/api/session', headers={'Host': 'evil.example'})
            response = client.getresponse()
            self.assertEqual(response.status, 403)
            response.read()
        finally:
            client.close()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()
