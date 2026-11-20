import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import robin_mcp as r

class NativeIntegration(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.old=r.ROOT
        self.old_backups=r.BACKUPS
        r.ROOT=Path(self.temp.name).resolve()
        r.BACKUPS=r.ROOT/'external-backups'
    def tearDown(self):
        r.ROOT=self.old
        r.BACKUPS=self.old_backups
        self.temp.cleanup()
    def test_native_edit_conflict_and_references(self):
        p=r.execute('create_project',{'name':'Тест MCP'})
        path=p['path']; rev=p['revision']
        v=r.execute('add_resource',dict(path=path,expected_revision=rev,name='Текст',type='Robin.String',value_spec={'value':'Привет <&>!'}))
        with self.assertRaisesRegex(ValueError,'expected_revision'):
            r.execute('add_resource',dict(path=path,expected_revision=rev,name='Stale',type='Robin.String',value_spec={'value':'x'}))
        action='<Action Id="old" Contract="Robin.Test" ParentSequenceId="old-parent"><Params><Param Name="text" /></Params><Results><Result Name="out" Id="result-old" /></Results><ImplementationInfo Platform="Net" Contract="Robin.Test" Version="1.0.0" /></Action>'
        added=r.execute('add_action',dict(path=path,expected_revision=v['revision'],sequence_id=p['sequence_id'],action_xml=action))
        bound=r.execute('set_parameter',dict(path=path,expected_revision=added['revision'],action_id=added['action_id'],parameter='text',value_spec={'resource_id':v['resource_id']}))
        with self.assertRaisesRegex(ValueError,'Dangling'):
            r.execute('remove_element',dict(path=path,expected_revision=bound['revision'],kind='Resource',id=v['resource_id']))
        read=r.execute('read_project',{'path':path})
        self.assertEqual([],read['errors'])
        self.assertEqual(bound['revision'],read['revision'])
        self.assertTrue(Path(bound['backup']).exists())
        self.assertFalse(Path(bound['backup']).is_relative_to(Path(path).parent))
        self.assertFalse((Path(path).parent/'.robin-mcp-backups').exists())
        self.assertNotEqual('result-old',added['id_mapping']['result-old'])
        self.assertIn('Привет',read['xml'])
    def test_real_projects_read_only_roundtrip(self):
        projects=list(r.DISCOVERY.rglob('*.robin-project'))
        if not projects: self.skipTest('No installed user projects')
        for p in projects:
            before=r.digest(p)
            _,root,_=r.load(p)
            encoded=r.native('write',text=r.xml(root))
            out=r.ROOT/p.name
            out.write_bytes(encoded)
            self.assertEqual(r.xml(root),r.native('read',out))
            self.assertEqual(before,r.digest(p))
    def test_workspace_guard(self):
        with self.assertRaisesRegex(ValueError,'restricted'):
            r.project_path(r.ROOT.parent/'outside.robin-project',write=True)
    def test_replace_reviewed_copy(self):
        old=r.execute('create_project',{'name':'Original'})
        staged=r.execute('create_project',{'name':'Reviewed'})
        added=r.execute('add_resource',dict(path=staged['path'],expected_revision=staged['revision'],name='Checked',type='Robin.String',value_spec={'value':'ready'}))
        result=r.execute('replace_project_from_copy',dict(path=old['path'],source=staged['path'],expected_revision=old['revision']))
        self.assertIn('Checked',r.execute('read_project',{'path':old['path']})['xml'])
        self.assertEqual(old['revision'],r.digest(Path(result['backup'])))
        with self.assertRaisesRegex(ValueError,'expected_revision'):
            r.execute('replace_project_from_copy',dict(path=old['path'],source=staged['path'],expected_revision=old['revision']))
    def test_stdio(self):
        requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2025-11-25'}},
                  {'jsonrpc':'2.0','method':'notifications/initialized'},
                  {'jsonrpc':'2.0','id':2,'method':'tools/list'},
                  {'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'unknown'}}]
        proc=subprocess.run([sys.executable,str(r.BASE/'robin_mcp.py')],input='\n'.join(map(json.dumps,requests))+'\n',text=True,encoding='utf-8',capture_output=True,timeout=15)
        responses=[json.loads(l) for l in proc.stdout.splitlines()]
        self.assertEqual(0,proc.returncode,proc.stderr)
        self.assertEqual(3,len(responses))
        self.assertEqual('2025-11-25',responses[0]['result']['protocolVersion'])
        self.assertEqual(len(r.TOOLS),len(responses[1]['result']['tools']))
        self.assertTrue(responses[2]['result']['isError'])

if __name__=='__main__': unittest.main()
