"""Fetch another machine's new transcripts over SSH into a local mirror. Read-only there.

The remote machine needs only OpenSSH and Python: it has no rsync. Files are listed by a
short remote script fed through stdin, changed ones are fetched with sftp, and each
replaces its mirror copy only once complete. The mirror never deletes: Codex removes old
transcripts, and this copy may then be the last one. Standard library only.
"""
import fcntl, json, logging, os, pathlib, subprocess, sys, time

BASE = pathlib.Path(__file__).resolve().parent
logging.basicConfig(filename=BASE / "pull-transcripts.log", level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
SSH_OPTIONS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15", "-o", "LogLevel=ERROR"]
LISTER = r"""
import json, os, sys
out = []
for name, root in json.loads(ROOTS).items():
    for folder, _, files in os.walk(root):
        for file in files:
            if file.endswith(".jsonl"):
                path = os.path.join(folder, file)
                try:
                    status = os.stat(path)
                except OSError:
                    continue
                out.append([name, os.path.relpath(path, root).replace(os.sep, "/"), status.st_size, status.st_mtime_ns])
sys.stdout.write(json.dumps(out))
"""


def sftp_quote(text):
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def remote_path(root, relative):
    path = root.replace("\\", "/").rstrip("/") + "/" + relative
    return ("/" + path) if path[1:2] == ":" else path


def list_remote(pull):
    script = "ROOTS = " + repr(json.dumps(pull["roots"])) + "\n" + LISTER
    result = subprocess.run(["ssh", *SSH_OPTIONS, pull["host"], pull["python"], "-"], input=script.encode(),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=120)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.decode(errors="replace")[:300])
    return json.loads(result.stdout)


def main():
    cfg = json.loads((BASE / "config.json").read_text(encoding="utf8"))
    pull = cfg.get("pull")
    if not pull:
        return 0
    state_path = BASE / "pull-state.json"
    state = json.loads(state_path.read_text(encoding="utf8")) if state_path.exists() else {}
    mirror = pathlib.Path(pull["mirror"]).expanduser()
    started = time.monotonic()
    try:
        listing = list_remote(pull)
    except (subprocess.SubprocessError, OSError, RuntimeError, ValueError) as error:
        logging.warning("remote not reachable, next pass retries: %s", str(error)[:200])
        return 0
    changed = [row for row in listing if state.get(f"{row[0]}/{row[1]}") != row[2:]]
    if not changed:
        return 0
    batch, targets = [], []
    for name, relative, size, mtime in changed[:int(pull.get("batch_files", 200))]:
        target = mirror / name / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(target.name + ".part")
        batch.append(f"-get -p {sftp_quote(remote_path(pull['roots'][name], relative))} {sftp_quote(str(partial))}")
        targets.append((f"{name}/{relative}", target, partial, size, mtime))
    subprocess.run(["sftp", *SSH_OPTIONS, "-q", "-b", "-", pull["host"]],
                   input=("\n".join(batch) + "\n").encode(), stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, timeout=3000)
    fetched, failed = 0, 0
    for key, target, partial, size, mtime in targets:
        # A file still being written may be longer than listed, never shorter.
        if partial.exists() and partial.stat().st_size >= size:
            os.replace(partial, target)
            state[key] = [size, mtime]
            fetched += 1
        else:
            partial.unlink(missing_ok=True)
            failed += 1
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state), encoding="utf8")
    os.replace(temporary, state_path)
    logging.info("listed=%d changed=%d fetched=%d failed=%d seconds=%.1f",
                 len(listing), len(changed), fetched, failed, time.monotonic() - started)
    return 0


if __name__ == "__main__":
    with (BASE / "pull-transcripts.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            sys.exit(0)
        sys.exit(main())
