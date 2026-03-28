//! Integration tests for the unprocessed IPFIX packet handling system.

#![allow(
    clippy::cast_possible_truncation,
    clippy::cast_precision_loss,
    clippy::redundant_closure_for_method_calls,
    clippy::missing_const_for_fn,
    clippy::doc_markdown
)]

use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::atomic::Ordering::Relaxed;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::Duration;

use tokio::sync::Mutex;

use flowcus_core::observability::Metrics;
use flowcus_ipfix::listener::MessageSink;
use flowcus_ipfix::protocol::{self, IPFIX_VERSION, IpfixMessage, SetContents};
use flowcus_ipfix::session::SessionStore;
use flowcus_ipfix::unprocessed;

// ---------------------------------------------------------------------------
// Test helpers
// ---------------------------------------------------------------------------

/// A MessageSink that collects all received messages for assertions.
struct CollectingSink {
    messages: StdMutex<Vec<IpfixMessage>>,
}

impl CollectingSink {
    fn new() -> Self {
        Self {
            messages: StdMutex::new(Vec::new()),
        }
    }

    fn message_count(&self) -> usize {
        self.messages.lock().unwrap().len()
    }

    fn take_messages(&self) -> Vec<IpfixMessage> {
        std::mem::take(&mut *self.messages.lock().unwrap())
    }
}

impl MessageSink for CollectingSink {
    fn on_message(&self, msg: IpfixMessage) {
        self.messages.lock().unwrap().push(msg);
    }
}

fn test_addr() -> SocketAddr {
    "10.0.0.1:4739".parse().unwrap()
}

/// Build an IPFIX message that contains ONLY a data set referencing `template_id`.
/// No template set is included, so this packet cannot be decoded without a
/// prior template.
fn build_data_only_packet(template_id: u16) -> Vec<u8> {
    let mut msg = Vec::new();

    // Data Set: set_id = template_id, length = 12 (4 header + 8 data)
    let mut data_set = Vec::new();
    data_set.extend_from_slice(&template_id.to_be_bytes());
    data_set.extend_from_slice(&12u16.to_be_bytes());
    // Two 4-byte fields: 192.168.1.1 -> 10.0.0.1
    data_set.extend_from_slice(&[192, 168, 1, 1]);
    data_set.extend_from_slice(&[10, 0, 0, 1]);

    // Message header
    let total_len = 16 + data_set.len();
    msg.extend_from_slice(&IPFIX_VERSION.to_be_bytes());
    msg.extend_from_slice(&(total_len as u16).to_be_bytes());
    msg.extend_from_slice(&1_700_000_000u32.to_be_bytes()); // export time
    msg.extend_from_slice(&1u32.to_be_bytes()); // seq
    msg.extend_from_slice(&1u32.to_be_bytes()); // domain
    msg.extend_from_slice(&data_set);

    msg
}

/// Build a template set message that defines `template_id` with two fields:
/// IE 8 (sourceIPv4Address, 4 bytes) and IE 12 (destinationIPv4Address, 4 bytes).
fn build_template_packet(template_id: u16) -> Vec<u8> {
    let mut msg = Vec::new();

    let mut tmpl_set = Vec::new();
    tmpl_set.extend_from_slice(&2u16.to_be_bytes()); // set_id = TEMPLATE
    tmpl_set.extend_from_slice(&16u16.to_be_bytes()); // set_length
    tmpl_set.extend_from_slice(&template_id.to_be_bytes());
    tmpl_set.extend_from_slice(&2u16.to_be_bytes()); // field_count
    tmpl_set.extend_from_slice(&8u16.to_be_bytes()); // IE 8
    tmpl_set.extend_from_slice(&4u16.to_be_bytes()); // length 4
    tmpl_set.extend_from_slice(&12u16.to_be_bytes()); // IE 12
    tmpl_set.extend_from_slice(&4u16.to_be_bytes()); // length 4

    let total_len = 16 + tmpl_set.len();
    msg.extend_from_slice(&IPFIX_VERSION.to_be_bytes());
    msg.extend_from_slice(&(total_len as u16).to_be_bytes());
    msg.extend_from_slice(&1_700_000_000u32.to_be_bytes());
    msg.extend_from_slice(&1u32.to_be_bytes());
    msg.extend_from_slice(&1u32.to_be_bytes()); // domain
    msg.extend_from_slice(&tmpl_set);

    msg
}

/// Register a template in the session by parsing a template packet.
fn register_template(session: &mut SessionStore, template_id: u16) {
    let tmpl_packet = build_template_packet(template_id);
    let addr = test_addr();
    let mut msg = protocol::parse_message(&tmpl_packet, addr).unwrap();
    flowcus_ipfix::decoder::decode_message(&mut msg, &tmpl_packet, session);
}

fn temp_dir(name: &str) -> PathBuf {
    let dir = std::env::temp_dir().join("flowcus_unproc_tests").join(name);
    let _ = std::fs::remove_dir_all(&dir);
    std::fs::create_dir_all(&dir).unwrap();
    dir
}

fn count_unproc_files(dir: &PathBuf) -> usize {
    std::fs::read_dir(dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .filter(|e| e.path().extension().is_some_and(|ext| ext == "unproc"))
        .count()
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[test]
fn test_save_and_read_unproc_file() {
    let dir = temp_dir("save_and_read");
    let packet = build_data_only_packet(256);
    let addr = test_addr();

    unprocessed::save_unprocessed(&dir, &packet, addr, &[256]).unwrap();

    // Verify a .unproc file exists
    assert_eq!(count_unproc_files(&dir), 1);

    // Read the file and verify header
    let files: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .collect();
    let data = std::fs::read(files[0].path()).unwrap();

    // Check magic
    assert_eq!(&data[0..4], b"FUPR");
    // Check version
    assert_eq!(u32::from_le_bytes(data[4..8].try_into().unwrap()), 1);
    // Check data_len matches packet length
    let data_len = u16::from_le_bytes(data[22..24].try_into().unwrap()) as usize;
    assert_eq!(data_len, packet.len());
    // Check that the raw packet is preserved
    assert_eq!(&data[24..24 + data_len], &packet[..]);

    // File name should contain the template ID
    let name = files[0].file_name().to_string_lossy().to_string();
    assert!(name.ends_with("_256.unproc"));

    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn test_reprocess_after_template_arrives() {
    let dir = temp_dir("reprocess");
    let packet = build_data_only_packet(256);
    let addr = test_addr();

    // Save the packet as unprocessed
    unprocessed::save_unprocessed(&dir, &packet, addr, &[256]).unwrap();
    assert_eq!(count_unproc_files(&dir), 1);

    // Now add the template to the session
    let session = Arc::new(Mutex::new(SessionStore::new(1800)));
    {
        let mut s = session.lock().await;
        register_template(&mut s, 256);
    }

    let sink = Arc::new(CollectingSink::new());
    let metrics = Metrics::new();

    // Run a single scan
    unprocessed::scan_once(
        &dir,
        &session,
        &(sink.clone() as Arc<dyn MessageSink>),
        &metrics,
        Duration::from_secs(300),
    )
    .await
    .unwrap();

    // File should be gone, message should be forwarded
    assert_eq!(count_unproc_files(&dir), 0);
    assert_eq!(sink.message_count(), 1);

    // Check that the decoded message actually has records
    let msgs = sink.take_messages();
    let data_set = msgs[0].sets.iter().find_map(|s| {
        if let SetContents::Data(d) = &s.contents {
            Some(d)
        } else {
            None
        }
    });
    assert!(data_set.is_some());
    assert_eq!(data_set.unwrap().records.len(), 1);

    // Check metrics
    assert_eq!(metrics.ipfix_unprocessed_reprocessed.load(Relaxed), 1);
    assert_eq!(metrics.ipfix_unprocessed_pending.load(Relaxed), 0);

    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn test_expire_old_unprocessed() {
    let dir = temp_dir("expire");
    let packet = build_data_only_packet(256);
    let addr = test_addr();

    // Save with a timestamp that is definitely old
    let old_ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
        - 600_000; // 10 minutes ago

    unprocessed::save_unprocessed_with_timestamp(&dir, &packet, addr, &[256], old_ts).unwrap();
    assert_eq!(count_unproc_files(&dir), 1);

    let session = Arc::new(Mutex::new(SessionStore::new(1800)));
    let sink: Arc<dyn MessageSink> = Arc::new(CollectingSink::new());
    let metrics = Metrics::new();

    // Scan with a short TTL (5 seconds) — the file is 10 minutes old
    unprocessed::scan_once(&dir, &session, &sink, &metrics, Duration::from_secs(5))
        .await
        .unwrap();

    // File should be deleted
    assert_eq!(count_unproc_files(&dir), 0);
    assert_eq!(metrics.ipfix_unprocessed_expired.load(Relaxed), 1);
    assert_eq!(metrics.ipfix_unprocessed_reprocessed.load(Relaxed), 0);

    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn test_unexpired_file_kept() {
    let dir = temp_dir("unexpired");
    let packet = build_data_only_packet(256);
    let addr = test_addr();

    // Save with current timestamp (default)
    unprocessed::save_unprocessed(&dir, &packet, addr, &[256]).unwrap();
    assert_eq!(count_unproc_files(&dir), 1);

    // Session has NO template for 256
    let session = Arc::new(Mutex::new(SessionStore::new(1800)));
    let sink: Arc<dyn MessageSink> = Arc::new(CollectingSink::new());
    let metrics = Metrics::new();

    // Scan with a long TTL — file is recent, template not available
    unprocessed::scan_once(&dir, &session, &sink, &metrics, Duration::from_secs(300))
        .await
        .unwrap();

    // File should still be there
    assert_eq!(count_unproc_files(&dir), 1);
    assert_eq!(metrics.ipfix_unprocessed_pending.load(Relaxed), 1);
    assert_eq!(metrics.ipfix_unprocessed_reprocessed.load(Relaxed), 0);
    assert_eq!(metrics.ipfix_unprocessed_expired.load(Relaxed), 0);

    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn test_multiple_unproc_files() {
    let dir = temp_dir("multiple");
    let addr = test_addr();

    // Save 3 packets with different template IDs: 256, 257, 258
    for tid in [256u16, 257, 258] {
        let packet = build_data_only_packet(tid);
        unprocessed::save_unprocessed(&dir, &packet, addr, &[tid]).unwrap();
    }
    assert_eq!(count_unproc_files(&dir), 3);

    // Add templates for 256 and 257 only
    let session = Arc::new(Mutex::new(SessionStore::new(1800)));
    {
        let mut s = session.lock().await;
        register_template(&mut s, 256);
        register_template(&mut s, 257);
    }

    let sink = Arc::new(CollectingSink::new());
    let metrics = Metrics::new();

    unprocessed::scan_once(
        &dir,
        &session,
        &(sink.clone() as Arc<dyn MessageSink>),
        &metrics,
        Duration::from_secs(300),
    )
    .await
    .unwrap();

    // 2 should be reprocessed, 1 should remain
    assert_eq!(count_unproc_files(&dir), 1);
    assert_eq!(sink.message_count(), 2);
    assert_eq!(metrics.ipfix_unprocessed_reprocessed.load(Relaxed), 2);
    assert_eq!(metrics.ipfix_unprocessed_pending.load(Relaxed), 1);

    // The remaining file should be for template 258
    let remaining: Vec<_> = std::fs::read_dir(&dir)
        .unwrap()
        .filter_map(|e| e.ok())
        .collect();
    assert_eq!(remaining.len(), 1);
    let name = remaining[0].file_name().to_string_lossy().to_string();
    assert!(name.contains("_258.unproc"));

    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn test_corrupt_unproc_file_deleted() {
    let dir = temp_dir("corrupt");

    // Write garbage to a .unproc file
    let garbage_path = dir.join("garbage_0_256.unproc");
    std::fs::write(&garbage_path, b"this is not valid data").unwrap();
    assert_eq!(count_unproc_files(&dir), 1);

    let session = Arc::new(Mutex::new(SessionStore::new(1800)));
    let sink: Arc<dyn MessageSink> = Arc::new(CollectingSink::new());
    let metrics = Metrics::new();

    // Scan should handle the corrupt file without panicking
    let result =
        unprocessed::scan_once(&dir, &session, &sink, &metrics, Duration::from_secs(300)).await;
    assert!(result.is_ok());

    // Corrupt file should be deleted
    assert_eq!(count_unproc_files(&dir), 0);

    std::fs::remove_dir_all(&dir).ok();
}

#[tokio::test]
async fn test_metrics_updated() {
    let dir = temp_dir("metrics");
    let addr = test_addr();
    let metrics = Metrics::new();

    // Save 2 packets: one for template 256 (will be reprocessed) and one for 257 (will expire)
    let packet_256 = build_data_only_packet(256);
    unprocessed::save_unprocessed(&dir, &packet_256, addr, &[256]).unwrap();

    let old_ts = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap()
        .as_millis() as u64
        - 600_000;
    let packet_257 = build_data_only_packet(257);
    unprocessed::save_unprocessed_with_timestamp(&dir, &packet_257, addr, &[257], old_ts).unwrap();

    // Also save one that will stay pending (258)
    let packet_258 = build_data_only_packet(258);
    unprocessed::save_unprocessed(&dir, &packet_258, addr, &[258]).unwrap();

    assert_eq!(count_unproc_files(&dir), 3);

    // Add template for 256 only
    let session = Arc::new(Mutex::new(SessionStore::new(1800)));
    {
        let mut s = session.lock().await;
        register_template(&mut s, 256);
    }

    let sink: Arc<dyn MessageSink> = Arc::new(CollectingSink::new());

    // TTL of 5 seconds — the 257 file (10 min old) will expire
    unprocessed::scan_once(&dir, &session, &sink, &metrics, Duration::from_secs(5))
        .await
        .unwrap();

    assert_eq!(metrics.ipfix_unprocessed_reprocessed.load(Relaxed), 1); // 256
    assert_eq!(metrics.ipfix_unprocessed_expired.load(Relaxed), 1); // 257
    assert_eq!(metrics.ipfix_unprocessed_pending.load(Relaxed), 1); // 258

    std::fs::remove_dir_all(&dir).ok();
}
