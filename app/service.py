"""Validated application operations; every mutation includes an audit event."""
import csv
import io
import json
import math
import re
import uuid
from datetime import date, datetime
from urllib.parse import urlsplit
from .auth import now, password_hash
from .schema import get_schema


class APIError(Exception):
    def __init__(self, status, code, message, fields=None):
        self.status, self.code, self.message, self.fields = status, code, message, fields
        super().__init__(message)


def fail(message, status=400, code='validation'):
    raise APIError(status, code, message)


def require_role(user, *roles):
    if user['role'] not in roles:
        fail('Permission denied', 403, 'forbidden')


def object_body(body, allowed):
    if not isinstance(body, dict) or set(body) - set(allowed):
        fail('Invalid request fields')


def text(value, limit=250, required=False):
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        fail('Invalid text value')
    return value


def integer(value, minimum=0, maximum=2147483647):
    if type(value) is not int or not minimum <= value <= maximum:
        fail('Invalid integer value')
    return value


def valid_date(value):
    try:
        if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d{2}-\d{2}', value):
            fail('Date must use YYYY-MM-DD')
        date.fromisoformat(value)
        return value
    except ValueError:
        fail('Invalid date')


def audit(conn, actor, action, entity, identifier, fields):
    conn.execute('INSERT INTO audit_events(id,actor_id,action,entity_type,entity_id,changed_fields_json,created_at) VALUES(?,?,?,?,?,?,?)',
                 (str(uuid.uuid4()), actor, action, entity, identifier, json.dumps(list(fields)), now()))


def validate_fields(version, fields, status, *, allow_raw=False):
    if version not in ('V1', 'V2', 'V3') or status not in ('draft', 'submitted'):
        fail('Invalid form version or status')
    metadata = {f['id']: f for f in get_schema()['versions'][version]}
    if not isinstance(fields, dict) or set(fields) - metadata.keys():
        fail('Unknown report fields')
    for key, cell in fields.items():
        object_body(cell, ('state', 'value', 'raw'))
        state, value = cell.get('state'), cell.get('value')
        if state not in ('value', 'unknown', 'not_applicable', 'not_recorded'):
            fail('Invalid field state')
        if 'raw' in cell:
            if not allow_raw:
                fail('Raw provenance is allowed only for historical imports')
            text(cell['raw'], 10000)
        if state != 'value':
            if value is not None:
                fail('Non-value states require null value')
            continue
        kind = metadata[key]['type']
        if kind == 'boolean':
            if type(value) is not bool:
                fail('Boolean field requires true or false')
        elif kind in ('integer', 'decimal'):
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                fail('Numeric field requires finite nonnegative value')
            if kind == 'integer':
                integer(value)
        elif kind == 'date':
            valid_date(value)
        elif kind == 'datetime':
            text(value, 250)
            try:
                datetime.fromisoformat(value.replace('Z', '+00:00'))
            except ValueError:
                fail('Invalid datetime')
        elif kind == 'links':
            if not isinstance(value, list) or len(value) > 10:
                fail('Links require an array of at most ten HTTPS URLs')
            for link in value:
                text(link, 2048, True)
                parsed = urlsplit(link)
                if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or any(ord(c) < 32 for c in link):
                    fail('Invalid HTTPS link')
        else:
            text(value, 10000 if kind == 'text' else 250)
            if kind == 'enum' and value not in metadata[key].get('options', []):
                fail('Invalid option')
    def val(key):
        cell = fields.get(key, {})
        return cell.get('value') if cell.get('state') == 'value' else None
    if status == 'submitted':
        required = [f['id'] for f in metadata.values() if f.get('required')]
        if version == 'V3':
            required += ['surgery_date', 'hospital_name', 'city', 'surgeon_name', 'application_engineers', 'surgery_type', 'surgery_success', 'navigation_usage', 'system_base', 'software_version', 'system_identity']
        if val('surgery_type') == 'Other':
            required.append('other_surgery_type')
        if val('system_identity') == 'Other':
            required.append('other_system_identity')
        if val('surgery_success') is False:
            required.append('failure_description')
        if val('navigation_usage') in ('Partially', 'Not used'):
            required.append('navigation_limitation_reason')
        missing = [key for key in set(required) if val(key) is None or (isinstance(val(key), str) and not val(key).strip())]
        if missing:
            raise APIError(400, 'validation', 'Required submission fields are missing', {key: 'required' for key in missing})
    return fields


def report(row):
    result = dict(row)
    result['fields'] = json.loads(result.pop('fields_json'))
    result['source'] = json.loads(result.pop('source_json') or 'null')
    return result


def accessible_report(conn, identifier, user):
    row = conn.execute('SELECT * FROM reports WHERE id=?', (identifier,)).fetchone()
    if row is None or (user['role'] == 'engineer' and row['owner_id'] != user['id']):
        fail('Report not found', 404, 'not_found')
    return row


def create_report(conn, user, body):
    require_role(user, 'engineer', 'admin')
    object_body(body, ('form_version', 'fields', 'status'))
    version, status = body.get('form_version', 'V3'), body.get('status', 'draft')
    fields = validate_fields(version, body.get('fields', {}), status)
    identifier, timestamp = str(uuid.uuid4()), now()
    with conn:
        conn.execute('INSERT INTO reports(id,owner_id,form_version,status,fields_json,version,created_at,updated_at) VALUES(?,?,?,?,?,1,?,?)', (identifier, user['id'], version, status, json.dumps(fields), timestamp, timestamp))
        audit(conn, user['id'], 'create', 'report', identifier, fields.keys())
    return report(accessible_report(conn, identifier, user))


def update_report(conn, user, identifier, body):
    require_role(user, 'engineer', 'admin')
    old = accessible_report(conn, identifier, user)
    object_body(body, ('fields', 'status', 'version'))
    version = integer(body.get('version'), 1)
    status = body.get('status', old['status'])
    fields = body.get('fields', {})
    if not isinstance(fields, dict):
        fail('Invalid report fields')
    original = json.loads(old['fields_json'])
    # A supplied raw value may only echo immutable stored provenance. Restore
    # omitted raw values server-side even when its normalized field is cleared.
    fields = {key: dict(cell) if isinstance(cell, dict) else cell for key, cell in fields.items()}
    for key, cell in fields.items():
        if isinstance(cell, dict) and 'raw' in cell:
            previous = original.get(key, {})
            if 'raw' not in previous or cell['raw'] != previous['raw']:
                fail('Raw import provenance cannot be introduced or changed')
    for key, cell in original.items():
        if 'raw' in cell:
            if key not in fields:
                fields[key] = {'state': 'not_recorded', 'value': None}
            if not isinstance(fields[key], dict):
                fail('Invalid report field')
            fields[key]['raw'] = cell['raw']
    fields = validate_fields(old['form_version'], fields, status, allow_raw=True)
    with conn:
        cursor = conn.execute('UPDATE reports SET fields_json=?,status=?,version=version+1,updated_at=? WHERE id=? AND version=?', (json.dumps(fields), status, now(), identifier, version))
        if cursor.rowcount != 1:
            fail('Report changed; reload before saving', 409, 'stale_version')
        audit(conn, user['id'], 'update', 'report', identifier, list(fields) + ['status'])
    return report(accessible_report(conn, identifier, user))


def pagination(query):
    try:
        return integer(int(query.get('page', '1')), 1), integer(int(query.get('page_size', '20')), 1, 100)
    except (ValueError, TypeError):
        fail('Invalid pagination')


def list_reports(conn, user, query, export=False):
    clauses, args = [], []
    if user['role'] == 'engineer':
        clauses.append('owner_id=?')
        args.append(user['id'])
    if query.get('owner_id'):
        clauses.append('owner_id=?')
        args.append(query['owner_id'])
    if query.get('status'):
        if query['status'] not in ('draft', 'submitted'):
            fail('Invalid status filter')
        clauses.append('status=?')
        args.append(query['status'])
    for key in ('surgery_type', 'system_identity', 'system_base'):
        if query.get(key):
            clauses.append(f"json_extract(fields_json,'$.{key}.value')=?")
            args.append(query[key])
    for key, op in (('date_from', '>='), ('date_to', '<=')):
        if query.get(key):
            clauses.append(f"json_extract(fields_json,'$.surgery_date.value'){op}?")
            args.append(valid_date(query[key]))
    if query.get('date_from') and query.get('date_to') and query['date_from'] > query['date_to']:
        fail('Invalid date range')
    if query.get('q'):
        term = text(query['q'], 250).replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
        clauses.append('(' + ' OR '.join(f"json_extract(fields_json,'$.{key}.value') LIKE ? ESCAPE '\\'" for key in ('hospital_name', 'city', 'surgeon_name', 'system_identity')) + ')')
        args.extend(['%' + term + '%'] * 4)
    where = ' WHERE ' + ' AND '.join(clauses) if clauses else ''
    total = conn.execute('SELECT count(*) FROM reports' + where, args).fetchone()[0]
    page, size = pagination(query)
    if export and total > 10000:
        fail('Export exceeds 10000 rows; narrow filters', 413, 'export_limit')
    rows = conn.execute('SELECT * FROM reports' + where + ' ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?', args + ([10000, 0] if export else [size, (page - 1) * size])).fetchall()
    return {'items': [report(row) for row in rows], 'total': total, 'page': page, 'page_size': size}


def safe_csv(value):
    value = '' if value is None else str(value)
    if value and (ord(value[0]) < 32 or value.lstrip().startswith(('=', '+', '-', '@'))):
        return "'" + value
    return value


def export_reports(conn, user, query):
    rows = list_reports(conn, user, query, True)['items']
    field_ids = list(dict.fromkeys(f['id'] for fields in get_schema()['versions'].values() for f in fields))
    base = ['id', 'owner_id', 'form_version', 'status', 'version', 'created_at', 'updated_at', 'source']
    output = io.StringIO(newline='')
    writer = csv.writer(output)
    writer.writerow(base + [column for key in field_ids for column in (key, key + '__state')])
    for row in rows:
        values = [json.dumps(row[key], ensure_ascii=False) if key == 'source' else row[key] for key in base]
        for key in field_ids:
            cell = row['fields'].get(key, {'state': 'not_recorded', 'value': None})
            value = cell.get('value')
            values += [json.dumps(value, ensure_ascii=False) if isinstance(value, list) else value, cell['state']]
        writer.writerow([safe_csv(value) for value in values])
    return output.getvalue().encode('utf-8-sig')


RESOURCE_FIELDS = {
    'systems': ('identity', 'name', 'city', 'hospital', 'application_experts', 'issues', 'readiness'),
    'inventory_items': ('category', 'name', 'serial_number', 'quantity', 'availability', 'remarks'),
    'inventory_movements': ('item_name', 'event_date', 'quantity', 'provided_by', 'reference_note', 'cost'),
}


def mutate_resource(conn, user, table, body, identifier=None, system_id=None):
    require_role(user, 'admin')
    allowed = RESOURCE_FIELDS[table]
    object_body(body, allowed + (('version',) if identifier else ()))
    if identifier:
        if not conn.execute(f'SELECT id FROM {table} WHERE id=?', (identifier,)).fetchone():
            fail('Object not found', 404, 'not_found')
        expected = integer(body.get('version'), 1)
    if system_id and not conn.execute('SELECT id FROM systems WHERE id=?', (system_id,)).fetchone():
        fail('System not found', 404, 'not_found')
    values = {key: body[key] for key in allowed if key in body}
    if not identifier:
        defaults = {'systems': {'identity': '', 'name': '', 'city': '', 'hospital': '', 'application_experts': '', 'issues': '', 'readiness': 'unknown'}, 'inventory_items': {'category': 'Other components', 'name': '', 'serial_number': None, 'quantity': None, 'availability': 'unknown', 'remarks': ''}, 'inventory_movements': {'item_name': '', 'event_date': None, 'quantity': None, 'provided_by': '', 'reference_note': '', 'cost': None}}
        values = defaults[table] | values
    for key, value in values.items():
        if key == 'quantity':
            if value is not None:
                integer(value)
        elif key == 'cost':
            if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                fail('Invalid cost')
        elif key == 'event_date':
            if value is not None:
                valid_date(value)
        elif key == 'serial_number' and value is None:
            pass
        else:
            text(value, 10000 if key in ('issues', 'remarks', 'reference_note') else 250, key in ('identity', 'name', 'item_name'))
    enums = {'readiness': ('ready', 'not_ready', 'unknown'), 'availability': ('available', 'unavailable', 'unknown'), 'category': ('Hardware', 'Cranial instruments', 'Spine instruments', 'Other components')}
    for key, options in enums.items():
        if key in values and values[key] not in options:
            fail('Invalid ' + key)
    timestamp = now()
    with conn:
        if identifier:
            values['updated_at'] = timestamp
            assignments = ','.join(key + '=?' for key in values)
            cursor = conn.execute(f'UPDATE {table} SET {assignments},version=version+1 WHERE id=? AND version=?', list(values.values()) + [identifier, expected])
            if cursor.rowcount != 1:
                fail('Object changed; reload before saving', 409, 'stale_version')
        else:
            identifier = str(uuid.uuid4())
            values |= {'id': identifier, 'created_at': timestamp}
            if table != 'inventory_movements':
                values |= {'updated_at': timestamp, 'version': 1}
            if system_id:
                values['system_id'] = system_id
            conn.execute(f"INSERT INTO {table}({','.join(values)}) VALUES({','.join('?' for _ in values)})", list(values.values()))
        audit(conn, user['id'], 'update' if 'version' in body else 'create', table, identifier, body.keys())
    return dict(conn.execute(f'SELECT * FROM {table} WHERE id=?', (identifier,)).fetchone())


def public_user(row):
    return {key: row[key] for key in ('id', 'username', 'role', 'active')}


def mutate_user(conn, actor, body, identifier=None):
    require_role(actor, 'admin')
    object_body(body, ('role', 'active', 'password') if identifier else ('username', 'password', 'role'))
    values = {}
    if 'role' in body:
        if body['role'] not in ('engineer', 'reviewer', 'admin'):
            fail('Invalid role')
        values['role'] = body['role']
    if 'active' in body:
        if type(body['active']) not in (bool, int) or body['active'] not in (0, 1):
            fail('Invalid active state')
        values['active'] = int(body['active'])
    if 'password' in body:
        try:
            values['password_hash'] = password_hash(body['password'])
        except ValueError as error:
            fail(str(error))
    with conn:
        conn.execute('BEGIN IMMEDIATE')
        if identifier:
            old = conn.execute('SELECT * FROM users WHERE id=?', (identifier,)).fetchone()
            if not old:
                fail('User not found', 404, 'not_found')
            if old['role'] == 'admin' and old['active'] and (values.get('role', 'admin') != 'admin' or not values.get('active', 1)):
                if conn.execute("SELECT count(*) FROM users WHERE active=1 AND role='admin'").fetchone()[0] <= 1:
                    fail('Cannot remove last active admin')
            if values:
                conn.execute('UPDATE users SET ' + ','.join(key + '=?' for key in values) + ' WHERE id=?', list(values.values()) + [identifier])
                conn.execute('DELETE FROM sessions WHERE user_id=?', (identifier,))
        else:
            if 'password_hash' not in values:
                fail('Password required')
            username = text(body.get('username'), 100, True)
            identifier = str(uuid.uuid4())
            conn.execute('INSERT INTO users(id,username,password_hash,role,active,created_at) VALUES(?,?,?,?,1,?)', (identifier, username, values['password_hash'], values.get('role', 'engineer'), now()))
        audit(conn, actor['id'], 'update' if identifier and 'username' not in body else 'create', 'user', identifier, body.keys())
    return public_user(conn.execute('SELECT * FROM users WHERE id=?', (identifier,)).fetchone())
