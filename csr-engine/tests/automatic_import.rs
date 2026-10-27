use csr_engine::{engine::Engine, import::parse_jsonl_file, search::SearchEngine};
use std::{fs,io::Write};

#[tokio::test]
async fn refreshes_growing_tail_and_skips_unchanged_content() {
    let d=tempfile::tempdir().unwrap();
    let source=d.path().join("conversation.jsonl");
    fs::write(&source,r#"{"type":"user","timestamp":"2026-10-05T00:00:00Z","message":{"content":"First test message"}}"#).unwrap();
    let engine=Engine::new(&d.path().join("test.db"),d.path()).unwrap();
    assert_eq!(engine.import_file(&source,"test").await.unwrap(),1);
    std::thread::sleep(std::time::Duration::from_millis(25));
    let mut f=fs::OpenOptions::new().append(true).open(&source).unwrap();
    writeln!(f,"\n{}",r#"{"type":"assistant","timestamp":"2026-10-05T00:01:00Z","message":{"content":"Newly appended response"}}"#).unwrap();
    drop(f);
    assert_eq!(parse_jsonl_file(&source,"test").unwrap().len(),1);
    assert_eq!(engine.import_file(&source,"test").await.unwrap(),1);
    assert_eq!(engine.import_file(&source,"test").await.unwrap(),0);
    let db=rusqlite::Connection::open(d.path().join("test.db")).unwrap();
    let content:String=db.query_row("select content from chunks",[],|r|r.get(0)).unwrap();
    assert!(content.contains("Newly appended response"));
    assert_eq!(db.query_row("select count(*) from chunks",[],|r|r.get::<_,i64>(0)).unwrap(),1);
}

#[test]
fn malformed_cache_becomes_a_cache_miss() {
    let d=tempfile::tempdir().unwrap();
    let mut search=SearchEngine::new(100);
    search.insert_chunk("test".into(),vec![0.25;384]);
    search.dump_to_disk(d.path(),1,0).unwrap();
    fs::write(d.path().join("chunks.hnsw.data"),[0u8;64]).unwrap();
    assert!(SearchEngine::load_from_disk(d.path(),1,0).is_none());
}

#[tokio::test]
async fn timestamp_only_changes_do_not_reembed_old_blocks() {
    let d=tempfile::tempdir().unwrap();
    let source=d.path().join("long.jsonl");
    let initial=serde_json::json!({"type":"user","timestamp":"2026-10-05T00:00:00Z","message":{"content":"a".repeat(2000)}});
    fs::write(&source,initial.to_string()).unwrap();
    let engine=Engine::new(&d.path().join("test.db"),d.path()).unwrap();
    let original=parse_jsonl_file(&source,"test").unwrap().len();
    assert!(original>1);
    assert_eq!(engine.import_file(&source,"test").await.unwrap(),original);
    std::thread::sleep(std::time::Duration::from_millis(25));
    let mut f=fs::OpenOptions::new().append(true).open(&source).unwrap();
    writeln!(f,"\n{}",r#"{"type":"assistant","timestamp":"2026-10-05T00:01:00Z","message":{"content":"New final reply"}}"#).unwrap();
    drop(f);
    assert_eq!(engine.import_file(&source,"test").await.unwrap(),1);
}

#[tokio::test]
async fn restores_missing_cached_vector_without_reembedding() {
    let d = tempfile::tempdir().unwrap();
    let source = d.path().join("external.jsonl");
    fs::write(&source, r#"{"type":"user","timestamp":"2026-10-06T00:00:00Z","message":{"content":"Stored vector should survive a stale reader cache"}}"#).unwrap();
    let engine = Engine::new(&d.path().join("test.db"), d.path()).unwrap();
    assert_eq!(engine.import_file(&source, "test").await.unwrap(), 1);
    let chunks = parse_jsonl_file(&source, "test").unwrap();
    let id = &chunks[0].id;
    engine.search().write().await.remove_chunk(id);
    std::thread::sleep(std::time::Duration::from_millis(25));
    fs::OpenOptions::new().append(true).open(&source).unwrap().write_all(b"\n").unwrap();
    assert_eq!(engine.import_file(&source, "test").await.unwrap(), 0);
    assert!(engine.search().read().await.has_chunk(id));
}
