"""Synthetic integration through the real shared worker; never opens the live DB.

Usage: python test_project_notes.py --worker /absolute/path/to/candidate-worker
"""
import argparse
import asyncio
from contextlib import closing
import importlib.util
import json
import pathlib
import sqlite3
import subprocess
import tempfile
import time

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("shared_notes_service", HERE / "service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


async def run(binary):
    started = time.monotonic()
    catalog = json.loads(subprocess.check_output([str(binary), "--catalog"], text=True))
    entry = next(t for t in catalog if t["name"] == "csr_project_notes")
    assert set(entry["inputSchema"]["required"]) == {"action"}
    with tempfile.TemporaryDirectory(prefix="csr-project-notes-") as directory:
        root = pathlib.Path(directory)
        (root / "sources").mkdir()
        worker = service.Worker({"binary": str(binary), "db_path": str(root / "csr-engine.db"),
            "projects_dir": str(root / "sources"), "request_timeout_seconds": 120,
            "idle_seconds": 120, "model_idle_seconds": 120}, root)
        report = {}

        async def call(action, project="synthetic/alpha", error=False, **args):
            name = "csr_save_project_note" if action == "save" else "csr_project_notes"
            arguments = {"project": project, **args}
            if action != "save": arguments["action"] = action
            result = await worker.call(name, arguments)
            if error:
                assert result.get("isError"), result
                return result
            assert not result.get("isError"), result
            return json.loads(result["content"][0]["text"])

        def draft(text, status="approved"):
            return {"title": "Schedule permissions", "kind": "decision", "status": status,
                "content": text, "source": "conversation:synthetic/message:2; explicit user approval",
                "change_reason": "Synthetic approval for this test only"}

        try:
            lookup = await call("search", query="schedule permissions")
            assert lookup["results"] == []
            note = draft("Los empleados solo pueden modificar sus propios turnos. Los supervisores pueden editar todos.")
            saved = await call("save", topic_key="decision/schedule-permissions", note=note,
                expected_revision=0, lookup_token=lookup["lookup_token"])
            assert saved["revision"] == 1 and saved["verified"] is True
            pid = worker.proc.pid
            retry = await call("save", topic_key="decision/schedule-permissions", note=note,
                expected_revision=0, lookup_token=lookup["lookup_token"])
            assert retry == saved
            report["create_readback_and_lost_response_retry"] = True

            # Natural Spanish paraphrase, not an identifier or exact saved sentence.
            found = await call("search", query="Quien tiene permiso para cambiar los horarios de otros trabajadores")
            assert found["results"][0]["topic_key"] == "decision/schedule-permissions", found
            assert "content" not in found["results"][0]
            assert len(found["results"][0]["preview"]) <= 240
            report["spanish_paraphrase_and_compact_search"] = True

            foreign = await call("search", project="synthetic/beta", query="schedule permissions")
            assert foreign["results"] == []
            assert (await call("read", project="synthetic/beta", topic_key="decision/schedule-permissions"))["note"] is None
            await call("save", project="synthetic/beta", topic_key="decision/schedule-permissions", note=note,
                expected_revision=0, lookup_token=found["lookup_token"], error=True)
            report["project_isolation"] = True

            # A preview alone must never permit replacing a full note.
            await call("save", topic_key="decision/schedule-permissions", note=draft("preview-only overwrite"),
                expected_revision=1, lookup_token=found["lookup_token"], error=True)
            current = (await call("read", topic_key="decision/schedule-permissions",lookup_token=found["lookup_token"]))["note"]
            assert current["note"] == note
            second = draft("Employees edit their own shifts; supervisors and administrators may edit everyone's shifts.")
            saved2 = await call("save", topic_key="decision/schedule-permissions", note=second,
                expected_revision=1, lookup_token=found["lookup_token"])
            assert saved2["revision"] == 2
            await call("save", topic_key="decision/schedule-permissions", note=note,
                expected_revision=1, lookup_token=found["lookup_token"], error=True)
            history = await call("history", topic_key="decision/schedule-permissions", limit=1)
            assert history["revisions"][0]["note"] == second and history["before_revision"] == 2
            previous = await call("history", topic_key="decision/schedule-permissions", before_revision=2)
            assert previous["revisions"][0]["note"] == note
            assert previous["before_revision"] is None
            report["updates_preserve_history_and_reject_stale_writes"] = True

            # Unknown input and invalid drafts are not permission to guess.
            await call("search", project="all", query="permissions", error=True)
            invalid = draft("not approved"); invalid["source"] = ""
            await call("save", topic_key="decision/other", note=invalid,
                expected_revision=0, lookup_token=found["lookup_token"], error=True)
            assert worker.proc.pid == pid
            report["conflicts_keep_the_same_worker"] = True

            await worker._stop()
            reopened = (await call("read", topic_key="decision/schedule-permissions"))["note"]
            assert reopened["revision"] == 2 and reopened["note"] == second
            report["restart_persistence"] = True

            # Separate legacy reflections remain supported and do not approve notes.
            legacy = await worker.call("store_reflection", {"content": "Synthetic historical observation", "tags": ["test"]})
            assert not legacy.get("isError"), legacy
            with closing(sqlite3.connect(root / "csr-engine.db")) as db:
                assert db.execute("SELECT count(*) FROM reflections").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM project_notes").fetchone()[0] == 1
                assert db.execute("SELECT count(*) FROM project_note_revisions").fetchone()[0] == 2
                assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
            report["legacy_reflection_and_database_integrity"] = True
            report["seconds"] = round(time.monotonic() - started, 2)
            print(json.dumps(report, indent=2))
        finally:
            await worker._stop()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=pathlib.Path)
    args = parser.parse_args()
    binary = args.worker.resolve(strict=True)
    asyncio.run(run(binary))
