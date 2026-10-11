import io,json,tempfile,unittest,zipfile
from pathlib import Path
from xml.sax.saxutils import escape
from app.database import initialize,connect
from app.schema import get_schema
from app.importer import preview_workbook,commit_import

def workbook(version='V3', values=None, inventories=False, epoch=False):
    sheets=[('Form responses 1',1)]
    if inventories:sheets += [('Test System '+str(i),i+2) for i in range(6)]
    stream=io.BytesIO()
    with zipfile.ZipFile(stream,'w') as z:
        z.writestr('xl/workbook.xml','<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><workbookPr date1904="'+str(int(epoch))+'"/><sheets>'+''.join('<sheet name="'+n+'" sheetId="'+str(i)+'" r:id="r'+str(i)+'"/>' for n,i in sheets)+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<Relationships>'+''.join('<Relationship Id="r'+str(i)+'" Target="worksheets/sheet'+str(i)+'.xml"/>' for n,i in sheets)+'</Relationships>')
        def cell(col,row,value):return '<c r="'+col+str(row)+'" t="inlineStr"><is><t>'+escape(str(value))+'</t></is></c>'
        headers=get_schema()['versions'][version]
        rows='<row r="1">'+''.join(cell(f['source_column'],1,f['label']) for f in headers)+'</row>'
        rows+='<row r="2">'+cell('A',2,'2024')+'</row>'
        vals=values or {'C':'45292','D':'Synthetic Hospital','AM':'100','L':'1000'}
        rows+='<row r="3">'+''.join(cell(c,3,v) for c,v in vals.items())+'</row>'
        z.writestr('xl/worksheets/sheet1.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+rows+'</sheetData></worksheet>')
        for name,i in sheets[1:]:
            rows='<row r="1">'+cell('B',1,'Conflicting Title')+'</row><row r="2">'+cell('B',2,'Instruments ')+'</row><row r="3">'+cell('A',3,1)+cell('B',3,'Synthetic instrument')+cell('C',3,'AV')+'</row><row r="4">'+cell('A',4,2)+cell('B',4,'Synthetic unavailable')+cell('C',4,'NA')+cell('G',4,'45292')+cell('H',4,'Synthetic extra')+cell('I',4,2)+'</row><row r="5">'+cell('A',5,3)+cell('B',5,'Cranial Navigation Instruments')+'</row><row r="6">'+cell('A',6,'ii')+cell('B',6,'Synthetic cranial instrument')+cell('C',6,1)+'</row><row r="7">'+cell('H',7,'Current Status')+'</row><row r="8">'+cell('H',8,'Synthetic city')+'</row>'
            z.writestr('xl/worksheets/sheet'+str(i)+'.xml','<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'+rows+'</sheetData></worksheet>')
    return stream.getvalue()

class DatabaseImportTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name)/'test.sqlite';initialize(self.path);self.db=connect(self.path)
        with self.db:
            for id,role in [('owner','engineer'),('admin','admin'),('reviewer','reviewer')]:self.db.execute('INSERT INTO users VALUES(?,?,?,?,?,?)',(id,id,'testhash',role,1,'2026-01-01'))
    def tearDown(self):self.db.close();self.temp.cleanup()
    def test_schema_and_install_twice(self):
        initialize(self.path)
        self.assertEqual([len(get_schema()['versions'][v]) for v in ('V1','V2','V3')],[33,46,49])
        self.assertEqual(self.db.execute('SELECT count(*) FROM schema_migrations').fetchone()[0],1)
        with self.assertRaises(Exception),self.db:self.db.execute("INSERT INTO reports VALUES('bad','owner','V9','draft','{}',1,'now','now',NULL)")
    def test_preview_dates_sentinels_and_no_writes(self):
        p=preview_workbook(workbook(),'synthetic.xlsx')
        fields=p['reports'][0]['fields']
        self.assertEqual(fields['surgery_date']['value'],'2024-01-01')
        self.assertEqual(fields['tool_tip_accuracy_mm']['state'],'unknown')
        self.assertEqual(fields['xray_count']['state'],'unknown')
        self.assertEqual(self.db.execute('SELECT count(*) FROM reports').fetchone()[0],0)
        p=preview_workbook(workbook(values={'C':'0'},epoch=True),'epoch.xlsx')
        self.assertEqual(p['reports'][0]['fields']['surgery_date']['value'],'1904-01-01')
    def test_all_versions_idempotence_inventory(self):
        for v in ('V1','V2','V3'):
            p=preview_workbook(workbook(v,{'A':'45292','I':'Synthetic Hospital'} if v=='V1' else None,inventories=v=='V1'),'synthetic.xlsx')
            result=commit_import(self.db,p,'owner','admin');self.assertEqual(result['imported_reports'],1)
            again=commit_import(self.db,p,'owner','admin');self.assertEqual(again['imported_reports'],0)
            self.assertGreater(again['skipped_duplicates'],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM systems').fetchone()[0],6)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inventory_items').fetchone()[0],18)
        self.assertEqual(self.db.execute('SELECT count(*) FROM inventory_movements').fetchone()[0],6)
        self.assertEqual(self.db.execute("SELECT count(*) FROM inventory_items WHERE category='Cranial instruments'").fetchone()[0],6)
        item=self.db.execute("SELECT * FROM inventory_items WHERE availability='unavailable'").fetchone()
        self.assertIsNone(item['quantity']);self.assertEqual(item['remarks'],'');self.assertIsNotNone(item['source_json'])
    def test_atomic_rollback_and_permissions(self):
        p=preview_workbook(workbook(),'test.xlsx')
        self.db.execute("CREATE TRIGGER fail_audit BEFORE INSERT ON audit_events BEGIN SELECT RAISE(ABORT,'test');END;");self.db.commit()
        with self.assertRaises(Exception):commit_import(self.db,p,'owner','admin')
        self.assertEqual(self.db.execute('SELECT count(*) FROM reports').fetchone()[0],0)
        self.assertEqual(self.db.execute('SELECT count(*) FROM import_batches').fetchone()[0],0)
        with self.assertRaises(ValueError):commit_import(self.db,p,'reviewer','admin')
    def test_archive_abuse_rejected(self):
        with self.assertRaises(ValueError):preview_workbook(b'not zip','test.xlsx')
        stream=io.BytesIO()
        with zipfile.ZipFile(stream,'w') as z:z.writestr('../unsafe.xml','test')
        with self.assertRaises(ValueError):preview_workbook(stream.getvalue(),'test.xlsx')
    def test_same_rows_changed_workbook_dedupe(self):
        p=preview_workbook(workbook(),'one.xlsx');commit_import(self.db,p,'owner','admin')
        data=io.BytesIO(workbook())
        with zipfile.ZipFile(data,'a') as z:z.writestr('docProps/synthetic.xml','<test/>')
        p=preview_workbook(data.getvalue(),'two.xlsx')
        self.assertEqual(commit_import(self.db,p,'owner','admin')['imported_reports'],0)

if __name__=='__main__':unittest.main()
