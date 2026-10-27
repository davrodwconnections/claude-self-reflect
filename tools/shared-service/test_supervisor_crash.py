import pathlib,json,urllib.request,psutil,time
p=pathlib.Path(r'C:\Users\davro\.local\share\csr-shared-service\test-fixture');c=json.loads((p/'config.json').read_text())
def req(path,body=None):
 r=urllib.request.Request('http://127.0.0.1:18766'+path,data=json.dumps(body).encode() if body else None,headers={'Authorization':'Bearer '+c['token'],'Content-Type':'application/json','Accept':'application/json, text/event-stream'})
 with urllib.request.urlopen(r) as f:return json.load(f)
req('/mcp/',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'csr_reflect_on_past','arguments':{'query':'MARIPOSA-CUBANA','project':'all'}}})
s=req('/status');child=s['worker_pid'];parent=s['gateway_pid']
psutil.Process(parent).kill();time.sleep(1)
assert not psutil.pid_exists(child),'orphan worker survived'
v=json.loads((p/'validation.json').read_text());v['supervisor_crash_leaves_no_orphan']=True;(p/'validation.json').write_text(json.dumps(v,indent=2))
print('Supervisor crash: child terminated by Windows Job Object')
