pub mod cache;

use std::{path::PathBuf, sync::Mutex};

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
/// Thread-safe via Mutex (TextEmbedding::embed requires &mut self).
pub struct EmbeddingEngine {
    model: Mutex<TextEmbedding>,
}

/// Serializes first-run model downloads within this process. Concurrent
/// `TextEmbedding::try_new` calls (parallel test threads, racing hooks on a
/// fresh install) contend on hf-hub's cross-process blob lock and fail with
/// "Lock acquisition failed" instead of waiting.
static MODEL_INIT_LOCK: Mutex<()> = Mutex::new(());

impl EmbeddingEngine {
    /// Initialize the embedding model (downloads the quantized model on first run).
    pub fn new() -> Result<Self> {
        let cache_dir = cache::cache_dir();
        std::fs::create_dir_all(&cache_dir)?;

        let _init_guard = MODEL_INIT_LOCK
            .lock()
            .map_err(|e| anyhow::anyhow!("model init lock: {e}"))?;

        // Retry across processes: another csr-engine may hold the hf-hub blob
        // lock mid-download; once it finishes, the cached model loads instantly.
        let mut last_err = None;
        for attempt in 0..3 {
            if attempt > 0 {
                std::thread::sleep(std::time::Duration::from_secs(2 * attempt as u64));
            }
            let options = embedding_init_options(cache_dir.clone());
            match TextEmbedding::try_new(options) {
                Ok(model) => {
                    return Ok(Self {
                        model: Mutex::new(model),
                    })
                }
                Err(e) => last_err = Some(e),
            }
        }
        Err(anyhow::anyhow!(
            "embedding model init failed after retries: {}",
            last_err.expect("at least one attempt ran")
        ))
    }

    /// Embed a batch of texts. Returns one 384-dim vector per input.
    pub fn embed(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>> {
        let docs: Vec<String> = texts.iter().map(|s| s.to_string()).collect();
        let mut model = self
            .model
            .lock()
            .map_err(|e| anyhow::anyhow!("embedding lock: {e}"))?;
        let embeddings = model.embed(docs, None)?;
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

fn embedding_init_options(cache_dir: PathBuf) -> InitOptions {
    InitOptions::new(EmbeddingModel::ParaphraseMLMiniLML12V2Q)
        .with_cache_dir(cache_dir)
        .with_intra_threads(EMBEDDING_INTRA_THREADS)
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
