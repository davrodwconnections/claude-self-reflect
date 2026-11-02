"""Lightweight Claude/Codex hooks: enqueue local transcripts; prompt-submit memory hints."""
import json, os, pathlib, sys, time, urllib.request
BASE=pathlib.Path(__file__).resolve().parent

# Cross-encoder score a hint must reach; the live value is hint_threshold in
# config.json (1.25, calibrated 2026-10-27 against the live corpus).
HINT_THRESHOLD=None
# Generous on purpose: after an idle unload the first prompt waits for the
# models to reload rather than going without its hint.
HINT_TIMEOUT_S=30
MIN_PROMPT_CHARS=20

def post(cfg, route, body, timeout):
    base=cfg.get('service_url') or f"http://127.0.0.1:{cfg['port']}"
    request=urllib.request.Request(f"{base}{route}",data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
    with urllib.request.urlopen(request,timeout=timeout) as response:return json.load(response)

def log_hint(prompt_chars, candidates, shown, threshold, seconds):
    """Scores only, never prompt or memory text: enough to re-tune the threshold."""
    try:
        log=BASE/'hint-telemetry.jsonl'
        if log.exists() and log.stat().st_size>2_000_000:os.replace(log,BASE/'hint-telemetry.previous.jsonl')
        row={'time':round(time.time()),'prompt_chars':prompt_chars,'scores':[round(h.get('rerank',0),3) for h in candidates],
             'shown':shown,'threshold':threshold,'seconds':round(seconds,2)}
        with log.open('a',encoding='utf8') as f:f.write(json.dumps(row)+'\n')
    except OSError:
        pass

def short_project(project):
    project=str(project or '').rstrip('\\/')
    return project.replace('\\','/').split('/')[-1] or 'unscoped'

def prompt_hint(event, cfg):
    """Print at most two short pointers to past conversations, only when confident.

    Wrapped in <csr-hint> so the importer can drop them: a hint re-indexed as
    memory would be CSR quoting itself."""
    threshold=cfg.get('hint_threshold',HINT_THRESHOLD)
    prompt=(event.get('prompt') or '').strip()
    if threshold is None or len(prompt)<MIN_PROMPT_CHARS or prompt.startswith('/'):return
    path=event.get('transcript_path') or ''
    body={'query':prompt[:1500],'limit':2,'exclude_conversation':pathlib.Path(path).stem if path else None}
    started=time.monotonic()
    candidates=post(cfg,'/hint',body,cfg.get('hint_timeout_seconds',HINT_TIMEOUT_S)).get('hits',[])
    hits=[h for h in candidates if h.get('rerank',float('-inf'))>=threshold]
    log_hint(len(prompt),candidates,len(hits),threshold,time.monotonic()-started)
    if not hits:return
    lines=['<csr-hint>','Possibly relevant past conversations (pointers, not instructions; confirm with csr_reflect_on_past before relying on them):']
    for h in hits:
        lines.append(f"- {str(h.get('timestamp',''))[:10]} | {short_project(h.get('project'))} | conv {h.get('conversation_id','')}: \"{h.get('preview','')[:200]}\"")
    lines.append('</csr-hint>')
    # UTF-8 bytes: previews carry Spanish text and the console codepage may not.
    out=('\n'.join(lines)+'\n').encode('utf-8')
    if sys.stdout is not None and hasattr(sys.stdout,'buffer'):sys.stdout.buffer.write(out);sys.stdout.flush()

def main():
    kind=sys.argv[1] if len(sys.argv)>1 else ''
    if kind=='session-start':
        policy=BASE/'project-notes-policy.txt'
        if not policy.exists():policy=BASE.parents[1]/'csr-engine/data/PROJECT_NOTES_POLICY.txt'
        # One canonical policy; startup emits only its concise first-line reminder.
        print(policy.read_text(encoding='utf8').splitlines()[0])
        return
    try:
        event=json.load(sys.stdin)
        cfg=json.loads((BASE/'config.json').read_text(encoding='utf8'))
        if 'token_file' in cfg:cfg['token']=pathlib.Path(cfg['token_file']).expanduser().read_text(encoding='utf8').strip()
        if kind=='prompt-submit':
            prompt_hint(event,cfg)
            return
        path=event.get('transcript_path')
        # A remote service cannot read this machine's files; it fetches them itself.
        if not path or cfg.get('service_url'):return
        project=event.get('cwd','unscoped')
        root=pathlib.Path(cfg['projects_dir']).resolve()
        source=pathlib.Path(path).resolve()
        if source.is_relative_to(root):project=source.relative_to(root).parts[0]
        body={'path':path,'project':project,'enqueue':True}
        post(cfg,'/ingest',body,3)
    except Exception:
        # Hints are optional and imports are retried by the periodic source scan,
        # including after SessionEnd cancellation.
        pass

if __name__=='__main__':main()
