"""Rebuild the shared worker's HNSW index offline, then swap it in while the worker is idle.

Incremental updates degrade the index: on 2026-10-27 one query scored 0.236 against an
exact-search best of 0.729 until a rebuild restored it. Rebuilding inside the service
would take ~8 minutes against a 600 s request timeout, so the index is built from a
consistent copy of the database by a separate worker process and only the finished
directory is swapped in. Chunks imported meanwhile are backfilled by the live worker on
its next start; a chunk re-embedded during the build keeps its older vector until its
conversation is imported again.

Run weekly by the CSR-Index-Rebuild scheduled task. Standard library only.
"""
import json, logging, os, pathlib, shutil, signal, sqlite3, subprocess, sys, threading, time, urllib.request

BASE = pathlib.Path(__file__).resolve().parent
BUILD_TIMEOUT_S = 45 * 60
IDLE_WAIT_S = 30 * 60

logging.basicConfig(filename=BASE / "rebuild-index.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")


def service_status(cfg):
    request = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}/status",
                                     headers={"Authorization": "Bearer " + cfg["token"]})
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.load(response)


def copy_database(src, dst):
    source = sqlite3.connect(f"file:{src.as_posix()}?mode=ro", uri=True)
    target = sqlite3.connect(dst)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    count = sqlite3.connect(dst).execute("SELECT count(*) FROM chunk_embeddings").fetchone()[0]
    return count


def build_index(cfg, db_copy, work):
    """Start a worker on the copy: with no index next to it, Engine::new rebuilds one."""
    env = dict(os.environ, CSR_SHARED_WORKER="1", CSR_DISABLE_RECURSIVE_HOOKS="1", CSR_RERANKER="off")
    with open(work / "worker.log", "wb") as stderr:
        proc = subprocess.Popen([cfg["binary"], str(db_copy), cfg["projects_dir"]],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, env=env,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        watchdog = threading.Timer(BUILD_TIMEOUT_S, proc.kill)
        watchdog.start()
        try:
            proc.stdin.write((json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "rebuild-index", "version": "1"}}}) + "\n").encode())
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    raise RuntimeError("worker exited before answering initialize; see worker.log")
                try:
                    if json.loads(line).get("id") == 1:
                        break
                except ValueError:
                    continue  # index-build progress on stdout
        finally:
            watchdog.cancel()
            proc.stdin.close()
            try:
                proc.wait(timeout=60)
            except subprocess.TimeoutExpired:
                proc.kill()


def wait_until_idle(cfg):
    deadline = time.monotonic() + IDLE_WAIT_S
    while True:
        status = service_status(cfg)
        if not status.get("busy"):
            return status
        if time.monotonic() > deadline:
            raise RuntimeError("worker stayed busy; index not swapped")
        time.sleep(30)


def swap(live, new, prev, worker_pid):
    if worker_pid:
        os.kill(worker_pid, signal.SIGTERM)  # TerminateProcess; the service respawns on demand
        time.sleep(3)
    if prev.exists():
        shutil.rmtree(prev)
    live.rename(prev)
    try:
        shutil.move(str(new), str(live))
    except Exception:
        if live.exists():
            shutil.rmtree(live, ignore_errors=True)
        prev.rename(live)
        raise


def main():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf8"))
    db = pathlib.Path(cfg["db_path"])
    live, prev = db.parent / "shared-index", db.parent / "shared-index.prev"
    work = pathlib.Path(os.environ.get("TEMP", str(BASE))) / "csr-index-rebuild"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    started = time.monotonic()
    try:
        db_copy = work / "csr-engine.db"
        expected = copy_database(db, db_copy)
        logging.info("database copied: %d chunk embeddings", expected)
        build_index(cfg, db_copy, work)
        manifest = json.loads((work / "shared-index" / "manifest.json").read_text(encoding="utf8"))
        built = len(manifest["chunk_id_map"])
        if built != expected or manifest["chunk_embeddings_expected"] != expected:
            raise RuntimeError(f"built index has {built} chunks, expected {expected}")
        logging.info("index built in %.0f s", time.monotonic() - started)
        status = wait_until_idle(cfg)
        swap(live, work / "shared-index", prev, status.get("worker_pid"))
        logging.info("index swapped in; previous kept at %s", prev)
        return 0
    except Exception:
        logging.exception("index rebuild failed; live index left as it was")
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
