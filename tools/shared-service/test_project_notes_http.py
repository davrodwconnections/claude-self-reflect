"""Real loopback HTTP -> MCP gateway -> candidate worker -> isolated SQLite.

Never reads production config or data. Run only after builds have completed.
"""
import argparse
from contextlib import closing
import json
import os
import pathlib
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
import urllib.error
import psutil

HERE = pathlib.Path(__file__).resolve().parent


def run(binary):
    with tempfile.TemporaryDirectory(prefix="csr-notes-http-") as directory:
        root=pathlib.Path(directory); (root/'sources').mkdir()
        (root/'catalog.json').write_bytes(subprocess.check_output([str(binary),'--catalog']))
        (root/'project-notes-policy.txt').write_bytes(subprocess.check_output([str(binary),'--notes-policy']))
        with socket.socket() as probe:
            probe.bind(('127.0.0.1',0)); port=probe.getsockname()[1]
        token=secrets.token_urlsafe(32)
        cfg={'binary':str(binary),'db_path':str(root/'csr-engine.db'),'projects_dir':str(root/'sources'),
            'source_roots':[str(root/'sources')],'port':port,'token':token,'idle_seconds':120,
            'model_idle_seconds':120,'response_token_budget':2000,'request_timeout_seconds':120}
        (root/'config.json').write_text(json.dumps(cfg),encoding='utf8')
        url=f'http://127.0.0.1:{port}'
        def request(path,body=None):
            req=urllib.request.Request(url+path,data=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
            with urllib.request.urlopen(req,timeout=150) as response:return json.load(response)
        def rpc(method,params):
            data=request('/mcp/',{'jsonrpc':'2.0','id':1,'method':method,'params':params})
            assert 'error' not in data,data
            return data['result']
        def tool(name,args,error=False):
            result=rpc('tools/call',{'name':name,'arguments':args})
            if error:
                assert result.get('isError'),result
                return result
            assert not result.get('isError'),result
            text=result['content'][0]['text']
            value=json.loads(text)
            if isinstance(value,dict) and value.get('format')=='json-text-page':
                full=''
                while True:
                    full+=value['content']
                    if value['next_offset'] is None:break
                    value=json.loads(rpc('tools/call',{'name':'csr_read_page','arguments':{'page_id':value['page_id'],'offset':value['next_offset']}})['content'][0]['text'])
                return json.loads(json.loads(full)['content'][0]['text'])
            return value
        def read(action,**args):return tool('csr_project_notes',{'action':action,**args})
        project='synthetic/http-notes'
        draft={'title':'Schedule permissions','kind':'decision','status':'approved',
            'content':'Los empleados solo pueden modificar sus propios turnos.',
            'source':'conversation:synthetic-http/message:2; explicitly approved fixture',
            'change_reason':'Synthetic approval, not a product rule'}
        log=(root/'server.log').open('wb')
        process=subprocess.Popen([sys.executable,str(HERE/'service.py'),'--config',str(root/'config.json')],
            stdout=log,stderr=log,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
        try:
            deadline=time.monotonic()+30
            while True:
                try:request('/status');break
                except (OSError,urllib.error.URLError):
                    if process.poll() is not None or time.monotonic()>deadline:raise RuntimeError('Isolated HTTP server failed to start')
                    time.sleep(.1)
            initialized=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'synthetic-http-check','version':'1'}})
            assert 'Never store credentials' in initialized['instructions']
            catalog=rpc('tools/list',{})['tools'];assert len(catalog)==18
            assert '$ref' not in json.dumps(catalog)
            assert next(t for t in catalog if t['name']=='csr_project_notes')['annotations']['readOnlyHint']
            assert not next(t for t in catalog if t['name']=='csr_save_project_note')['annotations']['readOnlyHint']
            bad={**draft,'status':'aprobado'}
            tool('csr_save_project_note',{'project':project,'topic_key':'decision/permissions','note':bad,'expected_revision':0,'lookup_token':'x'},error=True)
            assert request('/status')['worker_pid'] is None
            lookup=read('search',project=project,query='schedule permissions')
            saved=tool('csr_save_project_note',{'project':project,'topic_key':'decision/permissions','note':draft,'expected_revision':0,'lookup_token':lookup['lookup_token']})
            assert saved['revision']==1
            pid=request('/status')['worker_pid']
            found=read('search',project=project,query='Quien puede cambiar el horario de un trabajador')
            assert found['results'][0]['topic_key']=='decision/permissions' and 'revision' not in found['results'][0]
            changed={**draft,'content':'Supervisors can edit every shift. '+('Verified fixture details. '*200)}
            args={'project':project,'topic_key':'decision/permissions','note':changed,'expected_revision':1,'lookup_token':found['lookup_token']}
            tool('csr_save_project_note',args,error=True)
            current=read('read',project=project,topic_key='decision/permissions',lookup_token=found['lookup_token'])
            assert current['note']['note']==draft
            assert tool('csr_save_project_note',args)['revision']==2
            assert tool('csr_save_project_note',args)['revision']==2
            assert read('read',project=project,topic_key='decision/permissions')['note']['note']==changed
            assert len(read('history',project=project,topic_key='decision/permissions')['revisions'])==2
            assert read('projects')['projects'][0]['project']==project
            assert read('list',project=project,status='approved')['results'][0]['topic_key']=='decision/permissions'
            for invalid in [bad,{key:value for key,value in draft.items() if key!='source'},{**draft,'unexpected':'no'}]:
                tool('csr_save_project_note',{**args,'note':invalid},error=True)
            assert request('/status')['worker_pid']==pid
            with closing(sqlite3.connect(root/'csr-engine.db')) as db:
                assert db.execute('PRAGMA integrity_check').fetchone()[0]=='ok'
            telemetry=(root/'retrieval.jsonl').read_text(encoding='utf8')
            assert token not in telemetry and 'Verified fixture details' not in telemetry
            print(json.dumps({'real_http_save_read_update_history':True,'semantic_search':True,
                'full_read_required':True,'malformed_arguments_keep_worker':True,'inline_schema':True,
                'read_write_annotations':True,'projects_and_status_list':True,'large_read_paging':True,'sqlite_integrity':'ok'},indent=2))
        finally:
            children=psutil.Process(process.pid).children(recursive=True) if process.poll() is None else []
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait(timeout=10)
            _,alive=psutil.wait_procs(children,timeout=10)
            if alive:raise RuntimeError('Isolated service children did not exit before fixture cleanup')
            log.close()


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--worker',type=pathlib.Path,required=True)
    run(parser.parse_args().worker.resolve(strict=True))
