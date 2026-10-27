"""Lightweight Claude hooks: enqueue local transcripts; no model or startup summary."""
import json, pathlib, sys, urllib.request
BASE=pathlib.Path(__file__).resolve().parent

def main():
    kind=sys.argv[1] if len(sys.argv)>1 else ''
    if kind=='session-start':
        print('CSR shared memory: consult claude-self-reflect proactively when past decisions or work matter. Start with 3 results; expand only for missing evidence. No automatic history summary.')
        return
    try:
        event=json.load(sys.stdin)
        path=event.get('transcript_path')
        if not path:return
        cfg=json.loads((BASE/'config.json').read_text(encoding='utf8'))
        project=event.get('cwd','unscoped')
        root=pathlib.Path(cfg['projects_dir']).resolve()
        source=pathlib.Path(path).resolve()
        if source.is_relative_to(root):project=source.relative_to(root).parts[0]
        body={'path':path,'project':project,'enqueue':True}
        request=urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/ingest",data=json.dumps(body).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
        with urllib.request.urlopen(request,timeout=3) as response:response.read()
    except Exception:
        # Periodic source scan provides durable retry, including SessionEnd cancellation.
        pass

if __name__=='__main__':main()
