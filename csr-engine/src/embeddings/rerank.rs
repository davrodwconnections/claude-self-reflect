//! Optional cross-encoder reranker for reflect_on_past.
//!
//! Off unless `CSR_RERANKER` names a model (`jina` or `bge`). The model is
//! loaded on first use and reloaded after an idle unload (see
//! `spawn_idle_unloader`); a load failure disables reranking for the life of
//! the process instead of failing the search.

use fastembed::{RerankInitOptions, RerankerModel, TextRerank};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, LazyLock, Mutex, Once};

static RERANKER: LazyLock<Arc<Mutex<Option<TextRerank>>>> =
    LazyLock::new(|| Arc::new(Mutex::new(None)));
static LAST_USED: LazyLock<Arc<AtomicU64>> = LazyLock::new(|| Arc::new(AtomicU64::new(0)));
static INIT_FAILED: AtomicBool = AtomicBool::new(false);
static UNLOADER: Once = Once::new();

/// Max chars of a chunk passed to the cross-encoder. 600 scored as well as 1000
/// on the paraphrase set and keeps latency near 1 s for ten documents.
const DOC_CHARS: usize = 600;

/// ONNX intra-op threads when the reranker runs on the CPU.
const CPU_THREADS: usize = 8;

fn model_from_env() -> Option<RerankerModel> {
    match std::env::var("CSR_RERANKER").ok()?.as_str() {
        "jina" => Some(RerankerModel::JINARerankerV2BaseMultiligual),
        "bge" => Some(RerankerModel::BGERerankerV2M3),
        _ => None,
    }
}

fn doc_chars() -> usize {
    std::env::var("CSR_RERANK_DOC_CHARS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(DOC_CHARS)
}

pub fn enabled() -> bool {
    model_from_env().is_some()
}

fn load(model: RerankerModel) -> Option<TextRerank> {
    let cache_dir = super::cache::cache_dir();
    let options = || RerankInitOptions::new(model.clone()).with_cache_dir(cache_dir.clone());
    if let Some(providers) = super::gpu_providers() {
        match TextRerank::try_new(options().with_execution_providers(providers)) {
            Ok(m) => return Some(m),
            Err(e) => tracing::warn!(error = %e, "GPU reranker init failed; using CPU"),
        }
    }
    // CPU fallback with bounded threads: ONNX on every core is the load that
    // crashes this machine's CPU.
    match TextRerank::try_new(options().with_intra_threads(CPU_THREADS)) {
        Ok(m) => Some(m),
        Err(e) => {
            tracing::warn!(error = %e, "reranker init failed; reranking disabled");
            None
        }
    }
}

/// Relevance scores for `docs` against `query`, in input order. `None` when
/// reranking is off or the model could not be loaded or run.
pub fn scores(query: &str, docs: &[String]) -> Option<Vec<f32>> {
    let model = model_from_env()?;
    if INIT_FAILED.load(Ordering::Relaxed) {
        return None;
    }
    UNLOADER.call_once(|| super::spawn_idle_unloader(&RERANKER, &LAST_USED, "reranker"));
    let mut slot = RERANKER.lock().ok()?;
    if slot.is_none() {
        match load(model) {
            Some(m) => *slot = Some(m),
            None => {
                INIT_FAILED.store(true, Ordering::Relaxed);
                return None;
            }
        }
    }
    LAST_USED.store(super::now_secs(), Ordering::Relaxed);
    let trimmed: Vec<&str> = docs
        .iter()
        .map(|d| crate::format::truncate_chars(d, doc_chars()))
        .collect();
    let results = match slot.as_mut().expect("loaded above").rerank(query, trimmed, false, Some(16)) {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(error = %e, "rerank failed; keeping fused order");
            return None;
        }
    };
    LAST_USED.store(super::now_secs(), Ordering::Relaxed);
    let mut out = vec![f32::MIN; docs.len()];
    for r in results {
        if let Some(slot) = out.get_mut(r.index) {
            *slot = r.score;
        }
    }
    Some(out)
}
