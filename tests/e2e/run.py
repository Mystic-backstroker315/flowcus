#!/usr/bin/env python3
"""
Flowcus end-to-end test orchestrator.

Runs inside the test-runner Docker container alongside a fresh flowcus instance.
No IPFIX traffic from the host can reach the container — full isolation.

Phases:
  1. Send basic IPv4 flows (template 256) → wait for flush → query
  2. Send extended IPv4 flows (template 257) → wait for flush → query
  3. Send L2/VLAN flows (template 258) + second observation domain → flush → query
  4. Send bulk flows to trigger merges → wait for merge gen ≥ 1 → query
  5. Post-merge stabilization → final query pass

Each phase:
  - Ingests known data
  - Takes a storage snapshot (parts, generations)
  - Runs the full query test suite
  - Records verdicts with storage state context

Artifacts are written to $ARTIFACTS_DIR as JSON files.
"""

import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from ipfix_generator import (
    IpfixSender,
    FlowRecord,
    FieldSpec,
    generate_phase1_flows,
    generate_phase2_flows,
    generate_phase3_flows,
    generate_phase4_bulk_flows,
    IE_INGRESS_INTERFACE,
    IE_EGRESS_INTERFACE,
    IE_FLOW_START_SECONDS,
    IE_FLOW_END_SECONDS,
)
from storage_monitor import StorageMonitor
from query_validator import QueryValidator, build_test_cases, build_consistency_test_cases, execute_query
from histogram_validator import (
    HistogramValidator,
    build_histogram_test_cases,
    build_histogram_consistency_test_cases,
)


# ---------------------------------------------------------------------------
# Config from environment
# ---------------------------------------------------------------------------
API_BASE = os.environ.get("FLOWCUS_API", "http://localhost:2137")
IPFIX_HOST = os.environ.get("FLOWCUS_IPFIX_HOST", "localhost")
IPFIX_PORT = int(os.environ.get("FLOWCUS_IPFIX_PORT", "4739"))
STORAGE_DIR = os.environ.get("FLOWCUS_STORAGE_DIR", "/data/storage")
ARTIFACTS_DIR = os.environ.get("ARTIFACTS_DIR", "/artifacts")

FLOWS_DIR = os.path.join(STORAGE_DIR, "flows")


def wait_for_api(timeout: float = 60.0):
    """Wait until the flowcus API is healthy."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            result = execute_query(API_BASE, "last 1h")
            if result.http_status == 200:
                print(f"  API is ready (status={result.http_status})")
                return
        except Exception:
            pass
        time.sleep(0.5)
    print("  WARNING: API may not be fully ready, proceeding anyway")


def write_artifact(name: str, data: dict):
    """Write a JSON artifact to the artifacts directory."""
    os.makedirs(ARTIFACTS_DIR, exist_ok=True)
    path = os.path.join(ARTIFACTS_DIR, name)
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    print(f"  Artifact written: {path}")


def main():
    print("=" * 72)
    print("FLOWCUS END-TO-END TEST")
    print(f"  API:     {API_BASE}")
    print(f"  IPFIX:   {IPFIX_HOST}:{IPFIX_PORT}")
    print(f"  Storage: {STORAGE_DIR}")
    print(f"  Time:    {datetime.now(timezone.utc).isoformat()}")
    print("=" * 72)

    # --- Setup ---
    sender = IpfixSender(IPFIX_HOST, IPFIX_PORT)
    monitor = StorageMonitor(FLOWS_DIR)
    validator = QueryValidator(API_BASE)
    hist_validator = HistogramValidator(API_BASE)
    all_ingested: list[FlowRecord] = []
    export_time = int(time.time())
    run_metadata = {
        "start_time": datetime.now(timezone.utc).isoformat(),
        "export_time": export_time,
        "api_base": API_BASE,
        "ipfix_target": f"{IPFIX_HOST}:{IPFIX_PORT}",
        "phases": [],
    }

    print("\n--- Waiting for API ---")
    wait_for_api()

    # Initial storage scan
    monitor.scan()
    initial_snapshot = monitor.snapshot()
    print(f"  Initial storage: {initial_snapshot.details['total_parts']} parts")

    # ===================================================================
    # PHASE 1: Basic IPv4 flows
    # ===================================================================
    print("\n" + "=" * 72)
    print("PHASE 1: Basic IPv4 flows (template 256)")
    print("=" * 72)

    phase1_flows = generate_phase1_flows(export_time)
    print(f"  Sending {len(phase1_flows)} flows...")
    sent = sender.send_flows(phase1_flows, export_time)
    all_ingested.extend(sent)
    print(f"  Sent {len(sent)} flows")

    # Wait for flush
    print("  Waiting for parts to appear...")
    time.sleep(3)  # Let the writer flush (flush_interval_secs=1)
    monitor.scan()

    # Take snapshot before querying
    snap = monitor.snapshot()
    storage_state = snap.details
    print(f"  Storage: {storage_state['total_parts']} parts, "
          f"{storage_state['total_rows']} rows, "
          f"max_gen={storage_state['max_generation']}")

    # Run queries
    print("\n  Running query test suite (phase1_post_flush)...")
    test_cases = build_test_cases(all_ingested, export_time, API_BASE)
    verdicts = validator.run_test_suite(test_cases, "phase1_post_flush", storage_state)

    print("\n  Running histogram test suite (phase1_post_flush)...")
    hist_cases = build_histogram_test_cases(all_ingested, export_time, API_BASE)
    hist_verdicts = hist_validator.run_test_suite(hist_cases, "phase1_post_flush", storage_state)

    run_metadata["phases"].append({
        "name": "phase1_post_flush",
        "flows_sent": len(sent),
        "total_ingested": len(all_ingested),
        "storage": storage_state,
        "passed": sum(1 for v in verdicts if v.passed) + sum(1 for v in hist_verdicts if v["passed"]),
        "failed": sum(1 for v in verdicts if not v.passed) + sum(1 for v in hist_verdicts if not v["passed"]),
    })

    # ===================================================================
    # PHASE 2: Extended IPv4 flows (different template)
    # ===================================================================
    print("\n" + "=" * 72)
    print("PHASE 2: Extended IPv4 flows (template 257)")
    print("=" * 72)

    phase2_flows = generate_phase2_flows(export_time)
    print(f"  Sending {len(phase2_flows)} flows...")
    sent = sender.send_flows(phase2_flows, export_time)
    all_ingested.extend(sent)

    time.sleep(3)
    monitor.scan()
    snap = monitor.snapshot()
    storage_state = snap.details
    print(f"  Storage: {storage_state['total_parts']} parts, "
          f"{storage_state['total_rows']} rows")

    print("\n  Running query test suite (phase2_multi_template)...")
    test_cases = build_test_cases(all_ingested, export_time, API_BASE)
    verdicts = validator.run_test_suite(test_cases, "phase2_multi_template", storage_state)

    print("\n  Running histogram test suite (phase2_multi_template)...")
    hist_cases = build_histogram_test_cases(all_ingested, export_time, API_BASE)
    hist_verdicts = hist_validator.run_test_suite(hist_cases, "phase2_multi_template", storage_state)

    run_metadata["phases"].append({
        "name": "phase2_multi_template",
        "flows_sent": len(sent),
        "total_ingested": len(all_ingested),
        "storage": storage_state,
        "passed": sum(1 for v in verdicts if v.passed) + sum(1 for v in hist_verdicts if v["passed"]),
        "failed": sum(1 for v in verdicts if not v.passed) + sum(1 for v in hist_verdicts if not v["passed"]),
    })

    # ===================================================================
    # PHASE 3: L2 flows + second observation domain
    # ===================================================================
    print("\n" + "=" * 72)
    print("PHASE 3: L2/VLAN flows + observation domain 2")
    print("=" * 72)

    phase3_flows = generate_phase3_flows(export_time)
    print(f"  Sending {len(phase3_flows)} flows...")
    sent = sender.send_flows(phase3_flows, export_time)
    all_ingested.extend(sent)

    # Also send an option template (RFC coverage)
    print("  Sending option template (set ID 3)...")
    sender.send_option_template(
        template_id=300,
        scope_fields=[FieldSpec(IE_INGRESS_INTERFACE, 4)],
        option_fields=[
            FieldSpec(IE_EGRESS_INTERFACE, 4),
            FieldSpec(IE_FLOW_START_SECONDS, 4),
            FieldSpec(IE_FLOW_END_SECONDS, 4),
        ],
        domain=1,
        export_time=export_time,
    )

    # Wait longer for domain 2 flows — they may go through the unprocessed
    # pipeline (template arrives after data, reprocessed on scan interval).
    # Test config: unprocessed_scan_interval_secs=10
    time.sleep(5)
    monitor.scan()
    # Verify all phase3 flows arrived (22 total from phases 1-3)
    expected_rows = len(all_ingested)
    actual_rows = monitor.total_rows()
    if actual_rows < expected_rows:
        print(f"  Waiting for unprocessed reprocessing ({actual_rows}/{expected_rows} rows)...")
        time.sleep(12)  # Wait for unprocessed scan interval
        monitor.scan()
    snap = monitor.snapshot()
    storage_state = snap.details
    print(f"  Storage: {storage_state['total_parts']} parts, "
          f"{storage_state['total_rows']} rows")

    print("\n  Running query test suite (phase3_multi_domain)...")
    test_cases = build_test_cases(all_ingested, export_time, API_BASE)
    verdicts = validator.run_test_suite(test_cases, "phase3_multi_domain", storage_state)

    print("\n  Running histogram test suite (phase3_multi_domain)...")
    hist_cases = build_histogram_test_cases(all_ingested, export_time, API_BASE)
    hist_verdicts = hist_validator.run_test_suite(hist_cases, "phase3_multi_domain", storage_state)

    run_metadata["phases"].append({
        "name": "phase3_multi_domain",
        "flows_sent": len(sent),
        "total_ingested": len(all_ingested),
        "storage": storage_state,
        "passed": sum(1 for v in verdicts if v.passed) + sum(1 for v in hist_verdicts if v["passed"]),
        "failed": sum(1 for v in verdicts if not v.passed) + sum(1 for v in hist_verdicts if not v["passed"]),
    })

    # ===================================================================
    # PHASE 4: Bulk ingest to trigger merges
    # ===================================================================
    print("\n" + "=" * 72)
    print("PHASE 4: Bulk flows (trigger merges)")
    print("=" * 72)

    phase4_flows = generate_phase4_bulk_flows(export_time, count=500)
    print(f"  Sending {len(phase4_flows)} flows in batches...")
    # Send in small batches to create many small parts.
    # Pace conservatively to avoid UDP drops from kernel buffer overflow.
    batch_size = 10
    for i in range(0, len(phase4_flows), batch_size):
        batch = phase4_flows[i : i + batch_size]
        sent_batch = sender.send_flows(batch, export_time, max_records_per_set=5)
        all_ingested.extend(sent_batch)
        time.sleep(0.5)  # 500ms between batches to avoid UDP drops

    print(f"  Total sent: {len(all_ingested)} flows (some may be UDP-dropped)")

    # Wait for all flushes to complete
    print("  Waiting for flushes to complete...")
    time.sleep(5)  # Let writer flush all pending data
    monitor.scan()
    snap = monitor.snapshot()
    storage_state_during = snap.details
    gen_summary = monitor.generation_summary()
    stored_rows = storage_state_during["total_rows"]
    print(f"  Storage: {storage_state_during['total_parts']} parts, "
          f"{stored_rows} rows, generations: {gen_summary}")

    # UDP drops are expected under bulk load. Instead of comparing against
    # sent counts, use consistency tests that verify the query engine
    # returns internally-consistent results from whatever data survived.
    udp_dropped = len(all_ingested) - stored_rows
    if udp_dropped > 0:
        print(f"  NOTE: {udp_dropped}/{len(all_ingested)} flows dropped (UDP loss)")

    print("\n  Running consistency test suite (phase4_during_merge)...")
    consistency_cases = build_consistency_test_cases(API_BASE, export_time)
    verdicts = validator.run_test_suite(consistency_cases, "phase4_during_merge", storage_state_during)

    print("\n  Running histogram consistency suite (phase4_during_merge)...")
    hist_consistency = build_histogram_consistency_test_cases(API_BASE, export_time)
    hist_verdicts = hist_validator.run_test_suite(hist_consistency, "phase4_during_merge", storage_state_during)

    run_metadata["phases"].append({
        "name": "phase4_during_merge",
        "flows_sent": len(phase4_flows),
        "total_ingested": len(all_ingested),
        "storage": storage_state_during,
        "generation_summary": gen_summary,
        "passed": sum(1 for v in verdicts if v.passed) + sum(1 for v in hist_verdicts if v["passed"]),
        "failed": sum(1 for v in verdicts if not v.passed) + sum(1 for v in hist_verdicts if not v["passed"]),
    })

    # ===================================================================
    # PHASE 5: Wait for merges to stabilize, then query again
    # ===================================================================
    print("\n" + "=" * 72)
    print("PHASE 5: Post-merge stabilization")
    print("=" * 72)

    print("  Waiting for merge activity (gen >= 1)...")
    merge_happened = monitor.wait_for_generation(min_gen=1, timeout=30)
    if merge_happened:
        print("  Merge detected! Waiting for stabilization...")
        time.sleep(5)  # Let merges settle
    else:
        print("  No merge detected within timeout (may need more data or time)")

    monitor.scan()
    snap = monitor.snapshot()
    storage_state_post = snap.details
    gen_summary = monitor.generation_summary()
    print(f"  Storage: {storage_state_post['total_parts']} parts, "
          f"generations: {gen_summary}")

    print("\n  Running consistency test suite (phase5_post_merge)...")
    consistency_cases = build_consistency_test_cases(API_BASE, export_time)
    verdicts = validator.run_test_suite(consistency_cases, "phase5_post_merge", storage_state_post)

    print("\n  Running histogram consistency suite (phase5_post_merge)...")
    hist_consistency = build_histogram_consistency_test_cases(API_BASE, export_time)
    hist_verdicts = hist_validator.run_test_suite(hist_consistency, "phase5_post_merge", storage_state_post)

    run_metadata["phases"].append({
        "name": "phase5_post_merge",
        "flows_sent": 0,
        "total_ingested": len(all_ingested),
        "storage": storage_state_post,
        "generation_summary": gen_summary,
        "merge_detected": merge_happened,
        "passed": sum(1 for v in verdicts if v.passed) + sum(1 for v in hist_verdicts if v["passed"]),
        "failed": sum(1 for v in verdicts if not v.passed) + sum(1 for v in hist_verdicts if not v["passed"]),
    })

    # ===================================================================
    # Write artifacts
    # ===================================================================
    print("\n" + "=" * 72)
    print("WRITING ARTIFACTS")
    print("=" * 72)

    run_metadata["end_time"] = datetime.now(timezone.utc).isoformat()
    query_summary = validator.summary()
    hist_summary = hist_validator.summary()
    run_metadata["summary"] = {
        "total": query_summary["total"] + hist_summary["total"],
        "passed": query_summary["passed"] + hist_summary["passed"],
        "failed": query_summary["failed"] + hist_summary["failed"],
        "pass_rate": (
            f"{(query_summary['passed'] + hist_summary['passed']) / (query_summary['total'] + hist_summary['total']) * 100:.1f}%"
            if (query_summary["total"] + hist_summary["total"]) > 0 else "N/A"
        ),
        "query": query_summary,
        "histogram": hist_summary,
    }

    # 1. Main test report
    write_artifact("test_report.json", {
        "metadata": run_metadata,
        "verdicts": validator.all_verdicts(),
        "histogram_verdicts": hist_validator.all_verdicts(),
    })

    # 2. Storage timeline
    write_artifact("storage_timeline.json", {
        "events": monitor.get_timeline(),
        "final_snapshot": storage_state_post,
    })

    # 3. Ingested data reference (for other agents to compute expected values)
    ingested_ref = []
    for f in all_ingested:
        ingested_ref.append({
            "src_ip": f.src_ip,
            "dst_ip": f.dst_ip,
            "src_port": f.src_port,
            "dst_port": f.dst_port,
            "protocol": f.protocol,
            "bytes_count": f.bytes_count,
            "packets_count": f.packets_count,
            "tcp_flags": f.tcp_flags,
            "tos": f.tos,
            "vlan_id": f.vlan_id,
            "ingress_if": f.ingress_if,
            "egress_if": f.egress_if,
            "flow_start": f.flow_start,
            "flow_end": f.flow_end,
            "src_mac": f.src_mac,
            "dst_mac": f.dst_mac,
            "export_time": f.export_time,
            "observation_domain": f.observation_domain,
            "template_id": f.template_id,
            "phase": f.phase,
        })
    write_artifact("ingested_flows.json", {
        "total_flows": len(ingested_ref),
        "by_phase": {
            "phase1": sum(1 for f in ingested_ref if f["phase"] == "phase1"),
            "phase2": sum(1 for f in ingested_ref if f["phase"] == "phase2"),
            "phase3": sum(1 for f in ingested_ref if f["phase"] == "phase3"),
            "phase4": sum(1 for f in ingested_ref if f["phase"] == "phase4"),
        },
        "by_template": {
            str(tid): sum(1 for f in ingested_ref if f["template_id"] == tid)
            for tid in {f["template_id"] for f in ingested_ref}
        },
        "by_protocol": {
            str(p): sum(1 for f in ingested_ref if f["protocol"] == p)
            for p in {f["protocol"] for f in ingested_ref}
        },
        "by_domain": {
            str(d): sum(1 for f in ingested_ref if f["observation_domain"] == d)
            for d in {f["observation_domain"] for f in ingested_ref}
        },
        "flows": ingested_ref,
    })

    # 4. Query reference for other agents — what each query should return
    write_artifact("query_reference.json", {
        "description": (
            "Reference for other agents: each entry describes a query, "
            "what it tests, the expected result computed from ingested data, "
            "and how storage state (merges, generations) affected the outcome."
        ),
        "test_cases": [
            {
                "test_name": v.test_name,
                "query": v.query,
                "purpose": _test_purpose(v.test_name),
                "expected": v.expected,
                "actual_by_phase": {},  # Filled below
            }
            for v in validator.verdicts
            if v.phase == "phase5_post_merge"
        ],
        "phase_comparison": _build_phase_comparison(validator.verdicts),
    })

    # ===================================================================
    # Summary
    # ===================================================================
    print("\n" + "=" * 72)
    combined = run_metadata["summary"]
    print(f"RESULTS: {combined['passed']}/{combined['total']} passed "
          f"({combined['pass_rate']})")
    print("-" * 72)
    print("  Query tests:")
    for phase, stats in query_summary["by_phase"].items():
        status = "OK" if stats["failed"] == 0 else "FAIL"
        print(f"    [{status}] {phase}: {stats['passed']}/{stats['total']} passed")
    print("  Histogram tests:")
    for phase, stats in hist_summary["by_phase"].items():
        status = "OK" if stats["failed"] == 0 else "FAIL"
        print(f"    [{status}] {phase}: {stats['passed']}/{stats['total']} passed")
    print("=" * 72)

    sender.close()

    # Exit with failure code if any tests failed
    if combined["failed"] > 0:
        print(f"\n{combined['failed']} test(s) FAILED")
        sys.exit(1)
    else:
        print("\nAll tests PASSED")
        sys.exit(0)


def _test_purpose(test_name: str) -> str:
    """Human-readable purpose for each test case."""
    purposes = {
        "total_flow_count": "Verify all ingested flows are queryable (no data loss)",
        "filter_proto_tcp": "Filter by protocol identifier (TCP=6)",
        "filter_proto_udp": "Filter by protocol identifier (UDP=17)",
        "filter_dport_80": "Filter by single destination port",
        "filter_dport_80_443": "Filter by destination port list",
        "filter_src_10_0_0_0_8": "Filter by source CIDR (10.0.0.0/8)",
        "filter_src_cidr_and_tcp": "Combined filter: CIDR AND protocol",
        "filter_bytes_gt_1000": "Numeric comparison filter on byte count",
        "filter_not_icmp": "Negation filter (NOT proto ICMP)",
        "filter_dport_80_or_22": "OR filter on destination ports",
        "filter_sport_range": "Source port range filter (1024-65535)",
        "filter_src_exact": "Exact source IP match",
        "filter_src_not_192_168": "Negated CIDR filter on source",
        "limit_5": "LIMIT clause restricts row count",
        "aggregate_sum_bytes": "Aggregation: sum(bytes) across all flows",
        "aggregate_count": "Aggregation: count() all flows",
        "group_by_protocol": "GROUP BY protocol with count()",
        "top_3_by_bytes": "TOP N aggregation",
        "filter_tcp_sum_bytes": "Combined filter + aggregation",
        "stats_parts_scanned": "Sanity: query engine scans at least one part",
    }
    return purposes.get(test_name, "")


def _build_phase_comparison(verdicts: list) -> dict:
    """
    Build a comparison showing how the same query performed across phases.

    This is the key artifact for understanding merge/ingestion interaction:
    if a query returns different counts in phase4_during_merge vs
    phase5_post_merge, that indicates a potential race condition.
    """
    by_test: dict[str, dict] = {}
    for v in verdicts:
        if v.test_name not in by_test:
            by_test[v.test_name] = {}
        by_test[v.test_name][v.phase] = {
            "passed": v.passed,
            "expected": v.expected,
            "actual": v.actual,
            "storage_parts": v.storage_state.get("total_parts", "?"),
            "storage_max_gen": v.storage_state.get("max_generation", "?"),
        }

    # Flag consistency issues
    issues = []
    for test_name, phases in by_test.items():
        # Compare phase4_during_merge vs phase5_post_merge
        during = phases.get("phase4_during_merge", {})
        after = phases.get("phase5_post_merge", {})
        if during and after:
            if during.get("passed") != after.get("passed"):
                issues.append({
                    "test": test_name,
                    "issue": "Result changed between during-merge and post-merge",
                    "during_merge": during,
                    "post_merge": after,
                })

    return {
        "by_test": by_test,
        "consistency_issues": issues,
        "has_issues": len(issues) > 0,
    }


if __name__ == "__main__":
    main()
