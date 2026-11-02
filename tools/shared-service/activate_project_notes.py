"""Controlled CSR notes activation. Dry-run by default; no credential regeneration."""
import argparse
from contextlib import closing
import datetime
import hashlib
import json
import os
import pathlib
import shutil
import sqlite3
import subprocess
import sys
import time
import urllib.request
import uuid
import psutil

HERE = pathlib.Path(__file__).resolve().parent
BASE = pathlib.Path.home() / ".local/share/csr-shared-service"
TASK = "CSR-Shared-Service"
PAUSE_TASKS = ("CSR-Conversations-AutoImport", "CSR-Index-Rebuild")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def powershell(script):
    result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                            capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Scheduled-task operation failed; credential output suppressed")
    return result.stdout.strip()


def task_info(name):
    return json.loads(powershell("$t=Get-ScheduledTask -TaskName '" + name +
        "' -ErrorAction Stop; [pscustomobject]@{State=[string]$t.State;Enabled=$t.Settings.Enabled;"
        "Execute=$t.Actions[0].Execute;Arguments=$t.Actions[0].Arguments}|ConvertTo-Json -Compress"))


def task(command, name):
    powershell(command + " -TaskName '" + name + "' -ErrorAction Stop | Out-Null")


def private_directory(path):
    path.mkdir()
    sid = powershell("[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value")
    result = subprocess.run(["icacls.exe", str(path), "/inheritance:r", "/grant:r",
        f"*{sid}:(OI)(CI)F", "*S-1-5-18:(OI)(CI)F"], capture_output=True,
        creationflags=subprocess.CREATE_NO_WINDOW)
    if result.returncode:
        raise RuntimeError("Could not protect backup directory")


def atomic_write(path, data, expected=None):
    if expected is not None and (not path.exists() or digest(path) != expected):
        raise RuntimeError("Concurrent configuration change: " + path.name)
    tmp = path.with_name(path.name + ".notes-" + uuid.uuid4().hex + ".tmp")
    tmp.write_bytes(data)
    if path.exists():
        import win32security
        security = win32security.GetFileSecurity(str(path), win32security.DACL_SECURITY_INFORMATION)
        win32security.SetFileSecurity(str(tmp), win32security.DACL_SECURITY_INFORMATION, security)
    os.replace(tmp, path)


def verified_processes(status, base, target, process_factory=psutil.Process, process_iter=psutil.process_iter):
    """Recognize a venv launcher AND its real interpreter; never kill by name."""
    launcher = base / ".venv/Scripts/pythonw.exe"
    venv = base / ".venv/pyvenv.cfg"
    settings = {}
    for line in venv.read_text(encoding="utf8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            settings[key.strip().lower()] = value.strip()
    if not settings.get("home"):
        raise RuntimeError("Missing venv runtime identity")
    runtime = pathlib.Path(settings["home"]) / "pythonw.exe"
    expected = {launcher.resolve(), runtime.resolve()}
    script = (base / "service.py").resolve()
    def is_gateway(process):
        return (pathlib.Path(process.exe()).resolve() in expected and
                any(pathlib.Path(arg.strip('"')).resolve() == script for arg in process.cmdline()[1:]))
    candidates = []
    if status.get("gateway_pid"):
        try:
            gateway = process_factory(status["gateway_pid"])
            if not is_gateway(gateway):
                raise RuntimeError("Gateway process identity changed")
            candidates.append(gateway)
        except psutil.NoSuchProcess:
            pass
    else:
        # Recovery may run after HTTP startup failed. Match the exact script/runtime.
        for process in process_iter():
            try:
                if is_gateway(process):
                    candidates.append(process)
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    owned = {}
    for gateway in candidates:
        owned[gateway.pid] = gateway
        try:
            parent = gateway.parent()
            if parent and pathlib.Path(parent.exe()).resolve() == launcher.resolve() and is_gateway(parent):
                owned[parent.pid] = parent
            for child in gateway.children():
                if pathlib.Path(child.exe()).resolve() == target.resolve():
                    owned[child.pid] = child
        except psutil.NoSuchProcess:
            pass
    if status.get("worker_pid"):
        try:
            worker = process_factory(status["worker_pid"])
            if pathlib.Path(worker.exe()).resolve() != target.resolve() or worker.ppid() not in owned:
                raise RuntimeError("Worker process identity changed")
            owned[worker.pid] = worker
        except psutil.NoSuchProcess:
            pass
    return list(owned.values())


class Operations:
    def __init__(self, base, cfg):
        self.base, self.cfg = base, cfg
    task_info = staticmethod(task_info)
    task = staticmethod(task)
    protect = staticmethod(private_directory)
    write = staticmethod(atomic_write)
    def export(self, binary, argument):
        return subprocess.check_output([str(binary), argument])
    def request(self, path, body=None):
        cfg = self.cfg
        req = urllib.request.Request(f"http://127.0.0.1:{cfg['port']}" + path,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + cfg["token"], "Content-Type": "application/json",
                     "Accept": "application/json, text/event-stream"})
        with urllib.request.urlopen(req, timeout=120) as response:
            return json.load(response)
    def processes(self, status, target):
        return verified_processes(status, self.base, target)
    def wait(self, processes):
        _, alive = psutil.wait_procs(processes, timeout=15)
        if alive:
            raise RuntimeError("Service processes still running; refusing to replace binaries")
    def backup(self, db, backup):
        with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as original, closing(sqlite3.connect(backup / "csr-engine.db")) as copied:
            original.backup(copied)
        shutil.copy2(backup / "csr-engine.db", backup / "restore-verification.db")
        with closing(sqlite3.connect(backup / "restore-verification.db")) as restored:
            if restored.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup restoration verification failed")
            return {name: restored.execute("SELECT count(*) FROM " + name).fetchone()[0]
                    for name in ("chunks", "reflections")}


def main(binary, apply, expected_sha256=None, *, base=None, home=None, operations=None,
         task_name=TASK, pause_tasks=PAUSE_TASKS):
    if operations is None and os.name != "nt":
        raise RuntimeError("Native activation is Windows-specific")
    base = (base or BASE).resolve(strict=True)
    home = home or pathlib.Path.home()
    cfg_path = base / "config.json"
    cfg_bytes = cfg_path.read_bytes()
    cfg = json.loads(cfg_bytes)
    ops = operations or Operations(base, cfg)
    target = pathlib.Path(cfg["binary"]).resolve(strict=True)
    if target.parent != base or target.name != "csr-shared-worker.exe":
        raise RuntimeError("Unexpected worker destination")
    db = pathlib.Path(cfg["db_path"]).resolve(strict=True)
    source = binary.resolve(strict=True)
    source_hash = digest(source)
    if apply and not expected_sha256:
        raise ValueError("--expected-sha256 is required with --apply")
    if expected_sha256 and source_hash != expected_sha256.lower():
        raise RuntimeError("Candidate SHA256 differs from reviewed artifact")
    catalog = ops.export(source, "--catalog")
    if not {"csr_project_notes", "csr_save_project_note"} <= {t["name"] for t in json.loads(catalog)}:
        raise RuntimeError("Candidate lacks reviewed tools")
    policy = ops.export(source, "--notes-policy")
    if policy.strip() != (HERE.parents[1] / "csr-engine/data/PROJECT_NOTES_POLICY.txt").read_bytes().strip():
        raise RuntimeError("Candidate policy differs from source")
    info = ops.task_info(task_name)
    launcher = base / ".venv/Scripts/pythonw.exe"
    if pathlib.Path(info["Execute"]).resolve() != launcher.resolve() or info["Arguments"] != '"' + str(base / "service.py") + '"':
        raise RuntimeError("Service task identity differs from expected deployment")
    current = ops.request("/status")
    ops.processes(current, target)  # This validation MUST run in dry-run too.
    paused = {name: ops.task_info(name) for name in pause_tasks}
    if any(item["State"] == "Running" for item in paused.values()):
        raise RuntimeError("Importer/rebuild running; retry at an idle boundary")
    hook_paths = [home / ".codex/hooks.json", home / ".claude/settings.json"]
    originals = {path: path.read_bytes() for path in hook_paths}
    planned = {}
    command = '"' + str(launcher.with_name("python.exe")) + '" "' + str(base / "hook.py") + '" session-start'
    for path, data in originals.items():
        value = json.loads(data)
        groups = value.setdefault("hooks", {}).setdefault("SessionStart", [])
        if not any(h.get("command") == command for group in groups for h in group.get("hooks", [])):
            groups.append({"matcher": "startup|resume|clear|compact",
                           "hooks": [{"type": "command", "command": command, "timeout": 5}]})
        planned[path] = (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode()
    preflight = {"preflight": "ok", "candidate_sha256": source_hash, "process_identity_verified": True,
                 "active_worker_busy": current["busy"], "database_bytes": db.stat().st_size, "apply": apply}
    print(json.dumps(preflight), flush=True)
    if not apply:
        return preflight
    if shutil.disk_usage(base).free < db.stat().st_size * 3 + 500_000_000:
        raise RuntimeError("Insufficient protected backup/recovery space")
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    backup = base / "backups" / ("before-project-notes-" + stamp)
    ops.protect(backup)
    stage = backup / "candidate"
    stage.mkdir()
    for name, path in {"csr-shared-worker.exe": source, "service.py": HERE / "service.py", "hook.py": HERE / "hook.py"}.items():
        shutil.copy2(path, stage / name)
    if digest(stage / target.name) != expected_sha256.lower():
        raise RuntimeError("Staged binary differs from reviewed SHA256")
    (stage / "catalog.json").write_bytes(catalog)
    (stage / "project-notes-policy.txt").write_bytes(policy)
    original_assets = {path.name: path.read_bytes() if path.exists() else None
                       for path in (base / name for name in ("csr-shared-worker.exe", "service.py", "hook.py", "catalog.json", "project-notes-policy.txt"))}
    for name, data in original_assets.items():
        if data is not None:
            (backup / name).write_bytes(data)
    (backup / "config.json").write_bytes(cfg_bytes)
    for index, data in enumerate(originals.values()):
        (backup / f"client-{index}.json").write_bytes(data)
    enabled, installed_assets, written_hooks = [], [], []
    stopped = False
    last_status = current
    receipt = {"activated": False, "backup": str(backup), "candidate_sha256": source_hash,
               "database_restored_to_live": False, "client_hook_execution_verified": False}
    failure = None
    resume_allowed = True

    def wait_status():
        deadline = time.monotonic() + 30
        while True:
            try:
                return ops.request("/status")
            except OSError:
                if time.monotonic() > deadline:
                    raise RuntimeError("Service did not start")
                time.sleep(.2)

    def call_tool(name, args):
        reply = ops.request("/mcp/", {"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                                     "params": {"name": name, "arguments": args}})
        result = reply.get("result", {})
        if "error" in reply or result.get("isError") or not result.get("content"):
            raise RuntimeError("Worker postflight failed: " + name)
        return result

    def stop_service():
        nonlocal stopped, last_status
        try:
            snapshot = ops.request("/status")
        except OSError:
            snapshot = {}  # Exact script/runtime discovery if HTTP never became ready.
        owned = ops.processes(snapshot, target)
        ops.task("Stop-ScheduledTask", task_name)
        stopped = True
        ops.wait(owned)

    try:
        for name, item in paused.items():
            if item["Enabled"]:
                ops.task("Disable-ScheduledTask", name)
                enabled.append(name)
        if any(ops.task_info(name)["State"] == "Running" for name in pause_tasks):
            raise RuntimeError("Importer/rebuild started during preflight; retry later")
        deadline = time.monotonic() + 120
        while True:
            last_status = ops.request("/status")
            if not last_status["busy"] and not last_status["queued_imports"]:
                break
            if time.monotonic() > deadline:
                raise RuntimeError("Service did not become idle")
            time.sleep(.5)
        counts = ops.backup(db, backup)
        print(json.dumps({"backup_verified": True, "restored_row_counts": counts}), flush=True)
        last_status = ops.request("/status")
        if last_status["busy"] or last_status["queued_imports"] or cfg_path.read_bytes() != cfg_bytes:
            raise RuntimeError("New work/configuration change; abort before stopping")
        # Validate the complete set BEFORE marking or changing anything.
        for name, data in original_assets.items():
            path = base / name
            if (path.read_bytes() if path.exists() else None) != data:
                raise RuntimeError("Concurrent service asset change: " + name)
        stop_service()
        for name, data in original_assets.items():
            path = base / name
            if (path.read_bytes() if path.exists() else None) != data:
                raise RuntimeError("Concurrent service asset change: " + name)
        for name, data in original_assets.items():
            installed_assets.append(name)
            ops.write(base / name, (stage / name).read_bytes(),
                      hashlib.sha256(data).hexdigest() if data is not None else None)
        if digest(target) != expected_sha256.lower():
            raise RuntimeError("Installed worker SHA256 mismatch")
        for path, data in planned.items():
            written_hooks.append(path)
            ops.write(path, data, hashlib.sha256(originals[path]).hexdigest())
        ops.task("Start-ScheduledTask", task_name)
        stopped = False
        last_status = wait_status()
        init = ops.request("/mcp/", {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                       "clientInfo": {"name": "notes-activation", "version": "1"}}})
        if policy.decode().strip() not in init["result"]["instructions"]:
            raise RuntimeError("Updated policy not delivered")
        tools = ops.request("/mcp/", {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})["result"]["tools"]
        if not {"csr_project_notes", "csr_save_project_note"} <= {t["name"] for t in tools}:
            raise RuntimeError("Updated tools not delivered")
        call_tool("csr_project_notes", {"action": "projects"})
        call_tool("csr_reflect_on_past", {"query": "csr_project_notes", "project": "all", "limit": 1})
        last_status = ops.request("/status")
        if not last_status.get("worker_pid") or last_status["worker_pid"] == current.get("worker_pid"):
            raise RuntimeError("New live worker identity not established")
        ops.processes(last_status, target)
        if cfg_path.read_bytes() != cfg_bytes:
            raise RuntimeError("Credential/config preservation failed")
        receipt.update(activated=True, tools=len(tools), policy_matches=True, config_preserved=True,
                       live_worker_opened=True, legacy_search_verified=True, worker_pid=last_status["worker_pid"],
                       hooks_configured=[str(path) for path in written_hooks], rollback_failed=False)
    except BaseException as error:
        failure = type(error).__name__
        errors = []
        def attempt(label, operation, retries=1):
            for index in range(retries):
                try:
                    operation()
                    return True
                except BaseException as restore_error:
                    if index + 1 == retries:
                        errors.append(label + ":" + type(restore_error).__name__)
                    else:
                        time.sleep(.15 * (index + 1))
            return False
        if installed_assets or written_hooks:
            safe = attempt("stop-for-rollback", stop_service)
            if safe:
                for name in reversed(installed_assets):
                    old = original_assets[name]
                    if old is not None:
                        if (base / name).read_bytes() == old:
                            continue
                        new_hash = digest(stage / name)
                        attempt("restore-" + name, lambda name=name, old=old, new_hash=new_hash:
                                ops.write(base / name, old, new_hash), retries=3)
                for path in written_hooks:
                    if path.read_bytes() == originals[path]:
                        continue
                    attempt("restore-" + path.name, lambda path=path:
                            ops.write(path, originals[path], hashlib.sha256(planned[path]).hexdigest()), retries=3)
            else:
                errors.append("assets-not-restored:process-exit-unverified")
        # Always attempt restart, even if one restore failed.
        restarted = attempt("restart-service", lambda: ops.task("Start-ScheduledTask", task_name))
        if restarted:
            stopped = False
            restarted = attempt("verify-recovery", lambda: (wait_status(), call_tool(
                "csr_reflect_on_past", {"query": "csr_project_notes", "project": "all", "limit": 1})))
        resume_allowed = restarted and not errors
        receipt.update(activated=False, failure_type=failure, rollback_failed=bool(errors),
                       rollback_errors=errors, service_restarted=restarted)
    finally:
        task_errors, resumed = [], []
        if resume_allowed:
            for name in enabled:
                try:
                    ops.task("Enable-ScheduledTask", name)
                    resumed.append(name)
                except BaseException as error:
                    task_errors.append(name + ":" + type(error).__name__)
        receipt.update(tasks_resumed=resumed, task_restore_errors=task_errors,
                       tasks_left_paused=[name for name in enabled if name not in resumed])
        if task_errors:
            receipt["service_postflight_passed"] = receipt["activated"]
            receipt["activated"] = False
        (backup / "activation-receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf8")
        print(json.dumps(receipt, indent=2), flush=True)
    if failure or receipt["task_restore_errors"]:
        raise RuntimeError("Activation/recovery incomplete; inspect the protected activation receipt")
    return receipt


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=pathlib.Path)
    parser.add_argument("--expected-sha256")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    if args.apply and not args.expected_sha256:
        parser.error("--apply requires --expected-sha256")
    main(args.worker, args.apply, args.expected_sha256)
