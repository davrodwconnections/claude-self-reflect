//! Compare HNSW top-k against brute-force exact cosine on a DB copy, so retrieval
//! misses can be attributed to the model or to ANN approximation.
//!
//!   cargo run --release --example exact_vs_hnsw -- <db> <index_dir> <queries.json>
//!
//! `queries.json` is a list of {"conv": <conversation_id>, "verbatim": <text>,
//! "fuzzy": <text>} objects. Read-only against the database.

use anyhow::{Context, Result};
use csr_engine::embeddings::EmbeddingEngine;
use csr_engine::search::SearchEngine;
use rusqlite::{Connection, OpenFlags};
use std::collections::{HashMap, HashSet};
use std::path::PathBuf;

const K: usize = 10;

fn bytes_to_vec(bytes: &[u8]) -> Vec<f32> {
    bytes
        .chunks_exact(4)
        .map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]]))
        .collect()
}

fn cosine(a: &[f32], b: &[f32]) -> f32 {
    let dot: f32 = a.iter().zip(b).map(|(x, y)| x * y).sum();
    let na = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let nb = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    if na * nb == 0.0 {
        0.0
    } else {
        dot / (na * nb)
    }
}

/// First K distinct conversations in ranked chunk order.
fn top_convs(ranked: &[String], conv_of: &HashMap<String, String>) -> Vec<String> {
    let mut seen = Vec::new();
    for id in ranked {
        if let Some(c) = conv_of.get(id) {
            if !seen.contains(c) {
                seen.push(c.clone());
            }
        }
        if seen.len() == K {
            break;
        }
    }
    seen
}

fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    anyhow::ensure!(args.len() == 4, "usage: exact_vs_hnsw <db> <index_dir> <queries.json>");
    let db = PathBuf::from(&args[1]);
    let index_dir = PathBuf::from(&args[2]);
    let queries: Vec<serde_json::Value> =
        serde_json::from_str(&std::fs::read_to_string(&args[3])?)?;

    let conn = Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let conv_of: HashMap<String, String> = conn
        .prepare("SELECT id, conversation_id FROM chunks")?
        .query_map([], |r| Ok((r.get(0)?, r.get(1)?)))?
        .filter_map(|r| r.ok())
        .collect();
    let vecs: Vec<(String, Vec<f32>)> = conn
        .prepare("SELECT chunk_id, embedding FROM chunk_embeddings")?
        .query_map([], |r| {
            let id: String = r.get(0)?;
            let bytes: Vec<u8> = r.get(1)?;
            Ok((id, bytes_to_vec(&bytes)))
        })?
        .filter_map(|r| r.ok())
        .collect();
    let refl: i64 = conn.query_row("SELECT count(*) FROM reflection_embeddings", [], |r| r.get(0))?;
    let refl = refl as usize;
    eprintln!("{} chunk vectors, {} conversations", vecs.len(), conv_of.len());

    let index = SearchEngine::load_from_disk(&index_dir, vecs.len(), refl)
        .context("index cache did not load (counts differ?)")?;
    let engine = EmbeddingEngine::new()?;

    for kind in ["verbatim", "fuzzy"] {
        let (mut exact_hits, mut hnsw_hits, mut overlap, mut top1_gap) = (0, 0, 0usize, 0f32);
        for q in &queries {
            let text = q[kind].as_str().unwrap_or_default();
            let want = q["conv"].as_str().unwrap_or_default().to_string();
            let qv = engine.embed_single(text)?;

            let mut scored: Vec<(f32, &String)> =
                vecs.iter().map(|(id, v)| (cosine(&qv, v), id)).collect();
            scored.sort_by(|a, b| b.0.total_cmp(&a.0));
            let exact_ids: Vec<String> = scored.iter().take(200).map(|(_, id)| (*id).clone()).collect();

            let hnsw = index.search_chunks(&qv, 200, 0.0);
            let hnsw_ids: Vec<String> = hnsw.iter().map(|r| r.id.clone()).collect();

            let e_top: HashSet<&String> = exact_ids.iter().take(K).collect();
            overlap += hnsw_ids.iter().take(K).filter(|id| e_top.contains(id)).count();
            top1_gap += scored[0].0 - hnsw.first().map(|r| r.score).unwrap_or(0.0);

            if top_convs(&exact_ids, &conv_of).contains(&want) {
                exact_hits += 1;
            }
            if top_convs(&hnsw_ids, &conv_of).contains(&want) {
                hnsw_hits += 1;
            }
        }
        let n = queries.len();
        println!(
            "{kind:9} conv hit@{K}: exact {exact_hits}/{n}  hnsw {hnsw_hits}/{n}  | top-{K} chunk overlap {:.1}%  | mean top-1 score gap {:.3}",
            100.0 * overlap as f32 / (n * K) as f32,
            top1_gap / n as f32
        );
    }

    let qv = engine.embed_single("mod de Bannerlord para la IA de combate tactico")?;
    let best = vecs.iter().map(|(_, v)| cosine(&qv, v)).fold(f32::MIN, f32::max);
    let h = index.search_chunks(&qv, 1, 0.0);
    println!("bannerlord probe: exact top-1 {best:.3}  hnsw top-1 {:?}", h.first().map(|r| r.score));
    Ok(())
}
