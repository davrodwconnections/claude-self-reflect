# Project notes in shared CSR

## Approved scope

Add curated notes to the existing shared CSR service, alongside automatically
imported conversations. Preserve product decisions, system explanations and
pending work by project. Search before saving; update the same topic when its
decision changes; retain sources, dates and history. Distinguish proposals,
approvals and verified implementation. Use short search previews and read full
notes only when needed. Deliver a concise agent rule through existing CSR
integrations. The coordinating agent writes; delegated workers report findings.

This work does not install Engram or replace the conversation archive. No changes
to Spectrix source or its approved product behavior are part of this task.

## Implementation

- Separate additive tables in the current CSR SQLite database; existing import
  paths cannot silently create or update product decisions.
- `csr_project_notes` reads projects/list/search/read/history; `csr_save_project_note`
  writes. Existing legacy tools retain their behavior. Shared authentication and
  token paging apply; public schemas are self-contained without `$ref`.
- Explicit stable project and topic keys; compact previews; reuse the existing
  embedding model and lexical/cosine ranking helpers. Conversation-trained
  provenance, decay and cross-encoder weights are not assumed valid for notes.
- Fifteen-minute search receipts capture the project's generation. Changed
  notes invalidate prior receipts, requiring reconciliation before another write.
- Compare expected revision, atomically store current note/history/FTS update,
  verify readback, and preserve identical retry outcomes without extra revisions.
- Source metadata and status are agent-authored evidence. The service does not
  independently verify that quoted user approval or implementation is genuine.
- After the Opus 5.5 review, updates additionally require a full-read receipt for
  the current topic/revision and search token. Paths/bare project names are
  rejected; keys come from origin owner/repo or an agreed local/name. Projects
  and filtered topics can be listed with cursors, including replaced notes.
- One policy source is compiled into Rust and deployed for MCP and startup-hook
  delivery. Private-marked input is refused; the sensitive-note recovery procedure
  is documented rather than silently deleting retained evidence.

## Validation

Validated locally on Windows on 2026-10-30 (America/New_York), branch
`backport/9.4.1-fixes`, base HEAD `8bdc07bb9fac5f7298978457d29820b6d2429fa1`
plus the uncommitted changes in this checkout. Rust builds used one compiler job
and tests used one thread. No production database or existing session was used
as a test fixture.

Commands actually run (Rust commands from `csr-engine/` with
`CARGO_TARGET_DIR=D:/Proyectos/_csr-backport/csr-engine/target-gpu`):

- `cargo test --release --lib project_notes -j 1`: 7 passed, 0 failed.
- `target-gpu/release/deps/csr_engine-1841ec2b33b69d4a.exe storage:: --test-threads=1`:
  43 passed, 0 failed, including 5 of the 7 tests above.
- `cargo build --release --bin csr-shared-worker -j 1`: passed.
- `cargo test --release --test integration test_fts5_search -j 1 -- --exact`:
  1 passed, 0 failed, 62 filtered out.
- Python `py_compile` for service, hook and the new integration scripts: passed.
- `tools/shared-service/test_protocol.py`: passed; expected non-JSON native
  startup output did not break the protocol.
- `tools/shared-service/test_project_notes.py --worker <candidate>`: passed on
  the final candidate, 39.53 seconds. Covered create/readback, identical retries,
  Spanish paraphrase recall, project isolation, stale-write refusal, revision
  history, same-worker error handling, restart persistence, legacy reflections
  and SQLite integrity.
- `tools/shared-service/test_project_notes_gateway.py --worker <candidate>`:
  passed with the real catalog and a fixture worker. Covered HTTP authentication,
  origin refusal, policy/catalog delivery without loading a model, result caps,
  four lossless pages bounded to 2,000 tokens each, and metadata-only telemetry.
- `tools/shared-service/hook.py session-start`: emitted the concise notes rule.
- `git diff --check`: passed.

Python checks used the existing
`C:/Users/davro/.local/share/csr-shared-service/.venv/Scripts/python.exe`.
Final candidate: `csr-engine/target-gpu/release/csr-shared-worker.exe`, SHA256
`39833BB9773B55737671DE311C450C00E99EE50A192CEEB58A8BBC94C0A1F7E9`.

The first real-worker run passed its functional assertions but failed temporary
cleanup because the test's SQLite verification connection remained open. The
test now closes that connection explicitly; subsequent complete runs passed.
The first FTS compatibility run was blocked by a Windows executable lock while
the candidate was running. It passed after the candidate stopped. Builds and
real-worker execution must be sequential on Windows. The final rebuilt binary
was checked again by both new integration scripts.

Cleanup of the first synthetic fixture was rejected by automatic approval review
with `blocked by policy`; it remains at
`C:/Users/davro/AppData/Local/Temp/csr-project-notes-ye_9y67h`. No alternate deletion
method was attempted.

Not run: the entire Rust suite, Clippy, the old deployment-bound `test_service.py`,
production migration/activation, or autonomous note-maintenance journeys in real
Claude/Codex/T3 sessions. Tool behavior and policy delivery do not establish that
an agent will interpret every approval correctly or always consult its notes.

## Review corrections on 2026-11-01

Opus 5.5 reviewed the first candidate read-only and found defects around project
identity, malformed arguments, policy delivery and updates from previews. The
corrections add canonical origin owner/repo keys, project/topic listing and status
filters, replacement links, full-read receipts, separate reader/writer tools,
self-contained gateway schemas, safe invalid-params handling, one policy file,
private-marker refusal, create-cap enforcement and punctuation-aware FTS terms.
The shared worker can export `--notes-policy` without opening data or models.

The activation helper explicitly plans SessionStart hook registration for both
clients while retaining their existing trust decisions. It preserves credentials,
protects backups, verifies a recovered database copy, pauses known tasks and waits
for verified service processes to exit before replacing binaries. The old
`prepare_install.py` must not be used for this upgrade. The README documents
sensitive-note recovery requiring explicit scope and retention authority.

Updated checks actually run:

- `cargo test --release --lib project_notes -j 1`: 9 passed.
- Built unit-test executable with `storage:: --test-threads=1`: 45 passed.
- `cargo build --release --bin csr-shared-worker -j 1`: passed.
- `test_protocol.py`: passed, including JSON-RPC invalid-params without stopping
  the worker.
- `test_project_notes.py --worker <candidate>`: passed the corrected lifecycle,
  legacy-reflection and integrity checks.
- `test_project_notes_gateway.py --worker <candidate>`: passed, including inline
  public schemas and four lossless bounded pages.
- `test_project_notes_http.py --worker <candidate>`: passed a real loopback HTTP
  cycle through the real MCP gateway, real worker and isolated SQLite: create,
  paraphrase search, refusal of preview-only updates, full read/update, history,
  malformed inputs without worker restarts, listing, large-read paging, integrity
  and metadata-only logs. Fixture cleanup waits for child exit on Windows.
- `activate_project_notes.py --worker <candidate>` (without `--apply`): preflight
  passed. This preflight did not modify the live service or client configuration.
- Python syntax checks and `git diff --check`: passed.

Current reviewed candidate SHA256:
`AAAFC8C3DDA13E5B331506333D6C4CF08522C8430E5EB6E17CE990CFA3152FD0`.

`cargo test --release --test integration -j 1 -- --test-threads=1` could not run
the integration suite: rustc terminated while building the main `csr-engine` CLI
with `STATUS_ILLEGAL_INSTRUCTION`, then `STATUS_ACCESS_VIOLATION` on one retry.
There was no Rust source diagnostic; the root cause was not established in this
task. Do not count this suite as passed. The dedicated shared worker built and
passed its isolated runtime checks. Full Rust/Clippy and autonomous host-client
behavior remain unverified. At this point activation was still pending; the
following sections record subsequent operator corrections and live evidence.

## Activation-helper review corrections

The second Opus 5.5 review found that venv launchers differ from the actual
interpreter, rollback did not wait or isolate errors, and catalog discovery alone
could falsely report activation. These Python-only fixes retain the reviewed
Rust binary SHA above:

- Preflight verifies the task launcher, real interpreter from `pyvenv.cfg`, exact
  service script, launcher parent and worker ancestry. Both interpreter and
  launcher are included in process-exit waits.
- Base/home/operations/task names are injectable for isolated tests. Ten tests
  in `test_activate_project_notes.py` passed: success, interpreter identity,
  required/mismatched/staged hash, failed live-worker postflight, partial install,
  independent restore retries/failures, task-enable failure isolation and
  preservation of concurrent asset changes.
- `--apply` requires the reviewed SHA and checks the staged and installed bytes.
  All original assets are checked before installation. Recovery touches only
  attempted owned writes, preserves unknown concurrent bytes, waits for process
  exit, retries restores independently and always attempts restart. Failed
  recovery is explicit; import/rebuild remain paused if recovery is incomplete.
- Success requires real note-project and legacy-search calls on the live DB,
  a changed worker PID and preserved configuration, followed by task resumption.
- The public gateway corrects the candidate's old project-example description to
  `wavenorth-systems/spectrix`. The native binary schema example remains a known
  documentation-only follow-up for the next compilation. The read receipt proves
  a full-read request, not consumption of every page; the README states that all
  `csr_read_page` continuations must be consumed before an update.

The corrected preflight and the real loopback HTTP test passed before activation.

## Live activation on 2026-11-01

The user explicitly authorized activation. Ran the corrected helper with
`--apply --expected-sha256 AAAFC8C3DDA13E5B331506333D6C4CF08522C8430E5EB6E17CE990CFA3152FD0`.
It exited 0 and recorded `activated: true` only after real-worker postflight.
The protected backup and verified restored copy contain 254,265 chunks and 2,343
reflections. The live database was not replaced or restored from an older copy.

Evidence directory:
`C:/Users/davro/.local/share/csr-shared-service/backups/before-project-notes-20261101T233253Z-9b5a7b`.
Safe metadata receipts are `activation-receipt.json` and
`live-notes-verification.json`; other backup files may contain private data and
credentials and must not be printed or shared.

Verified after activation:

- Installed candidate SHA matches, policy matches its canonical file, 18 public
  tools are advertised, worker PID 56504 opened the live database, and a legacy
  conversation search succeeded. No rollback errors.
- `CSR-Shared-Service` is Running/Enabled; `CSR-Conversations-AutoImport` and
  `CSR-Index-Rebuild` are Ready/Enabled. No task was left paused.
- Existing Codex/Claude hook configuration is semantically unchanged apart from
  the appended SessionStart entry. Codex `config.toml` SHA256 remained
  `F66F9B247CCDF868BF397746FA9BC3428C57731608EA02F4EDAFB46E408C10A7`;
  existing trust state was not rewritten. This does not prove a new hook has run.
- The parent recorded the actual user-approved workflow in project
  `davrodwconnections/claude-self-reflect`, topic
  `decision/curated-project-memory`, revision 1, status `approved`, with the
  conversation as source. Exact readback and a Spanish paraphrase search passed.
  This was a real note through the live MCP HTTP endpoint, not synthetic data.
- An existing CSR tool was callable through the current T3 provider connection
  after restart. The new tool functions are not mapped into this existing
  conversation's tool context (`is not a function` when tried once).

Remaining host check: a fresh conversation must load the new reader/writer tools
and demonstrate the SessionStart rule (and any normal trust prompt). Do not claim
automatic agent compliance or successful native new-tool use in this old context.

## Delivery boundary

The backend is activated and live-verified. Native tool loading/hook execution in
a new host conversation remains to be checked; reconnect clients normally and
do not terminate existing conversations. No commit or push was made.
