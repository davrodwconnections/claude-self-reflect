import json,pathlib,urllib.request,concurrent.futures,time
base=pathlib.Path.home()/'.local/share/csr-shared-service';cfg=json.loads((base/'config.json').read_text())
def req(path,body=None):
 r=urllib.request.Request('http://127.0.0.1:18765'+path,data=json.dumps(body).encode() if body else None,headers={'Authorization':'Bearer '+cfg['token'],'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
 with urllib.request.urlopen(r,timeout=660) as f:return json.load(f)
for attempt in range(30):
 try:
  print('Before query:',req('/status'));break
 except OSError:
  if attempt==29:raise
  time.sleep(1)
def search(i):
 r=req('/mcp/',{'jsonrpc':'2.0','id':i,'method':'tools/call','params':{'name':'csr_reflect_on_past','arguments':{'query':'motor compartido consumo tokens conversaciones sesiones','project':'all','limit':3}}})
 assert 'error' not in r and not r['result'].get('isError'),r
 return r
with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:results=list(pool.map(search,range(3)))
status=req('/status');assert status['worker_starts']==1,status
report={'status':status,'three_queries_one_worker':True,'result_lengths':[len(json.dumps(r)) for r in results]}
(base/'production-validation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report,indent=2))
