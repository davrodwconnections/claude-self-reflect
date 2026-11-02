//! Curated project knowledge. Transcript import never writes these tables.
use anyhow::{ensure, Result};
use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};
use schemars::JsonSchema;
use serde::{Deserialize, Serialize};

use super::Storage;

pub const POLICY: &str = include_str!("../../data/PROJECT_NOTES_POLICY.txt");
pub const MAX_NOTES: i64 = 10_000;

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum NoteKind {
    Decision,
    System,
    Pending,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum NoteStatus {
    Proposed,
    Approved,
    Implemented,
    Pending,
    Resolved,
    NeedsReview,
    Superseded,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema, PartialEq, Eq)]
#[serde(deny_unknown_fields)]
pub struct NoteDraft {
    pub title: String,
    pub kind: NoteKind,
    pub status: NoteStatus,
    pub content: String,
    /// Conversation/message, document section or file+commit, with relevant evidence.
    pub source: String,
    /// Why this revision is being recorded; never infer approval from a proposal.
    pub change_reason: String,
    /// Required when superseded: topic key of the replacement in this project.
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub superseded_by: Option<String>,
}

#[derive(Clone, Debug, Serialize, Deserialize, JsonSchema)]
pub struct ProjectNote {
    pub id: String,
    pub project: String,
    pub topic_key: String,
    pub revision: i64,
    pub updated_at: String,
    pub note: NoteDraft,
}

pub fn migrate(conn: &Connection) -> Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS note_projects (
        project TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS project_notes (
        id TEXT PRIMARY KEY, project TEXT NOT NULL, topic_key TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision > 0), data TEXT NOT NULL,
        embedding BLOB NOT NULL, UNIQUE(project, topic_key));
        CREATE TABLE IF NOT EXISTS project_note_revisions (
        note_id TEXT NOT NULL REFERENCES project_notes(id), revision INTEGER NOT NULL,
        data TEXT NOT NULL, PRIMARY KEY(note_id, revision));
        CREATE TABLE IF NOT EXISTS project_note_lookups (
        token TEXT PRIMARY KEY, project TEXT NOT NULL, generation INTEGER NOT NULL,
        expires_at INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS project_note_reads (
        token TEXT NOT NULL REFERENCES project_note_lookups(token) ON DELETE CASCADE,
        topic_key TEXT NOT NULL, revision INTEGER NOT NULL,
        PRIMARY KEY(token, topic_key));
        CREATE VIRTUAL TABLE IF NOT EXISTS project_notes_fts USING fts5(
        note_id UNINDEXED, title, content, topic_key);",
    )?;
    Ok(())
}

pub fn project_key(project: &str) -> Result<String> {
    let key = project.trim().to_lowercase();
    let parts: Vec<_> = key.split('/').collect();
    ensure!(
        key.len() <= 250 && parts.len() == 2 && parts.iter().all(|part|
            !part.is_empty() && ![".", ".."].contains(part) && part.chars().all(|c|
                c.is_ascii_lowercase() || c.is_ascii_digit() || "-_.".contains(c))),
        "Use canonical lowercase owner/repo from the repository remote (or agreed local/name); paths and bare names are rejected"
    );
    Ok(key)
}

pub fn validate_topic(topic: &str) -> Result<()> {
    ensure!(
        !topic.is_empty()
            && topic.len() <= 160
            && topic
                .chars()
                .all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || "-_/".contains(c)),
        "topic_key must be 1-160 lowercase ASCII letters, digits, -, _ or /"
    );
    Ok(())
}

pub fn validate_draft(draft: &NoteDraft) -> Result<()> {
    for (name, value, max) in [
        ("title", &draft.title, 200),
        ("content", &draft.content, 8000),
        ("source", &draft.source, 2000),
        ("change_reason", &draft.change_reason, 2000),
    ] {
        ensure!(
            !value.trim().is_empty() && value.chars().count() <= max,
            "{name} must be nonblank and at most {max} characters"
        );
        ensure!(
            !value.to_lowercase().contains("<private"),
            "Private-marked text cannot be stored in project notes"
        );
    }
    if let Some(topic) = &draft.superseded_by {
        validate_topic(topic)?;
    }
    ensure!(
        (draft.status == NoteStatus::Superseded) == draft.superseded_by.is_some(),
        "superseded notes require superseded_by; other statuses must omit it"
    );
    Ok(())
}

pub struct NoteCandidates {
    pub notes: Vec<(ProjectNote, Vec<f32>)>,
    pub lexical_ids: Vec<String>,
    pub lookup_token: String,
}

impl Storage {
    /// One snapshot of current notes, lexical matches and a short-lived write guard.
    pub fn note_candidates(&self, project: &str, fts_query: &str) -> Result<NoteCandidates> {
        let project = project_key(project)?;
        let mut conn = self
            .conn
            .lock()
            .map_err(|_| anyhow::anyhow!("Storage unavailable"))?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let count: i64 = tx.query_row(
            "SELECT count(*) FROM project_notes WHERE project=?1",
            [&project],
            |r| r.get(0),
        )?;
        ensure!(
            count <= MAX_NOTES,
            "Project exceeds note scan budget; use list/read and reconcile existing notes"
        );
        let mut stmt = tx.prepare(
            "SELECT data, embedding FROM project_notes WHERE project=?1 ORDER BY topic_key",
        )?;
        let rows = stmt.query_map([&project], |r| {
            Ok((r.get::<_, String>(0)?, r.get::<_, Vec<u8>>(1)?))
        })?;
        let mut notes = Vec::new();
        for row in rows {
            let (data, bytes) = row?;
            ensure!(bytes.len() % 4 == 0, "Invalid note vector");
            let vector = bytes
                .chunks_exact(4)
                .map(|c| f32::from_le_bytes(c.try_into().unwrap()))
                .collect();
            notes.push((serde_json::from_str(&data)?, vector));
        }
        drop(stmt);
        let mut lexical_ids = Vec::new();
        if !fts_query.is_empty() {
            let mut stmt = tx.prepare(
                "SELECT n.id FROM project_notes_fts f JOIN project_notes n ON n.id=f.note_id
                WHERE project_notes_fts MATCH ?1 AND n.project=?2 ORDER BY rank LIMIT 30",
            )?;
            lexical_ids = stmt
                .query_map(params![fts_query, project], |r| r.get(0))?
                .collect::<rusqlite::Result<_>>()?;
        }
        tx.execute(
            "INSERT OR IGNORE INTO note_projects(project) VALUES(?1)",
            [&project],
        )?;
        let generation: i64 = tx.query_row(
            "SELECT generation FROM note_projects WHERE project=?1",
            [&project],
            |r| r.get(0),
        )?;
        let lookup_token = uuid::Uuid::new_v4().to_string();
        tx.execute(
            "DELETE FROM project_note_lookups WHERE expires_at <= unixepoch()",
            [],
        )?;
        // A bounded receipt pool, not an unbounded copy of search queries.
        tx.execute(
            "DELETE FROM project_note_lookups WHERE token IN
            (SELECT token FROM project_note_lookups ORDER BY expires_at DESC LIMIT -1 OFFSET 999)",
            [],
        )?;
        tx.execute(
            "INSERT INTO project_note_lookups VALUES(?1,?2,?3,unixepoch()+900)",
            params![lookup_token, project, generation],
        )?;
        tx.commit()?;
        Ok(NoteCandidates {
            notes,
            lexical_ids,
            lookup_token,
        })
    }

    pub fn read_note(&self, project: &str, topic: &str) -> Result<Option<ProjectNote>> {
        let project = project_key(project)?;
        validate_topic(topic)?;
        let conn = self
            .conn
            .lock()
            .map_err(|_| anyhow::anyhow!("Storage unavailable"))?;
        let data: Option<String> = conn
            .query_row(
                "SELECT data FROM project_notes WHERE project=?1 AND topic_key=?2",
                params![project, topic],
                |r| r.get(0),
            )
            .optional()?;
        data.map(|s| serde_json::from_str(&s).map_err(Into::into))
            .transpose()
    }

    /// Receipt proves that the full current revision was returned for this lookup.
    pub fn read_note_for_update(
        &self,
        project: &str,
        topic: &str,
        token: &str,
    ) -> Result<Option<ProjectNote>> {
        let project = project_key(project)?;
        validate_topic(topic)?;
        let mut conn = self
            .conn
            .lock()
            .map_err(|_| anyhow::anyhow!("Storage unavailable"))?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let valid: bool = tx.query_row(
            "SELECT EXISTS(SELECT 1 FROM project_note_lookups l
            JOIN note_projects p ON p.project=l.project AND p.generation=l.generation
            WHERE l.token=?1 AND l.project=?2 AND l.expires_at>unixepoch())",
            params![token, project],
            |r| r.get(0),
        )?;
        ensure!(
            valid,
            "Search again before reading for update; lookup missing, stale or expired"
        );
        let data: Option<String> = tx
            .query_row(
                "SELECT data FROM project_notes WHERE project=?1 AND topic_key=?2",
                params![project, topic],
                |r| r.get(0),
            )
            .optional()?;
        let note: Option<ProjectNote> = data.map(|s| serde_json::from_str(&s)).transpose()?;
        if let Some(n) = &note {
            tx.execute(
                "INSERT INTO project_note_reads VALUES(?1,?2,?3)
                ON CONFLICT(token,topic_key) DO UPDATE SET revision=excluded.revision",
                params![token, topic, n.revision],
            )?;
        }
        tx.commit()?;
        Ok(note)
    }

    pub fn list_note_projects(&self, after: &str, limit: usize) -> Result<Vec<(String, i64)>> {
        let conn = self
            .conn
            .lock()
            .map_err(|_| anyhow::anyhow!("Storage unavailable"))?;
        let mut stmt=conn.prepare("SELECT project,count(*) FROM project_notes WHERE project>?1 GROUP BY project ORDER BY project LIMIT ?2")?;
        let result = stmt
            .query_map(params![after, limit.clamp(1, 11) as i64], |r| {
                Ok((r.get(0)?, r.get(1)?))
            })?
            .collect::<rusqlite::Result<_>>()?;
        Ok(result)
    }

    pub fn list_notes(
        &self,
        project: &str,
        after: &str,
        kind: Option<&str>,
        status: Option<&str>,
        limit: usize,
    ) -> Result<Vec<ProjectNote>> {
        let project = project_key(project)?;
        let conn = self
            .conn
            .lock()
            .map_err(|_| anyhow::anyhow!("Storage unavailable"))?;
        let mut stmt = conn.prepare(
            "SELECT data FROM project_notes WHERE project=?1 AND topic_key>?2
            AND (?3 IS NULL OR json_extract(data,'$.note.kind')=?3)
            AND (?4 IS NULL OR json_extract(data,'$.note.status')=?4) ORDER BY topic_key LIMIT ?5",
        )?;
        let rows = stmt.query_map(
            params![project, after, kind, status, limit.clamp(1, 11) as i64],
            |r| r.get::<_, String>(0),
        )?;
        rows.map(|r| Ok(serde_json::from_str(&r?)?)).collect()
    }

    pub fn note_history(
        &self,
        project: &str,
        topic: &str,
        before: i64,
        limit: usize,
    ) -> Result<Vec<ProjectNote>> {
        let project = project_key(project)?;
        validate_topic(topic)?;
        ensure!(before > 0, "before_revision must be positive");
        let conn = self
            .conn
            .lock()
            .map_err(|_| anyhow::anyhow!("Storage unavailable"))?;
        let mut stmt = conn.prepare("SELECT v.data FROM project_note_revisions v JOIN project_notes n ON n.id=v.note_id
            WHERE n.project=?1 AND n.topic_key=?2 AND v.revision<?3 ORDER BY v.revision DESC LIMIT ?4")?;
        let rows = stmt.query_map(
            params![project, topic, before, limit.clamp(1, 11) as i64],
            |r| r.get::<_, String>(0),
        )?;
        rows.map(|row| Ok(serde_json::from_str(&row?)?)).collect()
    }

    /// Compare-and-swap plus a project-generation guard protects concurrent writers.
    pub fn save_note(
        &self,
        project: &str,
        topic: &str,
        expected: i64,
        lookup_token: &str,
        draft: NoteDraft,
        embedding: &[f32],
    ) -> Result<ProjectNote> {
        let project = project_key(project)?;
        validate_topic(topic)?;
        validate_draft(&draft)?;
        ensure!(
            expected >= 0,
            "expected_revision must be nonnegative; use 0 to create"
        );
        ensure!(
            embedding.len() == 384
                && embedding.iter().all(|v| v.is_finite())
                && embedding.iter().any(|v| *v != 0.0),
            "Invalid note embedding"
        );
        let mut conn = self
            .conn
            .lock()
            .map_err(|_| anyhow::anyhow!("Storage unavailable"))?;
        let tx = conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let receipt: Option<i64> = tx
            .query_row(
                "SELECT generation FROM project_note_lookups
            WHERE token=?1 AND project=?2 AND expires_at>unixepoch()",
                params![lookup_token, project],
                |r| r.get(0),
            )
            .optional()?;
        let receipt = receipt.ok_or_else(|| {
            anyhow::anyhow!(
                "Search this project's notes successfully before saving; lookup expired or missing"
            )
        })?;
        let current: Option<String> = tx
            .query_row(
                "SELECT data FROM project_notes WHERE project=?1 AND topic_key=?2",
                params![project, topic],
                |r| r.get(0),
            )
            .optional()?;
        let current: Option<ProjectNote> = current.map(|s| serde_json::from_str(&s)).transpose()?;
        if let Some(ref note) = current {
            // A lost response can be retried without another revision or history entry.
            if note.note == draft && (expected == note.revision || expected == note.revision - 1) {
                return Ok(note.clone());
            }
        }
        let generation: i64 = tx.query_row(
            "SELECT generation FROM note_projects WHERE project=?1",
            [&project],
            |r| r.get(0),
        )?;
        ensure!(
            receipt == generation,
            "Project notes changed since search; search and reconcile again"
        );
        ensure!(
            current.as_ref().map_or(0, |n| n.revision) == expected,
            "Revision conflict; read the latest note and reconcile before retrying"
        );
        if expected > 0 {
            let read: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM project_note_reads
                WHERE token=?1 AND topic_key=?2 AND revision=?3)",
                params![lookup_token, topic, expected],
                |r| r.get(0),
            )?;
            ensure!(
                read,
                "Read the full current note with this lookup_token before updating"
            );
        } else {
            let count: i64 = tx.query_row(
                "SELECT count(*) FROM project_notes WHERE project=?1",
                [&project],
                |r| r.get(0),
            )?;
            ensure!(
                count < MAX_NOTES,
                "Project note limit reached; update an existing topic instead of creating another"
            );
        }
        if let Some(replacement) = &draft.superseded_by {
            ensure!(replacement != topic, "A note cannot supersede itself");
            let exists: bool = tx.query_row(
                "SELECT EXISTS(SELECT 1 FROM project_notes WHERE project=?1
                AND topic_key=?2 AND json_extract(data,'$.note.status')!='superseded')",
                params![project, replacement],
                |r| r.get(0),
            )?;
            ensure!(
                exists,
                "Replacement must be an existing, non-superseded topic in this project"
            );
        }
        let note = ProjectNote {
            id: current.map_or_else(|| uuid::Uuid::new_v4().to_string(), |n| n.id),
            project: project.clone(),
            topic_key: topic.into(),
            revision: expected + 1,
            updated_at: chrono::Utc::now().to_rfc3339(),
            note: draft,
        };
        let data = serde_json::to_string(&note)?;
        let bytes: Vec<u8> = embedding.iter().flat_map(|f| f.to_le_bytes()).collect();
        tx.execute("INSERT INTO project_notes(id,project,topic_key,revision,data,embedding) VALUES(?1,?2,?3,?4,?5,?6)
            ON CONFLICT(project,topic_key) DO UPDATE SET revision=excluded.revision,data=excluded.data,embedding=excluded.embedding",
            params![note.id, project, topic, note.revision, data, bytes])?;
        tx.execute(
            "INSERT INTO project_note_revisions VALUES(?1,?2,?3)",
            params![note.id, note.revision, data],
        )?;
        tx.execute("DELETE FROM project_notes_fts WHERE note_id=?1", [&note.id])?;
        tx.execute(
            "INSERT INTO project_notes_fts(note_id,title,content,topic_key) VALUES(?1,?2,?3,?4)",
            params![note.id, note.note.title, note.note.content, topic],
        )?;
        tx.execute(
            "UPDATE note_projects SET generation=generation+1 WHERE project=?1",
            [&project],
        )?;
        let readback: String = tx.query_row(
            "SELECT data FROM project_notes WHERE id=?1",
            [&note.id],
            |r| r.get(0),
        )?;
        ensure!(readback == data, "Note readback mismatch");
        tx.commit()?;
        Ok(serde_json::from_str(&readback)?)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn draft(text: &str) -> NoteDraft {
        NoteDraft {
            title: "Schedule permissions".into(),
            kind: NoteKind::Decision,
            status: NoteStatus::Approved,
            content: text.into(),
            source: "conversation:synthetic-1/message:2; user approved this rule".into(),
            change_reason: "Explicit synthetic approval".into(),
            superseded_by: None,
        }
    }
    fn vector() -> Vec<f32> {
        vec![1.0; 384]
    }
    fn search(s: &Storage, project: &str) -> String {
        s.note_candidates(project, "").unwrap().lookup_token
    }
    fn save(s: &Storage, project: &str, topic: &str, rev: i64, text: &str) -> ProjectNote {
        let token = search(s, project);
        if rev > 0 {
            s.read_note_for_update(project, topic, &token).unwrap();
        }
        s.save_note(project, topic, rev, &token, draft(text), &vector())
            .unwrap()
    }

    #[test]
    fn project_notes_search_required_and_scoped() {
        let s = Storage::open_memory().unwrap();
        assert!(s
            .save_note(
                "test/alpha",
                "permissions",
                0,
                "missing",
                draft("only supervisors"),
                &vector()
            )
            .is_err());
        let token = search(&s, "test/alpha");
        assert!(s
            .save_note(
                "test/beta",
                "permissions",
                0,
                &token,
                draft("only supervisors"),
                &vector()
            )
            .is_err());
        s.save_note(
            "test/alpha",
            "permissions",
            0,
            &token,
            draft("only supervisors"),
            &vector(),
        )
        .unwrap();
        assert!(s.read_note("test/beta", "permissions").unwrap().is_none());
        assert!(s
            .note_candidates("test/beta", "\"supervisors\"")
            .unwrap()
            .notes
            .is_empty());
        assert!(s
            .note_candidates("test/beta", "\"supervisors\"")
            .unwrap()
            .lexical_ids
            .is_empty());
        assert!(s.note_candidates("all", "").is_err());
        assert!(s.note_candidates("", "").is_err());
    }

    #[test]
    fn project_notes_revision_history_and_retry() {
        let s = Storage::open_memory().unwrap();
        let token = search(&s, "test/alpha");
        let first = s
            .save_note(
                "test/alpha",
                "permissions",
                0,
                &token,
                draft("only supervisors"),
                &vector(),
            )
            .unwrap();
        let retry = s
            .save_note(
                "test/alpha",
                "permissions",
                0,
                &token,
                draft("only supervisors"),
                &vector(),
            )
            .unwrap();
        assert_eq!(retry.revision, 1);
        assert_eq!(retry.updated_at, first.updated_at);
        let second = save(
            &s,
            "test/alpha",
            "permissions",
            1,
            "supervisors and administrators",
        );
        assert_eq!(second.id, first.id);
        assert_eq!(second.revision, 2);
        let history = s
            .note_history("test/alpha", "permissions", i64::MAX, 3)
            .unwrap();
        assert_eq!(history.len(), 2);
        assert_eq!(history[0].note.content, "supervisors and administrators");
        assert_eq!(history[1].note.content, "only supervisors");
        assert_eq!(history[1].note.source, first.note.source);
        assert_eq!(
            s.note_history("test/alpha", "permissions", 2, 3)
                .unwrap()
                .len(),
            1
        );
        let candidates = s
            .note_candidates("test/alpha", "\"administrators\"")
            .unwrap();
        assert_eq!(candidates.notes.len(), 1);
        assert_eq!(candidates.notes[0].0.revision, 2);
        assert_eq!(candidates.lexical_ids, vec![first.id]);
        assert!(s
            .note_candidates("test/alpha", "\"only\"")
            .unwrap()
            .lexical_ids
            .is_empty());
    }

    #[test]
    fn project_notes_concurrent_writers_reconcile_instead_of_overwriting() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("notes.db");
        let a = Storage::open(&path).unwrap();
        let b = Storage::open(&path).unwrap();
        let ta = search(&a, "test/alpha");
        let tb = search(&b, "test/alpha");
        a.save_note(
            "test/alpha",
            "permissions",
            0,
            &ta,
            draft("first writer"),
            &vector(),
        )
        .unwrap();
        assert!(b
            .save_note(
                "test/alpha",
                "permissions",
                0,
                &tb,
                draft("second writer"),
                &vector()
            )
            .is_err());
        // Even a new key must reconcile changes made since the previous search.
        assert!(b
            .save_note(
                "test/alpha",
                "other-topic",
                0,
                &tb,
                draft("duplicate wording"),
                &vector()
            )
            .is_err());
        let fresh = search(&b, "test/alpha");
        assert!(b
            .save_note(
                "test/alpha",
                "permissions",
                0,
                &fresh,
                draft("wrong revision"),
                &vector()
            )
            .is_err());
        b.read_note_for_update("test/alpha", "permissions", &fresh)
            .unwrap();
        b.save_note(
            "test/alpha",
            "permissions",
            1,
            &fresh,
            draft("reconciled"),
            &vector(),
        )
        .unwrap();
        assert_eq!(
            a.read_note("test/alpha", "permissions")
                .unwrap()
                .unwrap()
                .revision,
            2
        );
    }

    #[test]
    fn project_notes_failed_save_rolls_back_note_history_and_index() {
        let s = Storage::open_memory().unwrap();
        save(&s, "test/alpha", "permissions", 0, "original rule");
        s.conn
            .lock()
            .unwrap()
            .execute_batch(
                "CREATE TRIGGER fail_note_history BEFORE INSERT ON project_note_revisions
            BEGIN SELECT RAISE(ABORT, 'synthetic write failure'); END;",
            )
            .unwrap();
        let token = search(&s, "test/alpha");
        s.read_note_for_update("test/alpha", "permissions", &token)
            .unwrap();
        let error = s
            .save_note(
                "test/alpha",
                "permissions",
                1,
                &token,
                draft("replacement"),
                &vector(),
            )
            .unwrap_err();
        assert!(error.to_string().contains("synthetic write failure"));
        assert_eq!(
            s.read_note("test/alpha", "permissions")
                .unwrap()
                .unwrap()
                .note
                .content,
            "original rule"
        );
        assert_eq!(
            s.note_history("test/alpha", "permissions", i64::MAX, 10)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            s.note_candidates("test/alpha", "\"original\"")
                .unwrap()
                .lexical_ids
                .len(),
            1
        );
        assert!(s
            .note_candidates("test/alpha", "\"replacement\"")
            .unwrap()
            .lexical_ids
            .is_empty());
    }

    #[test]
    fn project_notes_expiry_validation_and_migration() {
        let s = Storage::open_memory().unwrap();
        {
            let c = s.conn.lock().unwrap();
            migrate(&c).unwrap();
            migrate(&c).unwrap();
        }
        let token = search(&s, "test/alpha");
        s.conn
            .lock()
            .unwrap()
            .execute("UPDATE project_note_lookups SET expires_at=0", [])
            .unwrap();
        assert!(s
            .save_note(
                "test/alpha",
                "permissions",
                0,
                &token,
                draft("rule"),
                &vector()
            )
            .is_err());
        let mut invalid = draft("rule");
        invalid.source = " ".into();
        assert!(s
            .save_note(
                "test/alpha",
                "permissions",
                0,
                &search(&s, "test/alpha"),
                invalid,
                &vector()
            )
            .is_err());
        assert!(s
            .save_note(
                "test/alpha",
                "../UPPER",
                0,
                &search(&s, "test/alpha"),
                draft("rule"),
                &vector()
            )
            .is_err());
        assert!(s
            .save_note(
                "test/alpha",
                "permissions",
                0,
                &search(&s, "test/alpha"),
                draft("rule"),
                &[0.0; 384]
            )
            .is_err());
        assert_eq!(project_key(" Test/Alpha ").unwrap(), "test/alpha");
        assert!(project_key("C:\\Repo\\").is_err());
        assert!(project_key("spectrix").is_err());
    }

    #[test]
    fn project_notes_require_full_read_and_keep_listed_replacements() {
        let s = Storage::open_memory().unwrap();
        save(&s, "test/alpha", "old", 0, "original rule with details");
        let token = search(&s, "test/alpha");
        assert!(s
            .save_note(
                "test/alpha",
                "old",
                1,
                &token,
                draft("incomplete replacement"),
                &vector()
            )
            .unwrap_err()
            .to_string()
            .contains("Read the full"));
        assert!(s.read_note_for_update("test/beta", "old", &token).is_err());
        save(&s, "test/alpha", "new", 0, "new rule");
        let token = search(&s, "test/alpha");
        s.read_note_for_update("test/alpha", "old", &token).unwrap();
        let mut old = draft("original rule with details");
        old.status = NoteStatus::Superseded;
        old.superseded_by = Some("new".into());
        s.save_note("test/alpha", "old", 1, &token, old, &vector())
            .unwrap();
        let token = search(&s, "test/alpha");
        let mut pending = draft("unresolved product question");
        pending.kind = NoteKind::Pending;
        pending.status = NoteStatus::Pending;
        s.save_note("test/alpha", "pending", 0, &token, pending, &vector())
            .unwrap();
        let listed = s
            .list_notes("test/alpha", "", None, Some("superseded"), 10)
            .unwrap();
        assert_eq!(listed.len(), 1);
        assert_eq!(listed[0].note.superseded_by.as_deref(), Some("new"));
        assert_eq!(
            s.list_notes("test/alpha", "", Some("pending"), Some("pending"), 10)
                .unwrap()
                .len(),
            1
        );
        assert_eq!(
            s.list_notes("test/alpha", "old", None, None, 10).unwrap()[0].topic_key,
            "pending"
        );
        assert_eq!(
            s.list_note_projects("", 10).unwrap(),
            vec![("test/alpha".into(), 3)]
        );
    }

    #[test]
    fn project_notes_private_markers_and_create_cap() {
        let s = Storage::open_memory().unwrap();
        let token = search(&s, "test/alpha");
        let mut private = draft("safe");
        private.source = "<private>synthetic-sensitive-marker</private>".into();
        assert!(s
            .save_note("test/alpha", "secret", 0, &token, private, &vector())
            .is_err());
        assert!(s.read_note("test/alpha", "secret").unwrap().is_none());
        {
            let mut conn = s.conn.lock().unwrap();
            let tx = conn.transaction().unwrap();
            for i in 0..MAX_NOTES {
                tx.execute(
                    "INSERT INTO project_notes VALUES(?1,'test/alpha',?1,1,'{}',X'00')",
                    [format!("fixture-{i}")],
                )
                .unwrap();
            }
            tx.commit().unwrap();
        }
        assert!(s
            .save_note(
                "test/alpha",
                "beyond-limit",
                0,
                &token,
                draft("excess"),
                &vector()
            )
            .unwrap_err()
            .to_string()
            .contains("limit reached"));
        assert_eq!(
            crate::storage::queries::fts5_expression("decision/schedule-permissions src/api.rs"),
            "\"decision\" OR \"schedule-permissions\" OR \"src\" OR \"api\" OR \"rs\""
        );
    }
}
