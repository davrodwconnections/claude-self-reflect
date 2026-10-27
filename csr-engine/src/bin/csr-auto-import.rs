//! Bounded local import manifest. No daemon, AI narration, or network model calls.
use std::path::PathBuf;
use anyhow::Result;
use serde::Deserialize;
use csr_engine::engine::Engine;

#[derive(Deserialize)]
struct Manifest { db_path: PathBuf, files: Vec<Source> }
#[derive(Deserialize)]
struct Source { path: PathBuf, project: String }

#[tokio::main]
async fn main() -> Result<()> {
    let path = std::env::args_os().nth(1).expect("manifest path required");
    let manifest: Manifest = serde_json::from_slice(&std::fs::read(path)?)?;
    let engine = Engine::new(&manifest.db_path, &PathBuf::new())?;
    let mut failed = 0;
    for (i, source) in manifest.files.into_iter().enumerate() {
        let result = engine.import_file(&source.path, &source.project).await;
        match result {
            Ok(chunks) => println!("{}", serde_json::json!({"path":source.path,"status":"ok","chunks_updated":chunks})),
            Err(error) => {
                failed += 1;
                println!("{}", serde_json::json!({"path":source.path,"status":"error","error":error.to_string()}));
            }
        }
        if (i + 1) % 25 == 0 { engine.flush_index().await; }
    }
    engine.flush_index().await;
    anyhow::ensure!(failed == 0, "{failed} source files failed");
    Ok(())
}
