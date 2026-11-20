"""Local, UI-free Robin Studio source editor. Python 3.10+, Windows/.NET 4.8."""
from __future__ import annotations
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

BASE = Path(__file__).resolve().parent
STUDIO = Path(os.environ.get('ROBIN_STUDIO', r'C:\Program Files\Robin Platform\2.0.0\Studio'))
ROOT = Path(os.environ.get('ROBIN_WORKSPACE', str(BASE / 'projects'))).resolve()
BACKUPS = BASE / 'backups' / 'project-edits'
DISCOVERY = Path.home() / 'Documents/Robin/Studio/Projects'

def uid(): return str(uuid.uuid4())
def now(): return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')
def xml(e): return ET.tostring(e, encoding='unicode')
def digest(p): return hashlib.sha256(p.read_bytes()).hexdigest()

def parse(text):
    if '<!DOCTYPE' in text.upper() or '<!ENTITY' in text.upper():
        raise ValueError('DTD/entities are not supported')
    return ET.fromstring(text)

def native(operation, path=None, text=None):
    with tempfile.TemporaryDirectory(prefix='robin-mcp-') as d:
        d = Path(d)
        request = dict(studio=str(STUDIO), operation=operation, output=str(d/'output'))
        if path is not None: request['path'] = str(path)
        if text is not None:
            (d/'input.xml').write_text(text, encoding='utf-8')
            request['input'] = str(d/'input.xml')
        (d/'request.json').write_text(json.dumps(request), encoding='utf-8')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
                                 '-File', str(BASE/'bridge.ps1'), '-Request', str(d/'request.json')],
                                capture_output=True, timeout=60)
        if result.returncode:
            raise RuntimeError(result.stderr.decode('utf-8', errors='replace'))
        return (d/'output').read_text(encoding='utf-8-sig') if operation == 'read' else (d/'output').read_bytes()

def project_path(path, write=False):
    p = Path(path).resolve()
    if p.suffix != '.robin-project': raise ValueError('Expected .robin-project source file')
    if write and not p.is_relative_to(ROOT):
        raise ValueError('Writes are restricted to ROBIN_WORKSPACE; import a copy first')
    return p

def load(path):
    p = project_path(path)
    before = digest(p)
    root = parse(native('read', p))
    if digest(p) != before: raise ValueError('Project changed while reading; retry')
    if root.tag != 'ScenarioEntity' or root.get('SchemaVersion') != '5.5.0':
        raise ValueError('Only verified ScenarioEntity schema 5.5.0 is supported')
    return p, root, before

def validate(root):
    errors, ids = [], {}
    for e in root.iter():
        if e.tag in ('Action', 'Sequence', 'Resource', 'Result') and e.get('Id'):
            i = e.get('Id')
            if i in ids: errors.append('Duplicate Id: '+i)
            ids[i] = e
    for e in root.iter('FromResourceRef'):
        if e.get('Id') not in ids or ids[e.get('Id')].tag not in ('Resource','Result'):
            errors.append('Dangling resource reference: '+str(e.get('Id')))
    for seq in root.iter('Sequence'):
        for action in seq.findall('./Actions/Action'):
            if action.get('ParentSequenceId') != seq.get('Id'):
                errors.append('Wrong ParentSequenceId: '+str(action.get('Id')))
    return errors

def save(p, root, expected):
    errors = validate(root)
    if errors: raise ValueError('; '.join(errors))
    if digest(p) != expected: raise ValueError('Revision conflict; read project again')
    root.set('LastEditDate', now())
    data = native('write', text=xml(root))
    # Native encoding happens before the final conflict check and replacement.
    if digest(p) != expected: raise ValueError('Revision conflict during serialization')
    # Studio packages every file below the project directory. Robin's extractor
    # cannot create nested folders, so backups must live outside that tree.
    project_key = hashlib.sha256(str(p.resolve()).casefold().encode('utf-8')).hexdigest()[:20]
    backup = (BACKUPS / project_key / (p.name+'.'+uid()+'.bak')).resolve()
    if backup.is_relative_to(p.parent.resolve()):
        raise ValueError('Backup directory must be outside the project directory')
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(p, backup)
    temp = p.with_name('.'+uid()+'.tmp')
    try:
        temp.write_bytes(data)
        os.replace(temp, p)
    finally:
        temp.unlink(missing_ok=True)
    return dict(path=str(p), revision=digest(p), backup=str(backup),
                requires_studio_rebuild=True)

def find(root, tag, id):
    matches = [e for e in root.iter(tag) if e.get('Id') == id]
    if len(matches) != 1: raise ValueError(f'Expected one {tag} with Id={id}')
    return matches[0]

def value(type, value):
    tags = {'Robin.String':'RobinString','Robin.Numeric':'RobinNumeric','Robin.Boolean':'RobinBoolean',
            'Robin.FilePath':'RobinFilePath','Robin.FolderPath':'RobinFolderPath'}
    if type not in tags: raise ValueError('Use serialized_value for complex Robin types')
    e = ET.Element(tags[type])
    if type in ('Robin.FilePath','Robin.FolderPath'): e.text = globals()['value']('Robin.String', value)
    elif type == 'Robin.Boolean':
        if not isinstance(value, bool): raise ValueError('Boolean value required')
        e.text = str(value).lower()
    elif type == 'Robin.Numeric':
        if isinstance(value, bool) or not isinstance(value, (int,float)): raise ValueError('Numeric value required')
        e.text = str(value)
    else: e.text = str(value)
    return '<![CDATA['+xml(e)+']]>'

def set_value(e, spec):
    for child in list(e):
        if child.tag in ('Value','DisplayValue','FromResourceRef'): e.remove(child)
    e.attrib.pop('SelectedPossibleValue', None)
    if 'resource_id' in spec:
        ET.SubElement(e, 'FromResourceRef', Id=spec['resource_id'])
    else:
        text = spec.get('serialized_value')
        if text is None: text = value(spec.get('type','Robin.String'), spec['value'])
        ET.SubElement(e, 'Value').text = text
        ET.SubElement(e, 'DisplayValue').text = str(spec.get('display', spec.get('value','')))
    if 'selected_possible_value' in spec: e.set('SelectedPossibleValue', spec['selected_possible_value'])

def execute(name, a):
    if name == 'list_projects':
        directory = Path(a.get('directory', DISCOVERY)).resolve()
        return [dict(path=str(p), name=p.stem) for p in directory.rglob('*.robin-project')]
    if name == 'import_project':
        source = project_path(a['source'])
        load(source)
        target = ROOT / uid()
        ROOT.mkdir(parents=True, exist_ok=True)
        # Copy the complete project, including resource payload files.
        if any(p.is_symlink() or (hasattr(p,'is_junction') and p.is_junction()) for p in source.parent.rglob('*')):
            raise ValueError('Project contains links; import regular files only')
        shutil.copytree(source.parent, target, ignore=shutil.ignore_patterns('.robin-mcp-backups'))
        return dict(path=str(target/source.name), original_unchanged=True)
    if name == 'create_project':
        title = a['name']
        if not re.fullmatch(r'[\w -]{1,80}', title) or title.endswith((' ','.')):
            raise ValueError('Name must contain 1-80 letters, numbers, spaces, underscores or hyphens')
        root = ET.Element('ScenarioEntity', SchemaVersion='5.5.0', Platform='Net', RequireUi='true',
                          ScenarioLogLevel='RobotDeveloper', LastEditDate=now())
        ET.SubElement(root,'ResourcesCollection')
        seq = ET.SubElement(root,'Sequence', Id=uid(), IsEditable='true', IsExpanded='true')
        ET.SubElement(seq,'Actions')
        data = native('write', text=xml(root))
        target = ROOT / uid()
        target.mkdir(parents=True)
        p = target/(title+'.robin-project')
        p.write_bytes(data)
        meta = ET.Element('AboutRobotProject', contract=title, projectVersion='1.0.0', creationDate=now(),
                          lastEditDate=now(), studioDistrType='Standalone', studioVersion='2.24.4.101653')
        ET.SubElement(meta,'Author')
        ET.SubElement(ET.SubElement(ET.SubElement(meta,'Titles'),'Title',lang='ru-RU'),'content').text=title
        ET.SubElement(meta,'Descriptions')
        p.with_suffix('.robin-prjmeta').write_text(xml(meta), encoding='utf-8')
        return dict(path=str(p), sequence_id=seq.get('Id'), revision=digest(p), requires_studio_rebuild=True)
    p, root, revision = load(a['path'])
    if name == 'read_project':
        return dict(path=str(p), revision=revision, xml=xml(root), errors=validate(root))
    if name == 'validate_project': return dict(errors=validate(root), revision=revision, scope='Source structure only; not runtime validation')
    if name == 'list_action_templates':
        query = a.get('query','').casefold()
        return [dict(id=e.get('Id'), contract=e.get('Contract'), title=e.get('UniqueName'), xml=xml(e))
                for e in root.iter('Action') if query in (e.get('Contract','')+' '+e.get('UniqueName','')).casefold()]
    project_path(p, write=True)
    if a.get('expected_revision') != revision: raise ValueError('expected_revision is required and must match read_project')
    if name == 'replace_project_from_copy':
        _, replacement, _ = load(a['source'])
        return save(p, replacement, revision)
    if name == 'add_action':
        seq = find(root,'Sequence',a['sequence_id'])
        action = parse(a['action_xml'])
        if action.tag != 'Action': raise ValueError('Expected Action XML; use list_action_templates for native templates')
        mapping = {e.get('Id'):uid() for e in action.iter() if e.tag in ('Action','Sequence','Result','Resource') and e.get('Id')}
        for e in action.iter():
            for key in ('Id','ParentSequenceId'):
                if e.get(key) in mapping: e.set(key,mapping[e.get(key)])
        action.set('Id', action.get('Id') or uid())
        action.set('ParentSequenceId', seq.get('Id'))
        if 'title' in a: action.set('UniqueName',a['title'])
        actions = seq.find('Actions')
        if actions is None: actions = ET.SubElement(seq,'Actions')
        index = a.get('index',len(actions))
        if not isinstance(index,int) or not 0 <= index <= len(actions): raise ValueError('Invalid insertion index')
        actions.insert(index,action)
        result = dict(action_id=action.get('Id'), id_mapping=mapping)
    elif name == 'set_parameter':
        action = find(root,'Action',a['action_id'])
        params = action.find('Params')
        matches = [] if params is None else [e for e in params if e.get('Name') == a['parameter']]
        if len(matches) != 1: raise ValueError('Parameter missing or ambiguous; inspect native template')
        set_value(matches[0],a['value_spec']); result = {}
    elif name == 'add_resource':
        resources = root.find('ResourcesCollection')
        if resources is None: resources=ET.SubElement(root,'ResourcesCollection')
        collection = a.get('collection','Variables')
        groups = [e for e in resources if e.get('Name') == collection]
        group = groups[0] if groups else ET.SubElement(resources,'NamedCollection',Name=collection)
        items = group.find('Resources')
        if items is None: items=ET.SubElement(group,'Resources')
        if any(e.get('Name') == a['name'] for e in root.iter('Resource')): raise ValueError('Resource name already exists')
        e=ET.SubElement(items,'Resource',Id=uid(),Name=a['name'],Type=a['type'],IsRequired='false',IsConstant='false',
                       IsTrackWhileDebug='false',CreationDate=now(),IsOutResult='false',IsInputParameter='false')
        spec=dict(a['value_spec'],type=a['type'])
        set_value(e,spec); result=dict(resource_id=e.get('Id'))
    elif name == 'update_resource':
        e=find(root,'Resource',a['resource_id'])
        set_value(e,dict(a['value_spec'],type=e.get('Type'))); result={}
    elif name == 'set_properties':
        tag=a['kind']
        allowed={'Action':{'UniqueName','DelayStart','IsEnabled','IsHaveBreakpoint','LogLevel','TakeScreenshot','IsSoftStop'},
                 'Resource':{'Name','IsRequired','IsConstant','IsTrackWhileDebug','IsOutResult','IsInputParameter'},
                 'Sequence':{'IsExpanded','IsEditable'}}
        if tag not in allowed: raise ValueError('Unsupported kind')
        e=find(root,tag,a['id'])
        for key,v in a['properties'].items():
            if key not in allowed[tag]: raise ValueError('Unsupported property: '+key)
            if key.startswith('Is') or key == 'TakeScreenshot':
                if not isinstance(v,bool): raise ValueError(key+' requires boolean')
                v=str(v).lower()
            if key == 'DelayStart' and (not isinstance(v,(int,float)) or v < 0): raise ValueError('Invalid delay')
            e.set(key,str(v))
        result={}
    elif name == 'remove_element':
        if a['kind'] not in ('Action','Resource'): raise ValueError('Only Action/Resource removal supported')
        e=find(root,a['kind'],a['id'])
        parent=next(parent for parent in root.iter() if e in list(parent))
        parent.remove(e); result={}
    else: raise ValueError('Unknown tool: '+name)
    return dict(save(p,root,revision), **result)

S={'type':'string'}
O={'type':'object'}
PATH={'path':S}
EDIT=dict(PATH,expected_revision=S)
TOOLS=[]
def tool(name, description, properties, required=None):
    TOOLS.append(dict(name=name,description=description,inputSchema=dict(type='object',properties=properties,
                 required=list(properties) if required is None else required,additionalProperties=False)))
tool('list_projects','Discover local Robin source projects (read only).',{'directory':S},[])
tool('import_project','Copy a complete project into the editable workspace; never modifies the source.',{'source':S})
tool('create_project','Create an empty encrypted 5.5.0 source and metadata. Studio must save/build before execution.',{'name':S})
tool('read_project','Read native project XML and SHA256 revision. Read before every edit.',PATH)
tool('validate_project','Check source IDs, references and sequence parents; does not compile or execute.',PATH)
tool('list_action_templates','Read complete Action XML templates from an existing project, including parameters, results and versions.',dict(PATH,query=S),['path'])
tool('add_action','Insert native Action XML, regenerate IDs including nested results, preserve internal references. External references must already exist.',dict(EDIT,sequence_id=S,action_xml=S,title=S,index={'type':'integer','minimum':0}),['path','expected_revision','sequence_id','action_xml'])
tool('set_parameter','Set existing parameter: value_spec is {type,value}, {resource_id}, or {serialized_value,display}; optional selected_possible_value.',dict(EDIT,action_id=S,parameter=S,value_spec=O))
tool('add_resource','Add variable or element. Basic types String/Numeric/Boolean/FilePath/FolderPath support {value}; other types need native {serialized_value,display}.',dict(EDIT,name=S,type=S,value_spec=O,collection=S),['path','expected_revision','name','type','value_spec'])
tool('update_resource','Replace resource value using native serialized_value or a typed basic value.',dict(EDIT,resource_id=S,value_spec=O))
tool('set_properties','Edit supported Action/Resource/Sequence attributes. Identity and contracts cannot be overwritten.',dict(EDIT,kind=S,id=S,properties=O))
tool('remove_element','Remove Action or Resource; rejects changes that leave dangling resource references.',dict(EDIT,kind=S,id=S))
tool('replace_project_from_copy','Commit a reviewed project copy into an authorized workspace file, with revision check, native validation and backup. Does not rebuild runtime files.',dict(EDIT,source=S))

def serve():
    for line in sys.stdin:
        request=None
        try:
            request=json.loads(line)
            if not isinstance(request,dict): raise ValueError('Request must be an object')
            if 'id' not in request: continue
            method=request.get('method'); params=request.get('params',{})
            if method == 'initialize':
                version=params.get('protocolVersion')
                supported=('2024-11-05','2025-03-26','2025-06-18','2025-11-25')
                result=dict(protocolVersion=version if version in supported else supported[-1],capabilities={'tools':{}},
                            serverInfo={'name':'robin-studio-files','version':'0.1.0'},
                            instructions='Edits affect source files only. Close the project in Studio before editing. Import a copy, read revision, edit, then reopen and save/build in Studio.')
            elif method == 'ping': result={}
            elif method == 'tools/list': result={'tools':TOOLS}
            elif method == 'tools/call':
                try:
                    name=params['name']; arguments=params.get('arguments',{})
                    schema=next(t['inputSchema'] for t in TOOLS if t['name']==name)
                    if set(schema['required'])-arguments.keys(): raise ValueError('Missing required arguments')
                    if arguments.keys()-schema['properties'].keys(): raise ValueError('Unknown arguments')
                    result={'content':[{'type':'text','text':json.dumps(execute(name,arguments),ensure_ascii=False)}]}
                except Exception as e: result={'isError':True,'content':[{'type':'text','text':str(e)}]}
            else:
                print(json.dumps({'jsonrpc':'2.0','id':request['id'],'error':{'code':-32601,'message':'Method not found'}}),flush=True); continue
            response={'jsonrpc':'2.0','id':request['id'],'result':result}
        except Exception as e:
            response={'jsonrpc':'2.0','id':request.get('id') if isinstance(request,dict) else None,
                      'error':{'code':-32700 if isinstance(e,json.JSONDecodeError) else -32600,'message':str(e)}}
        print(json.dumps(response,ensure_ascii=False),flush=True)

if __name__ == '__main__':
    sys.stdin.reconfigure(encoding='utf-8'); sys.stdout.reconfigure(encoding='utf-8')
    serve()
