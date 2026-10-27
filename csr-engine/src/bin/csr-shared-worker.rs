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
    Engine::new(&PathBuf::from(&args[1]), &PathBuf::from(&args[2]))?
        .serve_mcp().await
}
