# Shared CSR service on Linux

Service dir: `~/.local/share/csr-shared-service`. Database: `~/.claude-self-reflect/csr-engine.db`.
Bearer token: the file named by `token_file` in `config.json` (mode 600).

## Units (systemd --user, linger on)

- `csr-shared.service`: MCP gateway on 127.0.0.1 and the Tailscale address, port 18765.
- `csr-import.timer`: every 2 minutes, `pull_transcripts.py` then `scan_import.py`.
- `csr-index-rebuild.timer`: Sunday 04:00, offline HNSW rebuild, plus a database backup.
- `csr-db-backup.timer`: daily 03:30, database backup in `db-backups`, 7 kept.

Install: copy `linux/systemd/*` to `~/.config/systemd/user/`, `systemctl --user daemon-reload`,
then `systemctl --user enable --now csr-shared.service csr-import.timer csr-index-rebuild.timer csr-db-backup.timer`.

## Build

`CARGO_TARGET_DIR=~/.cache/csr-build nice -n 15 cargo build --release --locked --bin csr-shared-worker`.
Check first that `src/embeddings/mod.rs` still names `ParaphraseMLMiniLML12V2Q`. Requires
`build-essential pkg-config libssl-dev`. The worker runs ONNX on the CPU here; DirectML is Windows-only.

## Transcripts from the PC

The laptop pulls them: the PC has no rsync and needs no change. `pull_transcripts.py` lists
`*.jsonl` through SSH and fetches changed files with sftp into `inbox/ainhoa/`, in the same
folder layout, so project names and chunk IDs stay the same. The mirror never deletes. Remove
`pull` from `config.json` to stop it. A PC that is off is skipped and retried on the next pass.

## Moving the database

`linux/cutover_resync.sh <source>` replaces the database with a fresh copy, then re-imports.
Reflections and project notes exist only in the database, so transcripts cannot rebuild them.
