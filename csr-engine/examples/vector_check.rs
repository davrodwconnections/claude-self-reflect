//! Re-embed stored chunks and compare with their stored vectors, to prove this machine's
//! embeddings match the index. Read-only against the database.
//!
//!   cargo run --release --example vector_check -- <db> [n]

use anyhow::Result;
use csr_engine::embeddings::EmbeddingEngine;
use rusqlite::{Connection, OpenFlags};

fn main() -> Result<()> {
    let db = std::env::args().nth(1).expect("db path");
    let n: i64 = std::env::args().nth(2).and_then(|v| v.parse().ok()).unwrap_or(300);
    let conn = Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let columns: Vec<String> = conn
        .prepare("SELECT name FROM pragma_table_info('chunk_embeddings')")?
        .query_map([], |r| r.get(0))?
        .filter_map(|r| r.ok())
        .collect();
    println!("chunk_embeddings columns: {columns:?}");
    let sql = format!(
        "SELECT c.content, e.{} FROM chunks c JOIN chunk_embeddings e ON e.{} = c.id \
         WHERE length(c.content) > 200 ORDER BY random() LIMIT ?1",
        columns[1], columns[0]
    );
    let rows: Vec<(String, Vec<u8>)> = conn
        .prepare(&sql)?
        .query_map([n], |r| Ok((r.get(0)?, r.get(1)?)))?
        .filter_map(|r| r.ok())
        .collect();
    let engine = EmbeddingEngine::new()?;
    let mut scores = Vec::with_capacity(rows.len());
    for (content, stored) in &rows {
        let stored: Vec<f32> = stored
            .chunks_exact(4)
            .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
            .collect();
        let fresh = engine.embed_single(content)?;
        let dot: f32 = fresh.iter().zip(&stored).map(|(a, b)| a * b).sum();
        let norm = fresh.iter().map(|x| x * x).sum::<f32>().sqrt()
            * stored.iter().map(|x| x * x).sum::<f32>().sqrt();
        scores.push(dot / norm);
    }
    scores.sort_by(|a, b| a.total_cmp(b));
    println!(
        "{} chunks: min {:.6} p01 {:.6} median {:.6}",
        scores.len(),
        scores[0],
        scores[scores.len() / 100],
        scores[scores.len() / 2]
    );
    Ok(())
}
