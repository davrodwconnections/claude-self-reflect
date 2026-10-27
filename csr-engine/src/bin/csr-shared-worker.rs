//! Dedicated worker: one model/index, owned by the local shared supervisor.
use anyhow::Result;
use csr_engine::{engine::Engine, mcp::CsrServer};
use std::path::PathBuf;

#[tokio::main]
async fn main() -> Result<()> {
    let args: Vec<String> = std::env::args().collect();
    if args.get(1).map(String::as_str) == Some("--catalog") {
        println!("{}", serde_json::to_string(&CsrServer::tool_catalog())?);
        return Ok(());
    }
    anyhow::ensure!(args.len() == 3, "Expected database and projects paths");
    std::env::set_var("CSR_SHARED_WORKER", "1");
    std::env::set_var("CSR_FORCE_IMPORT", "1");
    // The shared worker reranks reflect_on_past with the multilingual
    // cross-encoder unless the supervisor says otherwise (CSR_RERANKER=off).
    if std::env::var_os("CSR_RERANKER").is_none() {
        std::env::set_var("CSR_RERANKER", "jina");
    }
    // Run ONNX on the GPU (DirectML) unless told otherwise; this machine's CPU
    // crashes under sustained ONNX load. Falls back to the CPU if the GPU fails.
    if std::env::var_os("CSR_GPU").is_none() {
        std::env::set_var("CSR_GPU", "directml");
    }
    // The worker stays resident so the CPU never reloads the index, but frees
    // the GPU memory the models hold (~2.3 GB) after 10 idle minutes.
    if std::env::var_os("CSR_MODEL_IDLE_SECS").is_none() {
        std::env::set_var("CSR_MODEL_IDLE_SECS", "600");
    }
    Engine::new(&PathBuf::from(&args[1]), &PathBuf::from(&args[2]))?
        .serve_mcp().await
}
