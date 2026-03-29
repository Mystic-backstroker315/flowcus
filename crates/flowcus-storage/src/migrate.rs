//! Part format migration.
//!
//! Detects old-format parts on startup and migrates them to the current format.
//! Migration is non-blocking — runs in background, parts remain queryable during
//! migration (the executor handles both old and new formats).
//!
//! # Version history
//!
//! - **v0** (legacy, 4-segment dir name): `{gen}_{min}_{max}_{seq}`
//!   No `.stats` files. Column data, marks, and bloom filters only.
//!
//! - **v1** (current, 5-segment dir name): `1_{gen}_{min}_{max}_{seq}`
//!   Adds `.stats` files with per-granule pre-computed aggregates (sum, count, min, max).

use std::path::{Path, PathBuf};

use tracing::{debug, info, warn};

use crate::decode;
use crate::granule;
use crate::part;
use crate::schema::StorageType;

/// Scan the table directory for v0 parts and migrate them to v1.
///
/// Migration adds `.stats` files and renames the directory to include
/// the format version prefix. Safe to call multiple times — already-migrated
/// parts are skipped.
pub fn migrate_parts(table_base: &Path, granule_size: usize, bloom_bits: usize) {
    let v0_parts = find_v0_parts(table_base);
    if v0_parts.is_empty() {
        return;
    }

    info!(
        count = v0_parts.len(),
        "Found v0 parts to migrate, adding .stats files"
    );

    let mut migrated = 0usize;
    let mut failed = 0usize;

    for part_dir in &v0_parts {
        match migrate_v0_to_v1(part_dir, granule_size, bloom_bits) {
            Ok(new_path) => {
                migrated += 1;
                debug!(
                    from = %part_dir.display(),
                    to = %new_path.display(),
                    "Part migrated v0 → v1"
                );
            }
            Err(e) => {
                failed += 1;
                warn!(
                    error = %e,
                    part = %part_dir.display(),
                    "Failed to migrate part, will retry on next startup"
                );
            }
        }
    }

    info!(migrated, failed, "Part migration complete");
}

/// Find all v0 (legacy format) part directories under the table base.
fn find_v0_parts(table_base: &Path) -> Vec<PathBuf> {
    let mut v0_parts = Vec::new();
    walk_for_v0(table_base, 0, &mut v0_parts);
    v0_parts
}

fn walk_for_v0(dir: &Path, depth: u8, out: &mut Vec<PathBuf>) {
    let Ok(entries) = std::fs::read_dir(dir) else {
        return;
    };

    for entry in entries.flatten() {
        if !entry.file_type().map(|ft| ft.is_dir()).unwrap_or(false) {
            continue;
        }

        if depth < 4 {
            walk_for_v0(&entry.path(), depth + 1, out);
        } else {
            let name = match entry.file_name().to_str() {
                Some(n) => n.to_string(),
                None => continue,
            };
            // v0 parts have exactly 4 segments (no version prefix)
            if let Some((version, ..)) = part::parse_part_dir_name_versioned(&name) {
                if version == 0 {
                    out.push(entry.path());
                }
            }
        }
    }
}

/// Migrate a single v0 part to v1: compute and write `.stats` files,
/// then rename the directory to include the format version.
fn migrate_v0_to_v1(
    part_dir: &Path,
    granule_size: usize,
    _bloom_bits: usize,
) -> std::io::Result<PathBuf> {
    // Read schema to know column types
    let schema_path = part_dir.join("schema.bin");
    let schema = part::read_schema_bin(&schema_path)?;

    let col_types: std::collections::HashMap<String, StorageType> = schema
        .columns
        .iter()
        .map(|c| (c.name.clone(), c.storage_type))
        .collect();

    // List columns from the columns/ directory
    let columns = part::list_columns(part_dir)?;

    // For each column, compute and write .stats
    for col_name in &columns {
        let stats_path = part_dir.join("columns").join(format!("{col_name}.stats"));
        if stats_path.exists() {
            continue; // Already has stats (partial migration?)
        }

        let col_path = part_dir.join("columns").join(format!("{col_name}.col"));
        if !col_path.exists() {
            continue;
        }

        let st = col_types.get(col_name).copied().unwrap_or(StorageType::U32);

        // Decode the column to compute stats
        let buf = decode::decode_column(&col_path, st)?;
        let row_count = buf.row_count();
        let num_granules = row_count.div_ceil(granule_size.max(1));

        let mut stats = Vec::with_capacity(num_granules);
        for g in 0..num_granules {
            let row_start = g * granule_size;
            let row_end = ((g + 1) * granule_size).min(row_count);
            stats.push(granule::compute_granule_stats(&buf, row_start, row_end));
        }

        granule::write_stats(&stats_path, &stats, st)?;
    }

    // Rename directory: {gen}_{min}_{max}_{seq} → 1_{gen}_{min}_{max}_{seq}
    let dir_name = part_dir.file_name().and_then(|n| n.to_str()).unwrap_or("");
    let new_name = format!("{}_{dir_name}", part::PART_FORMAT_VERSION);
    let new_path = part_dir.with_file_name(new_name);

    std::fs::rename(part_dir, &new_path)?;

    Ok(new_path)
}

/// Start background migration on a tokio blocking thread.
pub fn start_background_migration(table_base: PathBuf, granule_size: usize, bloom_bits: usize) {
    tokio::task::spawn_blocking(move || {
        migrate_parts(&table_base, granule_size, bloom_bits);
    });
}
