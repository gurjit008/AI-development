"""Bounded XLSX parsing; dry-run proposals and atomic, idempotent imports."""
import hashlib
import io
import json
import math
import posixpath
import re
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from xml.etree import ElementTree as ET
from .schema import get_schema

NS = '{http://schemas.openxmlformats.org/spreadsheetml/2006/main}'
REL = '{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id'
MAX_BYTES = 16 * 1024 * 1024
MAX_CELL_TEXT = 10000
MAX_CELLS = 250000
MAX_DECODED_TEXT_BYTES = 8 * 1024 * 1024
MAX_PROPOSAL_BYTES = 32 * 1024 * 1024

def _xml(archive, name):
    data = archive.read(name)
    if re.search(br'<!\s*(DOCTYPE|ENTITY)', data, re.I) or b'\x00' in data:
        raise ValueError('Unsafe XML declaration or encoding')
    return ET.fromstring(data)

def _text(node):
    return ''.join(t.text or '' for t in node.iter(NS + 't'))

def _number(value):
    try:
        n = float(value)
        return n if math.isfinite(n) and n >= 0 else None
    except (ValueError, TypeError):
        return None

def _date(value, epoch1904, timestamp=False):
    try:
        number = float(value)
        if not math.isfinite(number) or not 0 <= number <= 2958465:
            raise ValueError('Out-of-range Excel date')
        # Excel's nonexistent 1900-02-29 is intentionally rejected.
        if not epoch1904 and 60 <= number < 61:
            raise ValueError('Invalid Excel leap day')
        base = datetime(1904, 1, 1) if epoch1904 else datetime(1899, 12, 31)
        result = base + timedelta(days=number if epoch1904 or number < 60 else number - 1)
        return result.isoformat() if timestamp else result.date().isoformat()
    except (ValueError, OverflowError):
        pass
    value = str(value).strip()
    try:
        result = datetime.fromisoformat(value.replace('Z', '+00:00'))
        return result.isoformat() if timestamp else result.date().isoformat()
    except ValueError:
        pass
    for fmt in ('%d/%m/%Y', '%d/%m/%Y %H:%M:%S', '%m/%d/%Y', '%d-%m-%Y'):
        try:
            result = datetime.strptime(value, fmt)
            return result.isoformat() if timestamp else result.date().isoformat()
        except ValueError:
            pass
    return None

def _fingerprint(kind, sheet, cells):
    stable_cells = {column: {'raw': cell['raw'], 'formula': cell['formula']} for column, cell in cells.items()}
    raw = json.dumps([kind, sheet, stable_cells], sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()

def _cell(field, raw, formula, epoch, warn):
    result = {'state': 'not_recorded', 'value': None, 'raw': raw}
    text = str(raw).strip()
    if not text:
        return result
    if formula:
        warn('Formula cell preserved as unknown; formulas are never evaluated')
        result['state'] = 'unknown'
        return result
    result['state'] = 'value'
    typ = field['type']
    if text.casefold() in ('na', 'n/a', 'none'):
        label = field['label'].casefold()
        result['state'] = 'not_applicable' if any(s in label for s in ('no issues', 'no feedback', 'not given', 'not seen', 'no information')) else 'unknown'
    elif typ in ('integer', 'decimal'):
        value = _number(text)
        if value is None or (typ == 'integer' and not value.is_integer()) or value in field.get('legacy_sentinels', []):
            result['state'] = 'unknown'
            warn('Unavailable sentinel or invalid numeric value preserved without measurement')
        else:
            result['value'] = int(value) if typ == 'integer' else value
    elif typ in ('date', 'datetime'):
        result['value'] = _date(text, epoch, typ == 'datetime')
        if result['value'] is None:
            result['state'] = 'unknown'
            warn('Unrecognized historical date retained in raw')
        elif typ == 'datetime':
            warn('Historical timestamp timezone unspecified; no timezone assumed')
    elif typ == 'boolean':
        if text.casefold() in ('yes', 'true', '1'):result['value'] = True
        elif text.casefold() in ('no', 'false', '0'):result['value'] = False
        else:result['state'] = 'unknown'; warn('Unmatched historical boolean preserved')
    elif typ == 'enum':
        options = {x.casefold(): x for x in field['options']}
        if text.casefold() in options:result['value'] = options[text.casefold()]
        else:result['state'] = 'unknown'; warn('Unmatched historical option preserved')
    elif typ == 'links':
        links = [x.strip() for x in re.split(r'[,\n]', text) if x.strip()]
        valid = len(links) <= 10 and all(len(x) <= 2048 and urlparse(x).scheme == 'https' and urlparse(x).hostname and not urlparse(x).username for x in links)
        if valid:result['value'] = links
        else:result['state'] = 'unknown'; warn('Unsafe or unsupported external link preserved only as inert raw text')
    else:
        limit = 250 if typ == 'short_text' else 10000
        if len(text) > limit:result['state'] = 'unknown'; warn('Oversized historical field retained in raw')
        else:result['value'] = text
    return result

def preview_workbook(data, filename):
    if not isinstance(data, bytes) or len(data) > MAX_BYTES:
        raise ValueError('Workbook exceeds 16 MiB limit')
    if not isinstance(filename, str) or len(filename) > 250:
        raise ValueError('Invalid workbook filename')
    proposal = {'filename': filename, 'workbook_hash': hashlib.sha256(data).hexdigest(), 'reports': [], 'inventories': [], 'diagnostics': []}
    diagnostics = proposal['diagnostics']
    def warn(message, sheet=None, row=None, field=None):
        if len(diagnostics) < 2000:
            diagnostics.append(dict(severity='warning', message=message, sheet=sheet, row=row, field=field))
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
        with archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(x.file_size for x in entries) > 64 * 1024 * 1024:
                raise ValueError('Expanded workbook limit exceeded')
            names = [x.filename for x in entries]
            if len(names) != len(set(names)):
                raise ValueError('Duplicate ZIP member names')
            for entry in entries:
                name = entry.filename
                if entry.flag_bits & 1 or '\\' in name or name.startswith('/') or '..' in name.split('/') or ':' in name or 'vbaproject' in name.lower() or name.lower().endswith('.bin'):
                    raise ValueError('Encrypted, macro, or unsafe workbook member')
                if entry.file_size > 0 and entry.file_size / max(entry.compress_size, 1) > 200:
                    raise ValueError('Suspicious compression ratio')
            workbook = _xml(archive, 'xl/workbook.xml')
            props = workbook.find(NS + 'workbookPr')
            epoch = props is not None and props.get('date1904') in ('1', 'true')
            rels = _xml(archive, 'xl/_rels/workbook.xml.rels')
            targets = {}
            for rel in rels:
                if rel.get('TargetMode') == 'External':
                    continue
                target = rel.get('Target', '')
                targets[rel.get('Id')] = posixpath.normpath(target.lstrip('/') if target.startswith('/') else posixpath.join('xl', target))
            strings = []
            if 'xl/sharedStrings.xml' in names:
                strings = [_text(n) for n in _xml(archive, 'xl/sharedStrings.xml').findall(NS + 'si')]
            sheets = []
            total_rows = 0
            total_cells = 0
            decoded_text_bytes = 0
            for sheet in workbook.find(NS + 'sheets'):
                target = targets.get(sheet.get(REL))
                if not target or not target.startswith('xl/') or target not in names:
                    raise ValueError('Unsupported sheet relationship')
                root = _xml(archive, target)
                rows = {}
                for row in root.iter(NS + 'row'):
                    total_rows += 1
                    if total_rows > 10000:raise ValueError('Workbook exceeds 10000 rows')
                    cells = {}
                    for c in row.findall(NS + 'c'):
                        address = c.get('r', '')
                        if not re.fullmatch(r'[A-Z]{1,3}[1-9][0-9]*', address):raise ValueError('Invalid cell address')
                        col = address.rstrip('0123456789')
                        node = c.find(NS + 'v')
                        value = node.text or '' if node is not None else ''
                        if c.get('t') == 's':
                            index = int(value)
                            if index < 0 or index >= len(strings):raise ValueError('Invalid shared string')
                            value = strings[index]
                        elif c.get('t') == 'inlineStr':value = _text(c)
                        if len(value) > MAX_CELL_TEXT:
                            raise ValueError(f'Cell text exceeds {MAX_CELL_TEXT} characters in sheet {sheet.get("name")} at {address}')
                        total_cells += 1
                        decoded_text_bytes += len(value.encode('utf-8'))
                        if total_cells > MAX_CELLS or decoded_text_bytes > MAX_DECODED_TEXT_BYTES:
                            raise ValueError('Workbook logical cell/text budget exceeded')
                        cells[col] = {'raw': value, 'formula': c.find(NS + 'f') is not None, 'style': c.get('s')}
                    rows[int(row.get('r'))] = cells
                sheets.append((sheet.get('name'), rows))
            versions = get_schema()['versions']
            version = None
            report_sheets = set()
            for name, rows in sheets:
                headers = {k: v['raw'].strip() for k, v in rows.get(1, {}).items()}
                matches = [v for v, fields in versions.items() if all(headers.get(f['source_column']) == f['label'].strip() for f in fields)]
                if not matches:
                    if 'form response' in name.lower():raise ValueError('Unsupported or modified report header layout')
                    continue
                ver = matches[0]
                if version is not None and version != ver:raise ValueError('Mixed report versions unsupported')
                version = ver; report_sheets.add(name)
                for rownum, cells in sorted(rows.items()):
                    if rownum == 1 or not any(c['raw'].strip() for c in cells.values()):continue
                    populated = [c['raw'].strip() for c in cells.values() if c['raw'].strip()]
                    if len(populated) == 1 and re.fullmatch(r'(19|20)\d{2}(\.0)?', populated[0]):
                        warn('Year marker row skipped', name, rownum); continue
                    fields = {}
                    for f in versions[ver]:
                        c = cells.get(f['source_column'], {'raw': '', 'formula': False})
                        fields[f['id']] = _cell(f, c['raw'], c['formula'], epoch, lambda msg: warn(msg, name, rownum, f['id']))
                    source = dict(filename=filename, workbook_hash=proposal['workbook_hash'], sheet=name, row=rownum)
                    proposal['reports'].append(dict(form_version=ver, fields=fields, source=source, fingerprint=_fingerprint('report:' + ver, name, cells)))
            if version is None:raise ValueError('No supported V1/V2/V3 report sheet found')
            if version == 'V1':
                for name, rows in sheets:
                    if name in report_sheets:continue
                    if name.lower() == 'last updated by':
                        warn('Workbook update legend retained as source metadata, not operational readiness', name)
                        proposal['legend'] = rows
                        continue
                    if rows.get(2, {}).get('B', {}).get('raw', '').strip().lower() != 'instruments':
                        raise ValueError('Unsupported inventory layout')
                    title = rows.get(1, {}).get('B', {}).get('raw', '')
                    warn('Sheet identity and title retained separately; location requires reconciliation', name)
                    inv = dict(sheet=name, title=title, items=[], movements=[], source={'sheet': name, 'title': title, 'rows': rows})
                    category = 'Hardware'
                    ledger_open = True
                    for rownum, cells in sorted(rows.items()):
                        def raw(col):return cells.get(col, {}).get('raw', '').strip()
                        if rownum <= 2:continue
                        label = raw('B')
                        category_header = bool(label and re.search(r'(cranial|spine)\s+(navigation\s+)?instruments|other components', label, re.I))
                        if category_header:
                            category = 'Cranial instruments' if 'cranial' in label.lower() else 'Spine instruments' if 'spine' in label.lower() else 'Other components'
                        # Actual stock rows have a numeric serial sequence in A.
                        if label and not category_header and (_number(raw('A')) is not None or re.fullmatch(r'[ivxlcdm]+', raw('A'), re.I)):
                            qty = _number(raw('C'))
                            if qty is not None and not qty.is_integer():qty = None
                            availability = 'unavailable' if raw('C').lower() in ('na', 'n/a') else 'available' if qty is not None or raw('C').lower() == 'av' else 'unknown'
                            item = dict(category=category,name=label,serial_number=None,quantity=int(qty) if qty is not None else None,availability=availability,remarks=raw('D'),source={'sheet':name,'row':rownum,'cells':cells},fingerprint=_fingerprint('item',name,cells))
                            inv['items'].append(item)
                        if raw('H').lower() == 'current status':
                            ledger_open = False
                            inv['source']['status_block_start_row'] = rownum
                        if ledger_open and raw('H') and raw('H').lower() not in ('instrumets / components', 'instruments / components'):
                            qty = _number(raw('I'))
                            date = _date(raw('G'),epoch) if raw('G') else None
                            movement = dict(item_name=raw('H'),event_date=date,quantity=int(qty) if qty is not None and qty.is_integer() else None,provided_by=raw('J'),reference_note=raw('G') if date is None else '',cost=_number(raw('K')),source={'sheet':name,'row':rownum,'cells':cells},fingerprint=_fingerprint('movement',name,cells))
                            inv['movements'].append(movement)
                    proposal['inventories'].append(inv)
            if proposal.get('legend'):
                for inventory in proposal['inventories']:
                    inventory['source']['update_legend'] = proposal['legend']
            proposal['summary'] = dict(version=version,reports=len(proposal['reports']),systems=len(proposal['inventories']),inventory_items=sum(len(x['items']) for x in proposal['inventories']),inventory_movements=sum(len(x['movements']) for x in proposal['inventories']))
            if len(json.dumps(proposal, ensure_ascii=False).encode('utf-8')) > MAX_PROPOSAL_BYTES:
                raise ValueError('Import proposal memory budget exceeded')
            return proposal
    except (zipfile.BadZipFile, KeyError, ET.ParseError, TypeError, IndexError, OverflowError) as error:
        raise ValueError('Invalid or unsupported XLSX workbook') from error


def commit_import(connection, proposal, owner_id, actor_id):
    """All source rows and audit events commit atomically; no stock reconciliation."""
    if any(d.get('severity') == 'error' for d in proposal['diagnostics']):raise ValueError('Blocking import diagnostics')
    now = datetime.now(timezone.utc).isoformat()
    batch_id = str(uuid.uuid4())
    counts = {'batch_id':batch_id,'imported_reports':0,'imported_inventory':0,'skipped_duplicates':0}
    def audit(action, entity, identifier, fields):
        connection.execute('INSERT INTO audit_events VALUES(?,?,?,?,?,?,?)',(str(uuid.uuid4()),actor_id,action,entity,identifier,json.dumps(fields),now))
    def seen(row, entity):
        if connection.execute('SELECT 1 FROM imported_rows WHERE fingerprint=?',(row['fingerprint'],)).fetchone():
            counts['skipped_duplicates'] += 1
            return True
        return False
    def mark(row, entity, identifier):
        connection.execute('INSERT INTO imported_rows VALUES(?,?,?,?)',(row['fingerprint'],batch_id,entity,identifier))
    with connection:
        connection.execute('BEGIN IMMEDIATE')
        owner = connection.execute('SELECT role,active FROM users WHERE id=?',(owner_id,)).fetchone()
        actor = connection.execute('SELECT role,active FROM users WHERE id=?',(actor_id,)).fetchone()
        if not owner or owner['role'] != 'engineer' or not owner['active']:raise ValueError('Import owner must be an active engineer')
        if not actor or actor['role'] != 'admin' or not actor['active']:raise ValueError('Import actor must be an active admin')
        connection.execute('INSERT INTO import_batches VALUES(?,?,?,?,?,?,?)',(batch_id,proposal['workbook_hash'],proposal['filename'],actor_id,now,0,0))
        for report in proposal['reports']:
            if seen(report,'report'):continue
            identifier = str(uuid.uuid4())
            source = dict(report['source'],import_batch_id=batch_id,row_fingerprint=report['fingerprint'])
            connection.execute('INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?)',(identifier,owner_id,report['form_version'],'draft',json.dumps(report['fields']),1,now,now,json.dumps(source)))
            mark(report,'report',identifier); audit('import','report',identifier,list(report['fields']))
            counts['imported_reports'] += 1
        for inv in proposal['inventories']:
            pending = [(kind,row) for kind,key in [('inventory_item','items'),('inventory_movement','movements')] for row in inv[key] if not seen(row,kind)]
            if not pending:continue
            # Sheet identity distinguishes conflicting physical labels; never collapse by serial.
            identity = 'historical:' + inv['sheet']
            system = connection.execute('SELECT id FROM systems WHERE identity=?',(identity,)).fetchone()
            sid = system['id'] if system else str(uuid.uuid4())
            if not system:
                connection.execute('INSERT INTO systems(id,identity,name,city,hospital,application_experts,issues,readiness,source_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(sid,identity,inv['title'] or inv['sheet'],'','','','Historical sheet/title location requires reconciliation','unknown',json.dumps(inv['source']),1,now,now))
                audit('import','system',sid,['identity','name'])
            for kind,row in pending:
                identifier = str(uuid.uuid4())
                source = dict(row['source'],import_batch_id=batch_id,workbook_hash=proposal['workbook_hash'],filename=proposal['filename'])
                if kind == 'inventory_item':
                    # Preserve quantity markers and source independently of normalized quantity.
                    remarks = row['remarks']
                    connection.execute('INSERT INTO inventory_items(id,system_id,category,name,serial_number,quantity,availability,remarks,source_json,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)',(identifier,sid,row['category'],row['name'],row['serial_number'],row['quantity'],row['availability'],remarks,json.dumps(source),1,now,now))
                else:
                    connection.execute('INSERT INTO inventory_movements VALUES(?,?,?,?,?,?,?,?,?,?)',(identifier,sid,row['item_name'],row['event_date'],row['quantity'],row['provided_by'],row['reference_note'],row['cost'],now,json.dumps(source)))
                mark(row,kind,identifier);audit('import',kind,identifier,['historical_source'])
                counts['imported_inventory'] += 1
        connection.execute('UPDATE import_batches SET report_count=?,inventory_count=? WHERE id=?',(counts['imported_reports'],counts['imported_inventory'],batch_id))
        audit('import','import_batch',batch_id,['report_count','inventory_count'])
    return counts
