"""Scheduled import adapter: reuse shared worker; emit the existing manifest protocol."""
import json, pathlib, sys, urllib.request
BASE=pathlib.Path(__file__).resolve().parent

def main():
    cfg=json.loads((BASE/'config.json').read_text(encoding='utf8'))
    manifest=json.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
    if pathlib.Path(manifest['db_path']).resolve()!=pathlib.Path(cfg['db_path']).resolve():
        raise ValueError('Manifest database does not match shared service')
    errors=0
    for source in manifest['files']:
        try:
            request=urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/ingest",data=json.dumps(source).encode(),headers={'Content-Type':'application/json','Authorization':'Bearer '+cfg['token']})
            with urllib.request.urlopen(request,timeout=660) as response:result=json.load(response)
        except Exception as error:
            errors+=1
            result={'path':source['path'],'status':'error','error':str(error)}
        print(json.dumps(result),flush=True)
    return int(errors>0)

if __name__=='__main__':sys.exit(main())
