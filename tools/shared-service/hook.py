"""Lightweight Claude/Codex hooks: enqueue local transcripts; prompt-submit memory hints."""
import json, pathlib, sys, urllib.request
BASE=pathlib.Path(__file__).resolve().parent

# Cross-encoder score a hint must reach. Calibrated on the live corpus: see
# hint-calibration.json next to this file.
HINT_THRESHOLD=None
HINT_TIMEOUT_S=4
MIN_PROMPT_CHARS=20

def post(cfg, route, body, timeout):
    request=urllib.request.Request(f"http://127.0.0.1:{cfg['port']}{route}",data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
    with urllib.request.urlopen(request,timeout=timeout) as response:return json.load(response)

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
    hits=[h for h in post(cfg,'/hint',body,HINT_TIMEOUT_S).get('hits',[]) if h.get('rerank',float('-inf'))>=threshold]
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
        print('CSR shared memory: consult claude-self-reflect proactively when past decisions or work matter. Start with 3 results; expand only for missing evidence. No automatic history summary.')
        return
    try:
        event=json.load(sys.stdin)
        cfg=json.loads((BASE/'config.json').read_text(encoding='utf8'))
        if kind=='prompt-submit':
            prompt_hint(event,cfg)
            return
        path=event.get('transcript_path')
        if not path:return
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
