//! Save and reprocess IPFIX packets that arrived before their template.
//!
//! When a data set references a template ID that is not yet cached, the raw
//! IPFIX packet is written to a `.unproc` file.  A background worker
//! periodically scans the directory, retries packets whose templates have
//! since arrived, and expires files that exceed the configured TTL.

use std::io;
use std::net::{Ipv4Addr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use tokio::sync::Mutex;
use tracing::{debug, info, warn};

use flowcus_core::observability::Metrics;

use crate::decoder;
use crate::display::DisplayMessage;
use crate::listener::MessageSink;
use crate::protocol::{self, SetContents};
use crate::session::SessionStore;

// ---------------------------------------------------------------------------
// .unproc binary format
// ---------------------------------------------------------------------------

const MAGIC: &[u8; 4] = b"FUPR";
const FORMAT_VERSION: u32 = 1;
/// Fixed header size in bytes (magic 4 + version 4 + received_at_ms 8
/// + exporter_ip 4 + exporter_port 2 + data_len 2 = 24).
const HEADER_LEN: usize = 24;

/// Save a raw IPFIX packet that could not be decoded because one or more
/// templates were missing.
///
/// One file is written per missing `template_id` so that the worker can
/// quickly check template availability from the file name alone.
pub fn save_unprocessed(
    dir: &Path,
    raw_packet: &[u8],
    exporter: SocketAddr,
    template_ids: &[u16],
) -> io::Result<()> {
    std::fs::create_dir_all(dir)?;

    let received_at_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;

    let ip_u32 = match exporter.ip() {
        std::net::IpAddr::V4(v4) => u32::from(v4),
        std::net::IpAddr::V6(v6) => {
            // Best-effort: use the last 4 bytes for v4-mapped addresses
            if let Some(v4) = v6.to_ipv4_mapped() {
                u32::from(v4)
            } else {
                0
            }
        }
    };

    for &tid in template_ids {
        let filename = format!("{received_at_ms}_{ip_u32}_{tid}.unproc");
        let path = dir.join(&filename);

        let mut buf = Vec::with_capacity(HEADER_LEN + raw_packet.len());
        buf.extend_from_slice(MAGIC);
        buf.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        buf.extend_from_slice(&received_at_ms.to_le_bytes());
        buf.extend_from_slice(&ip_u32.to_le_bytes());
        buf.extend_from_slice(&exporter.port().to_le_bytes());
        buf.extend_from_slice(&(raw_packet.len() as u16).to_le_bytes());
        buf.extend_from_slice(raw_packet);

        std::fs::write(&path, &buf)?;
        debug!(file = %filename, template_id = tid, "Saved unprocessed IPFIX packet");
    }

    Ok(())
}

/// Save with an explicit `received_at_ms` timestamp.
///
/// Primarily used in tests to create intentionally old files.
#[doc(hidden)]
pub fn save_unprocessed_with_timestamp(
    dir: &Path,
    raw_packet: &[u8],
    exporter: SocketAddr,
    template_ids: &[u16],
    received_at_ms: u64,
) -> io::Result<()> {
    std::fs::create_dir_all(dir)?;

    let ip_u32 = match exporter.ip() {
        std::net::IpAddr::V4(v4) => u32::from(v4),
        _ => 0,
    };

    for &tid in template_ids {
        let filename = format!("{received_at_ms}_{ip_u32}_{tid}.unproc");
        let path = dir.join(&filename);

        let mut buf = Vec::with_capacity(HEADER_LEN + raw_packet.len());
        buf.extend_from_slice(MAGIC);
        buf.extend_from_slice(&FORMAT_VERSION.to_le_bytes());
        buf.extend_from_slice(&received_at_ms.to_le_bytes());
        buf.extend_from_slice(&ip_u32.to_le_bytes());
        buf.extend_from_slice(&exporter.port().to_le_bytes());
        buf.extend_from_slice(&(raw_packet.len() as u16).to_le_bytes());
        buf.extend_from_slice(raw_packet);

        std::fs::write(&path, &buf)?;
    }

    Ok(())
}

// ---------------------------------------------------------------------------
// Reading .unproc header
// ---------------------------------------------------------------------------

struct UnprocHeader {
    received_at_ms: u64,
    exporter_ip: u32,
    exporter_port: u16,
    data_len: u16,
}

fn read_header(data: &[u8]) -> Option<UnprocHeader> {
    if data.len() < HEADER_LEN {
        return None;
    }
    if &data[..4] != MAGIC {
        return None;
    }
    let version = u32::from_le_bytes(data[4..8].try_into().ok()?);
    if version != FORMAT_VERSION {
        return None;
    }
    Some(UnprocHeader {
        received_at_ms: u64::from_le_bytes(data[8..16].try_into().ok()?),
        exporter_ip: u32::from_le_bytes(data[16..20].try_into().ok()?),
        exporter_port: u16::from_le_bytes(data[20..22].try_into().ok()?),
        data_len: u16::from_le_bytes(data[22..24].try_into().ok()?),
    })
}

// ---------------------------------------------------------------------------
// Background worker
// ---------------------------------------------------------------------------

/// Start the background reprocessing worker.
///
/// The task runs at low priority, yielding between files so it doesn't
/// starve the IPFIX listener or storage writer.
pub fn start_worker(
    unprocessed_dir: PathBuf,
    session: Arc<Mutex<SessionStore>>,
    sink: Arc<dyn MessageSink>,
    metrics: Arc<Metrics>,
    ttl: Duration,
    scan_interval: Duration,
) {
    tokio::spawn(async move {
        let mut interval = tokio::time::interval(scan_interval);
        loop {
            interval.tick().await;
            if let Err(e) = scan_once(&unprocessed_dir, &session, &sink, &metrics, ttl).await {
                warn!(error = %e, "Unprocessed worker scan error");
            }
        }
    });
}

/// Run a single scan of the unprocessed directory.
///
/// Public so that tests can invoke it directly without spinning up the
/// full interval-based worker.
pub async fn scan_once(
    dir: &Path,
    session: &Arc<Mutex<SessionStore>>,
    sink: &Arc<dyn MessageSink>,
    metrics: &Arc<Metrics>,
    ttl: Duration,
) -> io::Result<()> {
    let entries: Vec<PathBuf> = match std::fs::read_dir(dir) {
        Ok(rd) => rd
            .filter_map(|e| e.ok())
            .filter(|e| e.path().extension().is_some_and(|ext| ext == "unproc"))
            .map(|e| e.path())
            .collect(),
        Err(e) if e.kind() == io::ErrorKind::NotFound => {
            // Directory doesn't exist yet — nothing to do.
            metrics
                .ipfix_unprocessed_pending
                .store(0, std::sync::atomic::Ordering::Relaxed);
            return Ok(());
        }
        Err(e) => return Err(e),
    };

    let now_ms = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64;

    let ttl_ms = ttl.as_millis() as u64;
    let mut pending_count: i64 = 0;

    for path in &entries {
        // Yield between files so we don't starve other tasks.
        tokio::task::yield_now().await;

        let raw = match std::fs::read(path) {
            Ok(d) => d,
            Err(e) => {
                warn!(file = %path.display(), error = %e, "Failed to read .unproc file");
                continue;
            }
        };

        let Some(header) = read_header(&raw) else {
            // Corrupt file — delete it.
            warn!(file = %path.display(), "Corrupt .unproc file, deleting");
            let _ = std::fs::remove_file(path);
            continue;
        };

        // TTL check
        if now_ms.saturating_sub(header.received_at_ms) > ttl_ms {
            debug!(file = %path.display(), "Unprocessed file expired, deleting");
            let _ = std::fs::remove_file(path);
            metrics
                .ipfix_unprocessed_expired
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
            continue;
        }

        // Validate data length
        let expected_len = HEADER_LEN + header.data_len as usize;
        if raw.len() < expected_len {
            warn!(file = %path.display(), "Truncated .unproc file, deleting");
            let _ = std::fs::remove_file(path);
            continue;
        }

        let packet = &raw[HEADER_LEN..HEADER_LEN + header.data_len as usize];
        let exporter = SocketAddr::new(
            std::net::IpAddr::V4(Ipv4Addr::from(header.exporter_ip)),
            header.exporter_port,
        );

        // Parse the IPFIX message to discover which template IDs are needed.
        let parsed = match protocol::parse_message(packet, exporter) {
            Ok(m) => m,
            Err(e) => {
                warn!(file = %path.display(), error = %e, "Failed to parse stored IPFIX packet, deleting");
                let _ = std::fs::remove_file(path);
                continue;
            }
        };

        // Collect template IDs needed by data sets in this message.
        let needed: Vec<u16> = parsed
            .sets
            .iter()
            .filter_map(|s| {
                if let SetContents::Data(d) = &s.contents {
                    if d.template_id >= protocol::MIN_DATA_SET_ID {
                        return Some(d.template_id);
                    }
                }
                None
            })
            .collect();

        // Check if ALL needed templates are now available.
        let domain = parsed.header.observation_domain_id;
        let all_present = {
            let sess = session.lock().await;
            needed
                .iter()
                .all(|&tid| sess.get_template(exporter, domain, tid).is_some())
        };

        if all_present {
            // Fully decode and forward.
            let mut msg = parsed;
            {
                let mut sess = session.lock().await;
                decoder::decode_message(&mut msg, packet, &mut sess);
            }
            debug!(
                file = %path.display(),
                "\n{}",
                DisplayMessage(&msg)
            );
            sink.on_message(msg);
            let _ = std::fs::remove_file(path);
            info!(file = %path.display(), "Reprocessed unprocessed IPFIX packet");
            metrics
                .ipfix_unprocessed_reprocessed
                .fetch_add(1, std::sync::atomic::Ordering::Relaxed);
        } else {
            pending_count += 1;
        }
    }

    metrics
        .ipfix_unprocessed_pending
        .store(pending_count, std::sync::atomic::Ordering::Relaxed);

    Ok(())
}
