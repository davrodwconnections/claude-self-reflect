"""Periodic importer for Linux: find changed transcripts and hand them to the shared service.

Same job as csr-conversation-automation/worker.py on Windows. Unchanged files are
skipped by size and mtime. Nothing here deletes: a transcript that disappears keeps its
chunks. Standard library only.
"""
import fcntl, json, logging, os, pathlib, sys, time, urllib.request

BASE = pathlib.Path(__file__).resolve().parent
STATE = BASE / "scan-state.json"
logging.basicConfig(filename=BASE / "scan-import.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def load_config():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf8"))
    cfg["token"] = pathlib.Path(cfg["token_file"]).expanduser().read_text(encoding="utf8").strip()
    return cfg


def project_of(path, root, kind):
    if kind == "claude":
        return path.relative_to(root).parts[0]
    try:
        with path.open(encoding="utf8") as f:
            for _ in range(15):
                line = f.readline()
                if not line:
                    break
                event = json.loads(line)
                if event.get("type") == "session_meta":
                    return event.get("payload", {}).get("cwd", "codex-unscoped")
    except (ValueError, OSError):
        pass
    return "codex-unscoped"


def discover(cfg):
    sources = {}
    for entry in cfg["scan_roots"]:
        root = pathlib.Path(entry["path"]).expanduser()
        if not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            if path.is_symlink() or not path.is_file():
                continue
            status = path.stat()
            sources[str(path.resolve())] = {"root": root, "kind": entry["kind"],
                                            "stamp": [status.st_size, status.st_mtime_ns]}
    return sources


def ingest(cfg, path, project):
    request = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/ingest",
        data=json.dumps({"path": path, "project": project}).encode(),
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + cfg["token"]})
    with urllib.request.urlopen(request, timeout=660) as response:
        return json.load(response)


def main():
    cfg = load_config()
    state = json.loads(STATE.read_text(encoding="utf8")) if STATE.exists() else {}
    sources = discover(cfg)
    pending = [p for p, s in sources.items() if state.get(p) != s["stamp"]]
    pending.sort(key=lambda p: sources[p]["stamp"][1], reverse=True)
    started, failed, chunks = time.monotonic(), 0, 0
    for path in pending:
        source = sources[path]
        try:
            project = project_of(pathlib.Path(path), source["root"].resolve(), source["kind"])
            result = ingest(cfg, path, project)
            chunks += int(result.get("chunks_updated", 0) or 0)
            state[path] = source["stamp"]
            temporary = STATE.with_suffix(".tmp")
            temporary.write_text(json.dumps(state), encoding="utf8")
            os.replace(temporary, STATE)
        except Exception:
            failed += 1
            logging.exception("import failed for %s; next pass retries", path)
    if pending:
        logging.info("sources=%d pending=%d failed=%d new_chunks=%d seconds=%.1f",
                     len(sources), len(pending), failed, chunks, time.monotonic() - started)
    return int(failed > 0)


if __name__ == "__main__":
    with (BASE / "scan-import.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit(0)
        sys.exit(main())
