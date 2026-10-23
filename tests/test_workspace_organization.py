from pathlib import Path
import json,tempfile,unittest
from scripts.organize_workspace import apply,load_plan,rollback,check_owned,safe_relative,public_readmes
class WorkspaceOrganizationTests(unittest.TestCase):
 def fixture(self,root):
  (root/'config').mkdir();(root/'asharequant').mkdir();(root/'asharequant/main.py').write_text('value=42\n')
  (root/'docs').mkdir();(root/'docs/文件逐项说明.md').write_text('original guide')
  (root/'data').mkdir();(root/'data/account.sqlite3').write_bytes(b'ledger fixture')
  (root/'config.json').write_text('{"risk":0.225}')
  group=dict(name='02_Source (程序源码)',purpose='Source guide',entries=[dict(target=x,label=x.replace('/','__'),subfolder='',purpose='fixture') for x in ['asharequant/main.py','config','data','docs','config.json','backups']])
  (root/'config/workspace_layout.json').write_text(json.dumps(dict(groups=[group],metadata_names=['.workspace-layout.json','00_程序文件导航'])))
 def test_apply_preserves_inputs_and_links_then_rolls_back(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.fixture(root);legacy=root/'00_程序文件导航';legacy.mkdir();(legacy/'old.txt').write_text('original navigation');old={p:p.read_bytes() for p in [root/'asharequant/main.py',root/'data/account.sqlite3',root/'config.json']}
   cache=root/'asharequant/__pycache__/main.pyc';cache.parent.mkdir();cache.write_bytes(b'regenerable')
   r=apply(root,True);self.assertEqual(r['removed_cache_files'],1);self.assertFalse(cache.exists())
   self.assertTrue((root/'02_Source (程序源码)/asharequant__main.py').exists())
   self.assertTrue(all(p.read_bytes()==v for p,v in old.items()))
   rollback(root,r['backup']);self.assertEqual(cache.read_bytes(),b'regenerable');self.assertFalse((root/'02_Source (程序源码)').exists());self.assertEqual((root/'docs/文件逐项说明.md').read_text(),'original guide');self.assertEqual((legacy/'old.txt').read_text(),'original navigation')
 def test_unknown_root_and_new_module_require_classification(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.fixture(root);(root/'unexpected.txt').write_text('keep')
   with self.assertRaisesRegex(ValueError,'Unclassified root'):load_plan(root)
   (root/'unexpected.txt').unlink();(root/'asharequant/new.py').write_text('pass')
   with self.assertRaisesRegex(ValueError,'Unclassified source'):load_plan(root)
 def test_refresh_is_repeatable_and_user_file_never_deleted(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.fixture(root);apply(root);r=apply(root);self.assertGreater(r['links'],0)
   p=root/'02_Source (程序源码)/my_notes.txt';p.write_text('keep')
   with self.assertRaisesRegex(ValueError,'Unmanaged file'):apply(root)
   self.assertEqual(p.read_text(),'keep')
 def test_changed_generated_guide_blocks_overwrite(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);self.fixture(root);apply(root);p=root/'02_Source (程序源码)/00_README.md';p.write_text('personal edit')
   with self.assertRaisesRegex(ValueError,'Edited guide'):apply(root)
 def test_paths_cannot_escape_root(self):
  for p in ('/tmp/file','../file','group/../../file'):
   with self.assertRaises(ValueError):safe_relative(p)
 def public_fixture(self,root):
  self.fixture(root);p=root/'config/workspace_layout.json';layout=json.loads(p.read_text());layout['public_navigation']={'files':['asharequant/main.py']};p.write_text(json.dumps(layout));return load_plan(root)
 def test_public_navigation_never_lists_local_account_inventory(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);layout=self.public_fixture(root);pages=public_readmes(root,layout);text=''.join(pages.values())
   self.assertIn('asharequant/main.py',text);self.assertNotIn('account.sqlite3',text);self.assertNotIn('config.json',text);self.assertNotIn(str(root),text)
   (root/'data/private_trades.csv').write_text('private example')
   self.assertEqual(pages,public_readmes(root,load_plan(root)))
 def test_fresh_public_category_expands_and_rolls_back_without_data_change(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);layout=self.public_fixture(root);pages=public_readmes(root,layout)
   for name,text in pages.items():p=root/name;p.parent.mkdir();p.write_text(text)
   original=(root/'data/account.sqlite3').read_bytes();result=apply(root)
   for name,text in pages.items():self.assertEqual((root/name).read_text(),text)
   self.assertEqual((root/'data/account.sqlite3').read_bytes(),original)
   rollback(root,result['backup']);self.assertEqual(sorted(p.name for p in (root/'02_Source (程序源码)').iterdir()),['README.md'])
 def test_edited_public_category_is_not_overwritten(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);layout=self.public_fixture(root)
   for name,text in public_readmes(root,layout).items():p=root/name;p.parent.mkdir();p.write_text(text+'manual note')
   with self.assertRaisesRegex(ValueError,'Unmanaged category'):apply(root)
   self.assertTrue(p.read_text().endswith('manual note'))
 def test_public_targets_require_safe_paths_and_complete_classification(self):
  with tempfile.TemporaryDirectory() as d:
   root=Path(d);layout=self.public_fixture(root)
   for target in ['data/account.sqlite3','config.json','../outside.md']:
    layout['public_navigation']['files']=[target]
    with self.assertRaises(ValueError):public_readmes(root,layout)
   (root/'extra.md').write_text('new public documentation');layout['public_navigation']['files']=['extra.md']
   with self.assertRaisesRegex(ValueError,'Unclassified public'):public_readmes(root,layout)
   (root/'link.py').symlink_to('asharequant/main.py');layout['public_navigation']['files']=['link.py']
   with self.assertRaisesRegex(ValueError,'unsafe public'):public_readmes(root,layout)
