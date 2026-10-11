"""Read all workbook XML without changing reference files."""
from pathlib import Path
import json
import zipfile
import xml.etree.ElementTree as ET

NS = {'s': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
result = []
for path in sorted(Path('docs/reference').rglob('*.xlsx')):
    with zipfile.ZipFile(path) as archive:
        strings = []
        if 'xl/sharedStrings.xml' in archive.namelist():
            root = ET.fromstring(archive.read('xl/sharedStrings.xml'))
            strings = [''.join(x.itertext()) for x in root]
        rels = ET.fromstring(archive.read('xl/_rels/workbook.xml.rels'))
        targets = {x.attrib['Id']: x.attrib['Target'] for x in rels}
        book = ET.fromstring(archive.read('xl/workbook.xml'))
        sheets = []
        for sheet in book.find('s:sheets', NS):
            rid = sheet.attrib['{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id']
            target = targets[rid]
            target = target.lstrip('/') if target.startswith('/') else 'xl/' + target
            root = ET.fromstring(archive.read(target))
            cells = []
            for cell in root.findall('.//s:sheetData/s:row/s:c', NS):
                value = cell.find('s:v', NS)
                formula = cell.find('s:f', NS)
                value = value.text if value is not None else None
                if cell.attrib.get('t') == 's' and value is not None:
                    value = strings[int(value)]
                elif cell.attrib.get('t') == 'inlineStr':
                    value = ''.join(cell.find('s:is', NS).itertext())
                if value is not None or formula is not None:
                    cells.append({'cell': cell.attrib['r'], 'value': value, 'formula': formula.text if formula is not None else None})
            sheets.append({'name': sheet.attrib['name'], 'state': sheet.attrib.get('state', 'visible'), 'cells': cells,
                           'validations': [ET.tostring(x, encoding='unicode') for x in root.findall('.//s:dataValidation', NS)],
                           'merges': [x.attrib['ref'] for x in root.findall('.//s:mergeCell', NS)]})
        result.append({'file': str(path), 'sheets': sheets, 'other_parts': [x for x in archive.namelist() if any(k in x for k in ('drawing', 'comment', 'externalLink', 'vba', 'table'))]})
Path('docs/requirements/reference-extraction.json').write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding='utf-8')
for book in result:
    print(book['file'])
    for sheet in book['sheets']:
        print(sheet['name'], sheet['state'], 'cells:', len(sheet['cells']), 'formulas:', sum(c['formula'] is not None for c in sheet['cells']))
        print('HEADERS:', json.dumps([c for c in sheet['cells'] if int(''.join(filter(str.isdigit, c['cell']))) == 1], ensure_ascii=False))
        print('VALIDATIONS:', sheet['validations'])
    print('OTHER PARTS:', book['other_parts'])
