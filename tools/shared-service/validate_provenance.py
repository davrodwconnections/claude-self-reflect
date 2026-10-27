import json,pathlib,subprocess,urllib.request,time
base=pathlib.Path.home()/'.local/share/csr-shared-service';cfg=json.loads((base/'config.json').read_text())
def request(path,body=None):
 r=urllib.request.Request('http://127.0.0.1:18765'+path,data=json.dumps(body).encode() if body else None,headers={'Authorization':'Bearer '+cfg['token'],'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
 with urllib.request.urlopen(r,timeout=90) as f:return json.load(f)
pid=request('/status')['worker_pid']
# Real Claude boundary-hook delivery through pythonw (no console window).
source=next((pathlib.Path.home()/'.claude/projects').glob('*/*.jsonl'))
event={'transcript_path':str(source),'cwd':'hook-validation'}
r=subprocess.run([str(base/'.venv/Scripts/pythonw.exe'),str(base/'hook.py'),'stop'],input=json.dumps(event),text=True,capture_output=True,timeout=10,creationflags=subprocess.CREATE_NO_WINDOW)
assert r.returncode==0,r.stderr
# Read this real Codex conversation with the new source-aware paged tool.
cid='rollout-2026-10-05T21-49-34-01a10ee6-e1ea-71f1-8c22-fe9cad222f55'
r=request('/mcp/',{'jsonrpc':'2.0','id':71,'method':'tools/call','params':{'name':'get_full_conversation','arguments':{'conversation_id':cid}}})
assert 'error' not in r and not r['result'].get('isError'),r
page=json.loads(r['result']['content'][0]['text']);assert page['format']=='json-text-page'
assert '.codex' in page['content'] and 'source' in page['content']
report={'hidden_hook_exit':0,'codex_original_retrieved':True,'paged':True,'same_worker':request('/status')['worker_pid']==pid,'status':request('/status')}
(base/'provenance-validation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report,indent=2))
