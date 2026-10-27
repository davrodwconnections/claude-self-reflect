pub mod cache;
pub mod rerank;

use std::{
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
    sync::{Arc, Mutex},
};

use anyhow::Result;
use fastembed::{EmbeddingModel, InitOptions, TextEmbedding};

const EMBEDDING_INTRA_THREADS: usize = 8;

/// Wraps fastembed for quantized 384-dim paraphrase-multilingual-MiniLM-L12-v2
/// embeddings.
///
/// Upstream uses all-MiniLM-L6-v2, which is English-only: on a Spanish-language
/// transcript corpus it returns scores that are both low and tightly clustered
/// (measured: 0.544-0.551 across the top 3 of an unrelated query), so ranking
/// carries almost no signal. The multilingual model has the same 384 dimensions,
/// which keeps the schema and the HNSW index unchanged, and needs no query/passage
/// prefixes (unlike the E5 family).
///
/// Thread-safe via Mutex (TextEmbedding::embed requires &mut self). The model
/// can be unloaded while idle (see [`spawn_idle_unloader`]) and is reloaded on
/// the next call.
pub struct EmbeddingEngine {
    model: Arc<Mutex<Option<TextEmbedding>>>,
    last_used: Arc<AtomicU64>,
}

/// Serializes first-run model downloads within this process. Concurrent
/// `TextEmbedding::try_new` calls (parallel test threads, racing hooks on a
/// fresh install) contend on hf-hub's cross-process blob lock and fail with
/// "Lock acquisition failed" instead of waiting.
static MODEL_INIT_LOCK: Mutex<()> = Mutex::new(());

impl EmbeddingEngine {
    /// Initialize the embedding model (downloads the quantized model on first run).
    pub fn new() -> Result<Self> {
        let engine = Self {
            model: Arc::new(Mutex::new(Some(load_model()?))),
            last_used: Arc::new(AtomicU64::new(now_secs())),
        };
        spawn_idle_unloader(&engine.model, &engine.last_used, "embedding");
        Ok(engine)
    }

    /// Embed a batch of texts. Returns one 384-dim vector per input.
    pub fn embed(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>> {
        let docs: Vec<String> = texts.iter().map(|s| s.to_string()).collect();
        let mut slot = self
            .model
            .lock()
            .map_err(|e| anyhow::anyhow!("embedding lock: {e}"))?;
        if slot.is_none() {
            *slot = Some(load_model()?);
        }
        self.last_used.store(now_secs(), Ordering::Relaxed);
        let embeddings = slot.as_mut().expect("loaded above").embed(docs, None)?;
        self.last_used.store(now_secs(), Ordering::Relaxed);
        Ok(embeddings)
    }

    /// Embed a single text string.
    pub fn embed_single(&self, text: &str) -> Result<Vec<f32>> {
        let results = self.embed(&[text])?;
        results
            .into_iter()
            .next()
            .ok_or_else(|| anyhow::anyhow!("embedding returned empty result"))
    }

    /// Returns the embedding dimension (384 for all-MiniLM-L6-v2).
    pub fn dimension() -> usize {
        384
    }
}

/// Load the embedding model: on the GPU when enabled, else (or on GPU failure)
/// on the CPU, retrying across processes that may hold the download lock.
fn load_model() -> Result<TextEmbedding> {
    let cache_dir = cache::cache_dir();
    std::fs::create_dir_all(&cache_dir)?;

    let _init_guard = MODEL_INIT_LOCK
        .lock()
        .map_err(|e| anyhow::anyhow!("model init lock: {e}"))?;

    if let Some(providers) = gpu_providers() {
        let options = embedding_init_options(cache_dir.clone()).with_execution_providers(providers);
        match TextEmbedding::try_new(options) {
            Ok(model) => return Ok(model),
            Err(e) => tracing::warn!(error = %e, "GPU embedding init failed; using CPU"),
        }
    }

    // Retry across processes: another csr-engine may hold the hf-hub blob
    // lock mid-download; once it finishes, the cached model loads instantly.
    let mut last_err = None;
    for attempt in 0..3 {
        if attempt > 0 {
            std::thread::sleep(std::time::Duration::from_secs(2 * attempt as u64));
        }
        match TextEmbedding::try_new(embedding_init_options(cache_dir.clone())) {
            Ok(model) => return Ok(model),
            Err(e) => last_err = Some(e),
        }
    }
    Err(anyhow::anyhow!(
        "embedding model init failed after retries: {}",
        last_err.expect("at least one attempt ran")
    ))
}

pub(crate) fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// Seconds a model may sit unused before it is unloaded; `CSR_MODEL_IDLE_SECS`,
/// 0 (the default) keeps models loaded. The shared worker stays resident so the
/// CPU never reloads the index, but frees the GPU memory the models hold.
fn model_idle_secs() -> u64 {
    std::env::var("CSR_MODEL_IDLE_SECS")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(0)
}

/// Background thread that empties `slot` once it has been idle longer than
/// [`model_idle_secs`]. Holds only a weak reference, so it exits with its owner.
pub(crate) fn spawn_idle_unloader<T: Send + 'static>(
    slot: &Arc<Mutex<Option<T>>>,
    last_used: &Arc<AtomicU64>,
    what: &'static str,
) {
    let idle = model_idle_secs();
    if idle == 0 {
        return;
    }
    let slot = Arc::downgrade(slot);
    let last_used = Arc::downgrade(last_used);
    std::thread::spawn(move || loop {
        std::thread::sleep(std::time::Duration::from_secs(30));
        let (Some(slot), Some(last_used)) = (slot.upgrade(), last_used.upgrade()) else {
            return;
        };
        if now_secs().saturating_sub(last_used.load(Ordering::Relaxed)) < idle {
            continue;
        }
        // try_lock: a model in use is by definition not idle.
        let locked = slot.try_lock();
        if let Ok(mut guard) = locked {
            if guard.take().is_some() {
                tracing::info!(model = what, idle_secs = idle, "model unloaded after idle");
            }
        };
    });
}

/// DirectML on the high-performance GPU when `CSR_GPU=directml`, else `None`
/// (CPU). Registration errors instead of silently falling back, so callers can
/// log the fallback; on this machine the CPU crashes under sustained ONNX load
/// (i9-13900K instability), which is why the GPU path exists.
pub(crate) fn gpu_providers() -> Option<Vec<fastembed::ExecutionProviderDispatch>> {
    if std::env::var("CSR_GPU").ok().as_deref() != Some("directml") {
        return None;
    }
    Some(vec![ort::ep::DirectML::default()
        .with_performance_preference(ort::ep::directml::PerformancePreference::HighPerformance)
        .build()
        .error_on_failure()])
}

/// ONNX intra-op threads for embedding; `CSR_EMBED_THREADS` overrides the
/// default. Bulk re-imports crashed inside ONNX Runtime's threaded GEMM
/// (MlasSgemmThreaded) on this machine, so the knob allows a quieter setting.
fn embedding_threads() -> usize {
    std::env::var("CSR_EMBED_THREADS")
        .ok()
        .and_then(|v| v.parse().ok())
        .filter(|n: &usize| *n > 0)
        .unwrap_or(EMBEDDING_INTRA_THREADS)
}

fn embedding_init_options(cache_dir: PathBuf) -> InitOptions {
    InitOptions::new(EmbeddingModel::ParaphraseMLMiniLML12V2Q)
        .with_cache_dir(cache_dir)
        .with_intra_threads(embedding_threads())
        .with_show_download_progress(true)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn uses_quantized_multilingual_model_with_bounded_threads() {
        let cache_dir = PathBuf::from("test-fastembed-cache");
        let options = embedding_init_options(cache_dir.clone());

        assert_eq!(options.model_name, EmbeddingModel::ParaphraseMLMiniLML12V2Q);
        assert_eq!(options.cache_dir, cache_dir);
        assert_eq!(options.intra_threads, Some(8));
    }
}
