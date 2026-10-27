//! Compare CPU and DirectML (GPU) for the embedding model and the reranker:
//! speed, and whether the GPU reproduces the CPU vectors the index was built with.
//!
//!   cargo run --release --example gpu_check -- <db>

use anyhow::Result;
use fastembed::{
    EmbeddingModel, ExecutionProviderDispatch, InitOptions, RerankInitOptions, RerankerModel,
    TextEmbedding, TextRerank,
};
use rusqlite::{Connection, OpenFlags};
use std::time::Instant;

fn gpu() -> Vec<ExecutionProviderDispatch> {
    vec![ort::ep::DirectML::default()
        .with_performance_preference(ort::ep::directml::PerformancePreference::HighPerformance)
        .build()
        .error_on_failure()]
}

fn cosine(a: &[f32], b: &[f32]) -> f32 {
    let dot: f32 = a.iter().zip(b).map(|(x, y)| x * y).sum();
    let na = a.iter().map(|x| x * x).sum::<f32>().sqrt();
    let nb = b.iter().map(|x| x * x).sum::<f32>().sqrt();
    dot / (na * nb)
}

fn main() -> Result<()> {
    let db = std::env::args().nth(1).expect("db path");
    let conn = Connection::open_with_flags(&db, OpenFlags::SQLITE_OPEN_READ_ONLY)?;
    let texts: Vec<String> = conn
        .prepare("SELECT content FROM chunks WHERE length(content) > 200 ORDER BY rowid DESC LIMIT 300")?
        .query_map([], |r| r.get(0))?
        .filter_map(|r| r.ok())
        .collect();
    let refs: Vec<&str> = texts.iter().map(String::as_str).collect();
    let cache = dirs::cache_dir().unwrap().join("csr-engine").join("fastembed");

    println!("== embedding (ParaphraseMLMiniLML12V2Q), {} chunks", texts.len());
    let base = || {
        InitOptions::new(EmbeddingModel::ParaphraseMLMiniLML12V2Q).with_cache_dir(cache.clone())
    };
    let mut cpu = TextEmbedding::try_new(base().with_intra_threads(8))?;
    let t = Instant::now();
    let cpu_vecs = cpu.embed(refs.clone(), Some(10))?;
    println!("cpu  {:6.0} ms", t.elapsed().as_secs_f64() * 1000.0);
    match TextEmbedding::try_new(base().with_execution_providers(gpu())) {
        Ok(mut g) => {
            let _ = g.embed(vec!["warm up"], None)?;
            let t = Instant::now();
            let gpu_vecs = g.embed(refs.clone(), Some(10))?;
            println!("gpu  {:6.0} ms", t.elapsed().as_secs_f64() * 1000.0);
            let sims: Vec<f32> = cpu_vecs.iter().zip(&gpu_vecs).map(|(a, b)| cosine(a, b)).collect();
            let min = sims.iter().cloned().fold(f32::MAX, f32::min);
            let mean = sims.iter().sum::<f32>() / sims.len() as f32;
            println!("cpu-vs-gpu cosine: min {min:.6} mean {mean:.6}");
        }
        Err(e) => println!("gpu  unavailable: {e}"),
    }

    println!("\n== reranker (jina v2 multilingual), 20 queries x 10 docs");
    let queries: Vec<String> = texts.iter().step_by(15).take(20).map(|t| t.chars().take(120).collect()).collect();
    let rbase = || RerankInitOptions::new(RerankerModel::JINARerankerV2BaseMultiligual).with_cache_dir(cache.clone());
    let mut rcpu = TextRerank::try_new(rbase())?;
    let docs: Vec<Vec<&str>> = (0..20)
        .map(|i| refs.iter().skip(i * 10).take(10).map(|d| &d[..d.floor_char_boundary(600.min(d.len()))]).collect())
        .collect();
    let t = Instant::now();
    let cpu_scores: Vec<Vec<f32>> = queries
        .iter()
        .zip(&docs)
        .map(|(q, d)| {
            let mut r = rcpu.rerank(q.as_str(), d.clone(), false, Some(16)).unwrap();
            r.sort_by_key(|x| x.index);
            r.into_iter().map(|x| x.score).collect()
        })
        .collect();
    println!("cpu  {:6.0} ms per query", t.elapsed().as_secs_f64() * 1000.0 / 20.0);
    match TextRerank::try_new(rbase().with_execution_providers(gpu())) {
        Ok(mut g) => {
            let _ = g.rerank("warm up", vec!["a", "b"], false, None)?;
            let t = Instant::now();
            let mut max_diff = 0f32;
            let mut same_top = 0;
            for ((q, d), cs) in queries.iter().zip(&docs).zip(&cpu_scores) {
                let mut r = g.rerank(q.as_str(), d.clone(), false, Some(16))?;
                r.sort_by_key(|x| x.index);
                let gs: Vec<f32> = r.into_iter().map(|x| x.score).collect();
                for (a, b) in cs.iter().zip(&gs) {
                    max_diff = max_diff.max((a - b).abs());
                }
                let top = |s: &[f32]| s.iter().enumerate().max_by(|a, b| a.1.total_cmp(b.1)).unwrap().0;
                same_top += (top(cs) == top(&gs)) as usize;
            }
            println!("gpu  {:6.0} ms per query", t.elapsed().as_secs_f64() * 1000.0 / 20.0);
            println!("cpu-vs-gpu score max abs diff {max_diff:.5}; same top-1 in {same_top}/20 queries");
        }
        Err(e) => println!("gpu  unavailable: {e}"),
    }
    Ok(())
}
