"""Regression: non-JSON native library progress must not break MCP startup."""
import asyncio,importlib.util,json,pathlib,sys,tempfile
spec=importlib.util.spec_from_file_location('shared_service',pathlib.Path(__file__).with_name('service.py'))
service=importlib.util.module_from_spec(spec);spec.loader.exec_module(service)
async def run():
 with tempfile.TemporaryDirectory() as directory:
  root=pathlib.Path(directory);script=root/'fake_worker.py'
  script.write_text('''import json,sys
print('[HNSW] building index',flush=True)
for line in sys.stdin:
 m=json.loads(line)
 if 'id' not in m:continue
 result={'protocolVersion':'2025-11-25','capabilities':{},'serverInfo':{'name':'fixture','version':'1'}} if m['method']=='initialize' else {'content':[{'type':'text','text':'ok'}]}
 print(json.dumps({'jsonrpc':'2.0','id':m['id'],'result':result}),flush=True)
''')
  worker=service.Worker({'binary':sys.executable,'db_path':str(script),'projects_dir':'unused','request_timeout_seconds':10,'idle_seconds':1},root)
  result=await worker.call('probe',{})
  assert result['content'][0]['text']=='ok'
  assert worker.starts==1
  await worker._stop()
 print('Non-protocol startup output regression: passed')
asyncio.run(run())
