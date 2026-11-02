"""Isolated HTTP/MCP discovery, policy, auth, preview budget and paging checks.

Uses the candidate's real catalog and a fixture worker, never the live service.
"""
import argparse
import asyncio
import importlib.util
import json
import pathlib
import subprocess
import tempfile

from starlette.testclient import TestClient

HERE = pathlib.Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("notes_gateway", HERE / "service.py")
service = importlib.util.module_from_spec(spec)
spec.loader.exec_module(service)


class FixtureWorker:
    last_args = None

    def __init__(self, cfg, base):
        self.lock = asyncio.Lock()

    async def call(self, name, args):
        FixtureWorker.last_args = args
        text = "synthetic-note-evidence " * 1500
        return {"content": [{"type": "text", "text": text}], "isError": False}

    async def reap(self):
        await asyncio.Event().wait()

    async def _stop(self):
        pass

    def status(self):
        return {"worker_pid": None}


def run(binary):
    catalog = subprocess.check_output([str(binary), "--catalog"])
    with tempfile.TemporaryDirectory(prefix="csr-notes-gateway-") as directory:
        root = pathlib.Path(directory)
        (root / "catalog.json").write_bytes(catalog)
        cfg = {"token": "synthetic-fixture-only", "port": 18765,
            "source_roots": [str(root)], "response_token_budget": 2000}
        service.Worker = FixtureWorker
        app = service.create_app(cfg, root)
        headers = {"Authorization": "Bearer synthetic-fixture-only",
            "Accept": "application/json, text/event-stream"}
        with TestClient(app, base_url="http://127.0.0.1:18765") as client:
            assert client.get("/status").status_code == 401
            assert client.get("/status", headers={**headers, "Origin": "https://example.invalid"}).status_code == 403

            def rpc(method, params):
                response = client.post("/mcp/", headers=headers,
                    json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
                assert response.status_code == 200, response.text
                result = response.json()
                assert "error" not in result, result
                return result["result"]

            initialized = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                "clientInfo": {"name": "synthetic-notes-check", "version": "1"}})
            assert "csr_project_notes" in initialized["instructions"]
            tools = rpc("tools/list", {})["tools"]
            assert len(tools) == 18
            assert '$ref' not in json.dumps(tools)
            assert not any(t["name"].startswith("csr_internal_") for t in tools)
            assert any(t["name"] == "csr_project_notes" for t in tools)
            assert client.get("/status", headers=headers).json()["worker_pid"] is None

            result = rpc("tools/call", {"name": "csr_project_notes", "arguments": {
                "action": "search", "project": "synthetic/project", "query": "not-for-telemetry", "limit": 999}})
            assert FixtureWorker.last_args["limit"] == 10
            full = ""
            count = 0
            while True:
                assert len(service.Pages(2000).encoding.encode(service.dump(result), disallowed_special=())) <= 2000
                page = json.loads(result["content"][0]["text"])
                full += page["content"]
                count += 1
                if page["next_offset"] is None:
                    break
                result = rpc("tools/call", {"name": "csr_read_page", "arguments": {
                    "page_id": page["page_id"], "offset": page["next_offset"]}})
            assert json.loads(full)["content"][0]["text"] == "synthetic-note-evidence " * 1500
            telemetry = (root / "retrieval.jsonl").read_text(encoding="utf8")
            assert "synthetic-note-evidence" not in telemetry
            assert "not-for-telemetry" not in telemetry
            assert "synthetic-fixture-only" not in telemetry
            print(json.dumps({"http_auth": True, "policy_and_catalog": True,
                "discovery_without_model": True, "bounded_lossless_pages": count,
                "metadata_only_logs": True}, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", required=True, type=pathlib.Path)
    run(parser.parse_args().worker.resolve(strict=True))
