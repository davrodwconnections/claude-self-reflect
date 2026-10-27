import hashlib,json,pathlib,psutil,sqlite3,urllib.request,time,tomllib
home=pathlib.Path.home();base=home/'.local/share/csr-shared-service';c=json.loads((base/'config.json').read_text())
r=urllib.request.Request('http://127.0.0.1:18765/status',headers={'Authorization':'Bearer '+c['token']})
with urllib.request.urlopen(r) as f:status=json.load(f)
legacy=[]
for p in psutil.process_iter(['name','pid','cmdline','memory_info','ppid']):
 if p.info['name']=='csr-engine.exe' and 'hook' not in (p.info['cmdline'] or []):
  legacy.append({'pid':p.pid,'parent':p.info['ppid'],'rss_bytes':p.info['memory_info'].rss})
scanner=json.loads((home/'.local/share/csr-conversation-automation/last-run.json').read_text())
with sqlite3.connect('file:'+str(home/'.claude-self-reflect/csr-engine.db')+'?mode=ro',uri=True) as db:
 integrity=db.execute('pragma quick_check').fetchone()[0]
 chunks=db.execute('select count(*) from chunks').fetchone()[0]
assert integrity=='ok'
# Verify no credentials accidentally entered source files.
source=pathlib.Path(r'D:\Proyectos\claude-self-reflect\tools\shared-service')
assert all(c['token'] not in p.read_text(encoding='utf-8-sig') for p in source.glob('*') if p.is_file())
config=tomllib.loads((home/'.codex/config.toml').read_text(encoding='utf8'))
assert config['mcp_servers']['claude-self-reflect']['url'].endswith(':18765/mcp/')
assert json.loads((home/'.claude.json').read_text(encoding='utf8'))['mcpServers']['claude-self-reflect']['url'].endswith(':18765/mcp/')
report={'time':time.time(),'status':status,'legacy_instances_remaining':legacy,'scanner':scanner,'sqlite_quick_check':integrity,'chunks':chunks,'source_has_no_auth_token':True,'binary_sha256':hashlib.sha256((base/'csr-shared-worker.exe').read_bytes()).hexdigest(),'disk_bytes':{name:sum(p.stat().st_size for p in path.rglob('*') if p.is_file()) for name,path in {'runtime':base/'.venv','shared_index':home/'.claude-self-reflect/shared-index','backup':base/'backups'}.items()}}
(base/'final-validation.json').write_text(json.dumps(report,indent=2),encoding='utf8')
print(json.dumps({k:v for k,v in report.items() if k!='scanner'},indent=2));print('scanner:',scanner['status'],'sources:',scanner['total_sources'],'failed:',len(scanner['failed']),'changing/pending:',len(scanner['pending_next_pass']))
