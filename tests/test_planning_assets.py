import base64,sqlite3,tempfile,unittest,os
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).parents[1]/'src'))
from codex_workbench.planning_assets import PlanningAssets
class T(unittest.TestCase):
 def test_dedupe_link_list_and_integrity(self):
  with tempfile.TemporaryDirectory() as d:
   c=sqlite3.connect(':memory:'); a=PlanningAssets(Path(d)/'assets',c); enc=base64.b64encode(b'abc').decode(); x=a.add_base64(enc,name='a.txt',task_id='t1'); y=a.add_base64(enc,name='a.txt',task_id='t2'); self.assertEqual(x['id'],y['id']); self.assertEqual(1,len(a.list())); a.link(x['id'],'t3'); self.assertEqual(1,len(a.list('t3'))); self.assertEqual(b'abc',a.get(x['id'],include_content=True)['content'])
 def test_path_requires_root_and_rejects_escape(self):
  with tempfile.TemporaryDirectory() as d:
   c=sqlite3.connect(':memory:'); a=PlanningAssets(Path(d)/'assets',c); p=Path(d)/'x';p.write_bytes(b'x')
   with self.assertRaises(ValueError):a.add_path(p,allowed_root=Path(d)/'other')
   root=Path(d)/'root';root.mkdir(); outside=Path(d)/'secret';outside.write_bytes(b's')
   with self.assertRaises(ValueError):a.add_path(root/'..'/'secret',allowed_root=root)
   fifo=root/'pipe';os.mkfifo(fifo)
   with self.assertRaises(ValueError):a.add_path(fifo,allowed_root=root)

 def test_versions_and_null_refs_are_stable(self):
  with tempfile.TemporaryDirectory() as d:
   c=sqlite3.connect(':memory:'); a=PlanningAssets(Path(d)/'assets',c)
   x=a.add_bytes(b'a',name='same.txt',source_kind='output')
   y=a.add_bytes(b'b',name='same.txt',source_kind='output')
   self.assertEqual((1,2),(x['version'],y['version']))
   a.add_bytes(b'a',name='same.txt',source_kind='output')
   n=c.execute('SELECT COUNT(*) FROM asset_refs WHERE asset_id=?',(x['id'],)).fetchone()[0]
   self.assertEqual(1,n)
 def test_corruption_detected(self):
  with tempfile.TemporaryDirectory() as d:
   c=sqlite3.connect(':memory:'); a=PlanningAssets(Path(d)/'assets',c); x=a.add_base64(base64.b64encode(b'a').decode(),name='a'); stored=c.execute('SELECT path FROM assets WHERE id=?',(x['id'],)).fetchone()[0]; (Path(d)/'assets'/stored).write_bytes(b'b')
   with self.assertRaises(ValueError):a.get(x['id'],include_content=True)

 def test_long_unicode_name_uses_bounded_storage_filename(self):
  with tempfile.TemporaryDirectory() as d:
   c=sqlite3.connect(':memory:'); a=PlanningAssets(Path(d)/'assets',c)
   name='文' * 250 + '.txt'; x=a.add_bytes(b'x',name=name)
   stored=c.execute('SELECT path FROM assets WHERE id=?',(x['id'],)).fetchone()[0]
   self.assertEqual(name,x['name']); self.assertLessEqual(len(stored.encode()),255)
if __name__=='__main__':unittest.main()
