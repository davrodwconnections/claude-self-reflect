import datetime,json,pathlib,secrets,shutil,sqlite3,subprocess
home=pathlib.Path.home(); base=home/'.local/share/csr-shared-service'; backup=base/'backups/before-shared-service'
files={'codex-config.toml':home/'.codex/config.toml','codex-hooks.json':home/'.codex/hooks.json','codex-AGENTS.md':home/'.codex/AGENTS.md','claude.json':home/'.claude.json','claude-settings.json':home/'.claude/settings.json','claude-CLAUDE.md':home/'.claude/CLAUDE.md','import-worker.py':home/'.local/share/csr-conversation-automation/worker.py','import-config.json':home/'.local/share/csr-conversation-automation/config.json','import-state.json':home/'.local/share/csr-conversation-automation/state.json'}
for name,path in files.items():
 if (backup/name).exists():raise RuntimeError('Backup already exists: '+name)
 shutil.copy2(path,backup/name)
db=home/'.claude-self-reflect/csr-engine.db'
with sqlite3.connect(db) as source, sqlite3.connect(backup/'csr-engine.db') as target:
 source.backup(target)
 assert target.execute('pragma integrity_check').fetchone()[0]=='ok'
 print('SQLite backup and integrity check: OK')
source=pathlib.Path(r'D:\Proyectos\claude-self-reflect\tools\shared-service')
for name in ['service.py','hook.py','import_adapter.py','requirements.lock']:
 shutil.copy2(source/name,base/name)
binary=pathlib.Path(r'D:\Proyectos\claude-self-reflect\csr-engine\target\release\csr-shared-worker.exe')
shutil.copy2(binary,base/binary.name)
(base/'catalog.json').write_bytes(subprocess.check_output([str(base/binary.name),'--catalog']))
config={'binary':str(base/binary.name),'db_path':str(db),'projects_dir':str(home/'.claude/projects'),'source_roots':[str(home/'.claude/projects'),str(home/'.codex/sessions'),str(home/'.codex/archived_sessions')],'port':18765,'token':secrets.token_urlsafe(48),'idle_seconds':900,'response_token_budget':2000,'request_timeout_seconds':600}
(base/'config.json').write_text(json.dumps(config,indent=2),encoding='utf8')
# Initialize only the shared cache from the known local cache. SQLite stays canonical;
# no database or original transcript copy is introduced into the running service.
cache=home/'.claude-self-reflect/index'; shared=home/'.claude-self-reflect/shared-index'
if not shared.exists():shutil.copytree(cache,shared)
(backup/'manifest.json').write_text(json.dumps({k:str(v) for k,v in files.items()},indent=2),encoding='utf8')
print('Service files prepared; client configurations not switched yet')
