use std::fs;

use csr_engine::import::{parse_jsonl_file, parse_jsonl_messages};

#[test]
fn imports_codex_rollout_messages_and_tool_activity() {
    let path = std::env::temp_dir().join(format!("csr-codex-rollout-{}.jsonl", std::process::id()));
    let lines = [
        r#"{"type":"session_meta","timestamp":"2026-09-19T01:00:00Z","payload":{"cwd":"D:\\Proyectos\\General"}}"#,
        r#"{"type":"response_item","timestamp":"2026-09-19T01:00:01Z","payload":{"type":"message","role":"developer","content":[{"type":"input_text","text":"runtime instructions"}]}}"#,
        r#"{"type":"response_item","timestamp":"2026-09-19T01:00:02Z","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Inspect the current configuration"}]}}"#,
        r#"{"type":"response_item","timestamp":"2026-09-19T01:00:03Z","payload":{"type":"custom_tool_call","call_id":"call-1","name":"exec_command","input":"{\"cmd\":\"echo ok\"}"}}"#,
        r#"{"type":"response_item","timestamp":"2026-09-19T01:00:04Z","payload":{"type":"custom_tool_call_output","call_id":"call-1","output":"ok"}}"#,
        r#"{"type":"response_item","timestamp":"2026-09-19T01:00:05Z","payload":{"type":"message","role":"assistant","content":[{"type":"output_text","text":"The configuration is valid."}]}}"#,
    ];
    fs::write(&path, lines.join("\n")).unwrap();

    let messages = parse_jsonl_messages(&path).unwrap();
    assert_eq!(messages.len(), 4);
    assert_eq!(messages[0]["type"], "user");
    assert_eq!(messages[1]["type"], "assistant");
    assert_eq!(messages[2]["type"], "user");
    assert_eq!(messages[3]["type"], "assistant");

    let chunks = parse_jsonl_file(&path, "D--Proyectos-General").unwrap();
    let content = chunks
        .iter()
        .map(|chunk| chunk.content.as_str())
        .collect::<Vec<_>>()
        .join("\n");
    assert!(content.contains("Inspect the current configuration"));
    assert!(content.contains("exec_command"));
    assert!(content.contains("echo ok"));
    assert!(content.contains("The configuration is valid."));
    assert!(!content.contains("runtime instructions"));

    let _ = fs::remove_file(path);
}
