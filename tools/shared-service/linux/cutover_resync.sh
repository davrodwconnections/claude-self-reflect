#!/bin/bash
# Final sync for the cutover: replaces the laptop database with a fresh copy of the PC's.
#
# Needed because reflections and project notes live only in the database, so anything
# saved on the PC after the first copy is not in the transcripts. Run it after the PC
# service has stopped and a consistent copy exists on the PC. The service is down for
# the index build, 8 to 11 minutes.
#
# usage: cutover_resync.sh <source>
#   source: a local path, or ainhoa:D:/Proyectos/_csr-backups/cutover/csr-engine.db
# The sha256 file next to the source (<source>.sha256) is checked when it exists.
set -euo pipefail
SOURCE=${1:?source database required}
S=~/.local/share/csr-shared-service
D=~/.claude-self-reflect
STAGE=$S/migration/cutover
STAMP=$(date +%Y%m%d-%H%M%S)
mkdir -p "$STAGE"
rm -f "$STAGE/csr-engine.db" "$STAGE/csr-engine.db.sha256"

scp -q -o BatchMode=yes "$SOURCE" "$STAGE/csr-engine.db"
if scp -q -o BatchMode=yes "$SOURCE.sha256" "$STAGE/csr-engine.db.sha256" 2>/dev/null; then
  (cd "$STAGE" && sed 's/ \*/  /' csr-engine.db.sha256 | sha256sum -c -)
fi
[ "$(sqlite3 "$STAGE/csr-engine.db" 'PRAGMA quick_check;')" = ok ]

systemctl --user stop csr-import.timer csr-import.service csr-shared.service
sqlite3 "$D/csr-engine.db" 'PRAGMA wal_checkpoint(TRUNCATE);' > /dev/null
mv "$D/csr-engine.db" "$S/backups/csr-engine.before-cutover-$STAMP.db"
rm -f "$D/csr-engine.db-wal" "$D/csr-engine.db-shm"
mv "$STAGE/csr-engine.db" "$D/csr-engine.db"

# An index with more rows than the new database forces a rebuild at worker start, inside
# a request timeout. Build it here instead, from the stored vectors; nothing is embedded.
rm -rf "$D/shared-index.prev"
mv "$D/shared-index" "$D/shared-index.prev"
# With stdin closed the worker exits with an error after the build; the manifest check below decides.
nice -n 5 "$S/csr-shared-worker" "$D/csr-engine.db" ~/.claude/projects < /dev/null > /dev/null 2>&1 || true
python3 - <<P
import json, os, sqlite3
m = json.load(open(os.path.expanduser("$D/shared-index/manifest.json")))
n = sqlite3.connect(os.path.expanduser("$D/csr-engine.db")).execute("select count(*) from chunk_embeddings").fetchone()[0]
assert len(m["chunk_id_map"]) == n, (len(m["chunk_id_map"]), n)
print("index ok:", n, "chunks")
P

# Every transcript is compared again; unchanged chunks reuse their stored vectors.
rm -f "$S/scan-state.json"
systemctl --user start csr-shared.service csr-import.timer
systemctl --user start --no-block csr-import.service
echo "Database replaced, service up. The import pass now runs in the background."
