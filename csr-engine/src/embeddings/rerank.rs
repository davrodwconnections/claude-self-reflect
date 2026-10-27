//! Optional cross-encoder reranker for reflect_on_past.
//!
//! Off unless `CSR_RERANKER` names a model (`jina` or `bge`). The model is
//! loaded on first use and kept for the life of the process; a load failure
//! disables reranking instead of failing the search.

use fastembed::{RerankInitOptions, RerankerModel, TextRerank};
use std::sync::{Mutex, OnceLock};

static RERANKER: OnceLock<Option<Mutex<TextRerank>>> = OnceLock::new();

/// Max chars of a chunk passed to the cross-encoder. 600 scored as well as 1000
/// on the paraphrase set and keeps latency near 1 s for ten documents.
const DOC_CHARS: usize = 600;

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

/// Relevance scores for `docs` against `query`, in input order. `None` when
/// reranking is off or the model could not be loaded or run.
pub fn scores(query: &str, docs: &[String]) -> Option<Vec<f32>> {
    let cell = RERANKER.get_or_init(|| {
        let model = model_from_env()?;
        let cache_dir = super::cache::cache_dir();
        let options = RerankInitOptions::new(model).with_cache_dir(cache_dir);
        match TextRerank::try_new(options) {
            Ok(m) => Some(Mutex::new(m)),
            Err(e) => {
                tracing::warn!(error = %e, "reranker init failed; reranking disabled");
                None
            }
        }
    });
    let mut model = cell.as_ref()?.lock().ok()?;
    let trimmed: Vec<&str> = docs
        .iter()
        .map(|d| crate::format::truncate_chars(d, doc_chars()))
        .collect();
    let results = match model.rerank(query, trimmed, false, Some(16)) {
        Ok(r) => r,
        Err(e) => {
            tracing::warn!(error = %e, "rerank failed; keeping fused order");
            return None;
        }
    };
    let mut out = vec![f32::MIN; docs.len()];
    for r in results {
        if let Some(slot) = out.get_mut(r.index) {
            *slot = r.score;
        }
    }
    Some(out)
}
