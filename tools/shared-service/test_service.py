import concurrent.futures, json, pathlib, time, urllib.request, urllib.error, uuid, sys, sqlite3
ROOT=pathlib.Path(r'C:\Users\davro\.local\share\csr-shared-service\test-fixture')
CFG=json.loads((ROOT/'config.json').read_text())
URL=f"http://127.0.0.1:{CFG['port']}"
TOKEN=CFG['token']
def request(path, body=None, token=TOKEN):
    headers={'Authorization':'Bearer '+token,'Content-Type':'application/json','Accept':'application/json, text/event-stream'}
    req=urllib.request.Request(URL+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)
    with urllib.request.urlopen(req,timeout=180) as r:return json.load(r)
def rpc(method, params, rid=1):
    return request('/mcp/',{'jsonrpc':'2.0','id':rid,'method':method,'params':params})
def tool(name,args):
    r=rpc('tools/call',{'name':name,'arguments':args})
    assert 'error' not in r,r
    assert not r['result'].get('isError'),r
    return r['result']
report={}
try:
    request('/status',token='wrong')
    raise AssertionError('unauthorized request accepted')
except urllib.error.HTTPError as e:assert e.code==401
report['auth']=True
for i in range(3):
    r=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':f'test-{i}','version':'1'}})
    assert 'result' in r,r
    cat=rpc('tools/list',{})['result']['tools']
    assert len(cat)==16 and not any(t['name']=='csr_internal_import' for t in cat)
assert request('/status')['worker_pid'] is None
report['three_clients_discovery_without_worker']=True
cid='shared-'+uuid.uuid4().hex
source=ROOT/'sources'/f'{cid}.jsonl'
marker='MARIPOSA-CUBANA-7429'
source.write_text(json.dumps({'type':'user','timestamp':'2026-10-06T20:00:00Z','message':{'content':f'The project launch codename is {marker}. We decided to use a shared memory service.'}})+'\n',encoding='utf8')
r=request('/ingest',{'path':str(source),'project':'shared-fixture'})
assert r['status']=='ok' and r['chunks_updated']>0,r
pid=request('/status')['worker_pid']
report['import']=r
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:
    results=list(pool.map(lambda _:tool('csr_reflect_on_past',{'query':marker,'project':'all','limit':3}),range(3)))
assert all(cid in json.dumps(r) for r in results),results
assert request('/status')['worker_pid']==pid
report['three_clients_same_worker_and_new_import_visible']=True
assert request('/ingest',{'path':str(source),'project':'shared-fixture'})['chunks_updated']==0
report['idempotent_import']=True
# Long transcript forces paged response; every original character remains recoverable.
with source.open('a',encoding='utf8') as f:
    f.write(json.dumps({'type':'assistant','timestamp':'2026-10-06T20:01:00Z','message':{'content':' '.join('evidence-%04d'%i for i in range(1800))}})+'\n')
request('/ingest',{'path':str(source),'project':'shared-fixture'})
result=tool('get_full_conversation',{'conversation_id':cid,'project':'shared-fixture'})
import tiktoken
enc=tiktoken.get_encoding('cl100k_base')
pages=0; full=''
while True:
    assert len(enc.encode(json.dumps(result,ensure_ascii=False,separators=(',',':')),disallowed_special=()))<=2000
    page=json.loads(result['content'][0]['text'])
    assert page['format']=='json-text-page',page
    full+=page['content'];pages+=1
    if page['next_offset'] is None:break
    result=tool('csr_read_page',{'page_id':page['page_id'],'offset':page['next_offset']})
restored=json.loads(full)
assert 'evidence-1799' in json.dumps(restored),restored
report['bounded_lossless_pages']=pages
# Requests below are read-only: closing an HTTP connection does not own the worker.
tool('csr_reflect_on_past',{'query':marker,'project':'all'})
time.sleep(7)
assert request('/status')['worker_pid'] is None
report['idle_unloaded']=True
tool('csr_reflect_on_past',{'query':marker,'project':'all'})
newpid=request('/status')['worker_pid']
assert newpid and newpid!=pid
report['wake_retained_history']=True
# Simulated worker failure: supervisor must recover on next request.
import psutil
psutil.Process(newpid).kill()
time.sleep(.3)
assert cid in json.dumps(tool('csr_reflect_on_past',{'query':marker,'project':'all'}))
report['worker_crash_recovery']=True
try:
    request('/ingest',{'path':str(ROOT/'config.json'),'project':'bad'})
    raise AssertionError('outside source accepted')
except urllib.error.HTTPError as e:assert e.code==400
report['source_boundary']=True
report['status']=request('/status')
with sqlite3.connect(ROOT/'csr-engine.db') as db:
    assert db.execute('pragma integrity_check').fetchone()[0]=='ok'
report['sqlite_integrity']='ok'
(ROOT/'validation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report,indent=2))
