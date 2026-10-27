"""Verify actual native client discovery without starting an agent/model turn."""
import json,os,pathlib,queue,shutil,subprocess,threading,time
base=pathlib.Path.home()/'.local/share/csr-shared-service'
config=json.loads((base/'config.json').read_text());report={}
flags=subprocess.CREATE_NO_WINDOW
with (base/'codex-client.stderr.log').open('w',encoding='utf8') as err:
 proc=subprocess.Popen([shutil.which('codex'),'app-server','--stdio'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=err,text=True,encoding='utf8',creationflags=flags)
 received=queue.Queue()
 def reader():
  for line in proc.stdout:
   try:received.put(json.loads(line))
   except ValueError:pass
 threading.Thread(target=reader,daemon=True).start()
 def rpc(rid,method,params):
  proc.stdin.write(json.dumps({'jsonrpc':'2.0','id':rid,'method':method,'params':params})+'\n');proc.stdin.flush()
  deadline=time.time()+90
  while time.time()<deadline:
   message=received.get(timeout=max(1,deadline-time.time()))
   if message.get('id')==rid:
    assert 'error' not in message,message
    return message['result']
  raise TimeoutError(method)
 try:
  rpc(1,'initialize',{'clientInfo':{'name':'csr-migration-validation','version':'1.0'},'capabilities':{'experimentalApi':True}})
  proc.stdin.write('{"jsonrpc":"2.0","method":"initialized"}\n');proc.stdin.flush()
  result=rpc(2,'mcpServerStatus/list',{'serverName':'claude-self-reflect','detail':'toolsAndAuthOnly'})
  entries=result.get('data',[])
  entry=next(e for e in entries if e.get('name')=='claude-self-reflect')
  tools=entry.get('tools',{})
  names=list(tools) if isinstance(tools,dict) else [t['name'] for t in tools]
  assert 'csr_read_page' in names and 'csr_reflect_on_past' in names,names
  report['codex']={'connected':True,'tool_count':len(names),'has_paged_retrieval':True,'auth_status':entry.get('authStatus')}
 finally:
  proc.stdin.close()
  try:proc.wait(timeout=10)
  except subprocess.TimeoutExpired:proc.kill();proc.wait()
result=subprocess.run([shutil.which('claude'),'mcp','get','claude-self-reflect'],capture_output=True,text=True,encoding='utf8',errors='replace',timeout=60,creationflags=flags)
output=(result.stdout+'\n'+result.stderr).replace(config['token'],'[redacted]')
(base/'claude-client-validation.txt').write_text(output,encoding='utf8')
report['claude']={'exit_code':result.returncode,'connected':'Connected' in output or 'connected' in output,'shared_url_seen':f"127.0.0.1:{config['port']}" in output}
assert result.returncode==0 and report['claude']['connected'],output
(base/'native-client-validation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps(report,indent=2))
