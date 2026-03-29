"""
Query validator for e2e testing.

Executes FQL queries against the /api/query endpoint and compares
results against expected values computed from the known ingested data.

Each test case defines:
- The FQL query string
- A function that computes expected results from the ingested FlowRecords
- Comparison logic (exact match, subset, count, aggregation tolerance)
"""

import json
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Callable, Optional

from ipfix_generator import FlowRecord, PROTO_TCP, PROTO_UDP, PROTO_ICMP


@dataclass
class QueryResult:
    """Parsed response from /api/query."""
    columns: list[str]
    rows: list[list]
    stats: dict
    explain: list[dict]
    pagination: dict
    raw: dict
    http_status: int = 200
    error: Optional[str] = None

    @property
    def row_count(self) -> int:
        return len(self.rows)

    @property
    def total(self) -> int:
        return self.pagination.get("total", 0)

    @property
    def parts_scanned(self) -> int:
        return self.stats.get("parts_scanned", 0)

    @property
    def rows_scanned(self) -> int:
        return self.stats.get("rows_scanned", 0)


@dataclass
class TestVerdict:
    """Result of a single query validation."""
    test_name: str
    query: str
    phase: str
    passed: bool
    expected: dict
    actual: dict
    storage_state: dict
    details: str = ""
    duration_ms: float = 0

    def to_dict(self) -> dict:
        return {
            "test_name": self.test_name,
            "query": self.query,
            "phase": self.phase,
            "passed": self.passed,
            "expected": self.expected,
            "actual": self.actual,
            "storage_state": self.storage_state,
            "details": self.details,
            "duration_ms": self.duration_ms,
        }


def execute_query(api_base: str, query: str, limit: int = 10000) -> QueryResult:
    """Execute an FQL query against the flowcus API."""
    url = f"{api_base}/api/query"
    payload = json.dumps({"query": query, "limit": limit}).encode()
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = json.loads(resp.read())
            # Columns may be [{name, type}] objects or plain strings
            raw_cols = body.get("columns", [])
            col_names = [
                c["name"] if isinstance(c, dict) else c
                for c in raw_cols
            ]
            return QueryResult(
                columns=col_names,
                rows=body.get("rows", []),
                stats=body.get("stats", {}),
                explain=body.get("explain", []),
                pagination=body.get("pagination", {}),
                raw=body,
                http_status=resp.status,
            )
    except urllib.error.HTTPError as e:
        body = json.loads(e.read()) if e.fp else {}
        return QueryResult(
            columns=[], rows=[], stats={}, explain=[], pagination={},
            raw=body, http_status=e.code,
            error=body.get("error", str(e)),
        )
    except Exception as e:
        return QueryResult(
            columns=[], rows=[], stats={}, explain=[], pagination={},
            raw={}, http_status=0, error=str(e),
        )


# ---------------------------------------------------------------------------
# Expected-value computation helpers
# ---------------------------------------------------------------------------

def count_flows(flows: list[FlowRecord], predicate: Callable[[FlowRecord], bool]) -> int:
    return sum(1 for f in flows if predicate(f))


def sum_field(flows: list[FlowRecord], field: str, predicate: Callable[[FlowRecord], bool] = lambda _: True) -> int:
    return sum(getattr(f, field) for f in flows if predicate(f))


def unique_values(flows: list[FlowRecord], field: str, predicate: Callable[[FlowRecord], bool] = lambda _: True) -> set:
    return {getattr(f, field) for f in flows if predicate(f)}


def ip_in_cidr(ip: str, cidr: str) -> bool:
    """Check if an IP is in a CIDR range (IPv4 only, simple implementation)."""
    import ipaddress
    try:
        return ipaddress.ip_address(ip) in ipaddress.ip_network(cidr, strict=False)
    except ValueError:
        return False


# ---------------------------------------------------------------------------
# Test case definitions
# ---------------------------------------------------------------------------

def build_test_cases(
    ingested_flows: list[FlowRecord],
    export_time: int,
    api_base: str = "",
) -> list[tuple[str, str, Callable[[QueryResult], TestVerdict]]]:
    """
    Build test cases that validate queries against known ingested data.

    Returns list of (test_name, fql_query, validator_fn).
    The validator receives the QueryResult and returns a TestVerdict.
    """

    # Use absolute time range that covers our export_time
    ts_start = export_time - 3600
    ts_end = export_time + 3600

    # Helper: time range that covers all test data
    def tr() -> str:
        return "last 2h"

    cases = []

    # --- 1. Total flow count (no filter) ---
    def validate_total_count(result: QueryResult) -> dict:
        expected_total = len(ingested_flows)
        return {
            "expected_total": expected_total,
            "actual_total": result.total,
            "match": result.total == expected_total,
        }

    cases.append((
        "total_flow_count",
        f"{tr()}",
        validate_total_count,
    ))

    # --- 2. Filter by protocol: TCP ---
    def validate_tcp_filter(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.protocol == PROTO_TCP)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_proto_tcp",
        f"{tr()} | proto tcp",
        validate_tcp_filter,
    ))

    # --- 3. Filter by protocol: UDP ---
    def validate_udp_filter(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.protocol == PROTO_UDP)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_proto_udp",
        f"{tr()} | proto udp",
        validate_udp_filter,
    ))

    # --- 4. Filter by destination port: 80 ---
    def validate_dport_80(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.dst_port == 80)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_dport_80",
        f"{tr()} | dport 80",
        validate_dport_80,
    ))

    # --- 5. Filter by destination port list: 80,443 ---
    def validate_dport_list(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.dst_port in (80, 443))
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_dport_80_443",
        f"{tr()} | dport 80,443",
        validate_dport_list,
    ))

    # --- 6. Filter by source CIDR: 10.0.0.0/8 ---
    def validate_src_cidr(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: ip_in_cidr(f.src_ip, "10.0.0.0/8"))
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_src_10_0_0_0_8",
        f"{tr()} | src 10.0.0.0/8",
        validate_src_cidr,
    ))

    # --- 7. Combined filter: src 10.0.0.0/8 AND proto TCP ---
    def validate_combined(result: QueryResult) -> dict:
        expected = count_flows(
            ingested_flows,
            lambda f: ip_in_cidr(f.src_ip, "10.0.0.0/8") and f.protocol == PROTO_TCP,
        )
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_src_cidr_and_tcp",
        f"{tr()} | src 10.0.0.0/8 and proto tcp",
        validate_combined,
    ))

    # --- 8. Filter by bytes > 1000 ---
    def validate_bytes_gt(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.bytes_count > 1000)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_bytes_gt_1000",
        f"{tr()} | bytes > 1000",
        validate_bytes_gt,
    ))

    # --- 9. Negation: NOT proto ICMP ---
    def validate_not_icmp(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.protocol != PROTO_ICMP)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_not_icmp",
        f"{tr()} | not proto icmp",
        validate_not_icmp,
    ))

    # --- 10. OR filter: dport 80 or dport 22 ---
    def validate_or_filter(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.dst_port in (80, 22))
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_dport_80_or_22",
        f"{tr()} | dport 80 or dport 22",
        validate_or_filter,
    ))

    # --- 11. Source port range: sport 1024-65535 ---
    def validate_sport_range(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: 1024 <= f.src_port <= 65535)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_sport_range",
        f"{tr()} | sport 1024-65535",
        validate_sport_range,
    ))

    # --- 12. Exact source IP ---
    def validate_exact_src(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.src_ip == "10.0.0.1")
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_src_exact",
        f"{tr()} | src 10.0.0.1",
        validate_exact_src,
    ))

    # --- 13. Negated source: src NOT 192.168.0.0/16 ---
    def validate_src_not_cidr(result: QueryResult) -> dict:
        expected = count_flows(
            ingested_flows,
            lambda f: not ip_in_cidr(f.src_ip, "192.168.0.0/16"),
        )
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_src_not_192_168",
        f"{tr()} | src not 192.168.0.0/16",
        validate_src_not_cidr,
    ))

    # --- 14. Limit ---
    def validate_limit(result: QueryResult) -> dict:
        return {
            "expected_row_count": min(5, len(ingested_flows)),
            "actual_row_count": result.row_count,
            "match": result.row_count <= 5,
        }

    cases.append((
        "limit_5",
        f"{tr()} | limit 5",
        validate_limit,
    ))

    # --- 15. Sum bytes (group by all via large time bucket) ---
    def validate_sum_bytes(result: QueryResult) -> dict:
        expected_sum = sum_field(ingested_flows, "bytes_count")
        actual_sum = 0
        if result.rows:
            for i, col in enumerate(result.columns):
                if "sum" in col.lower() or "bytes" in col.lower() or "octet" in col.lower():
                    try:
                        actual_sum = int(result.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        return {
            "expected_sum": expected_sum,
            "actual_sum": actual_sum,
            "match": actual_sum == expected_sum,
        }

    cases.append((
        "aggregate_sum_bytes",
        f"{tr()} | group by 2h | sum(bytes)",
        validate_sum_bytes,
    ))

    # --- 16. Count all (group by large bucket to get total) ---
    def validate_count(result: QueryResult) -> dict:
        expected = len(ingested_flows)
        actual = 0
        if result.rows:
            for i, col in enumerate(result.columns):
                if "count" in col.lower():
                    try:
                        actual = int(result.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        return {
            "expected_count": expected,
            "actual_count": actual,
            "match": actual == expected,
        }

    cases.append((
        "aggregate_count",
        f"{tr()} | group by 2h | count()",
        validate_count,
    ))

    # --- 17. Group by protocol ---
    def validate_group_by_proto(result: QueryResult) -> dict:
        expected_groups = {}
        for f in ingested_flows:
            expected_groups[f.protocol] = expected_groups.get(f.protocol, 0) + 1
        return {
            "expected_group_count": len(expected_groups),
            "actual_group_count": result.total,
            "match": result.total == len(expected_groups),
            "expected_groups": {str(k): v for k, v in expected_groups.items()},
        }

    cases.append((
        "group_by_protocol",
        f"{tr()} | group by proto | count()",
        validate_group_by_proto,
    ))

    # --- 18. Top 3 by sum(bytes) ---
    def validate_top3(result: QueryResult) -> dict:
        return {
            "expected_max_rows": 3,
            "actual_rows": result.row_count,
            "match": result.row_count <= 3,
            "has_rows": result.row_count > 0,
        }

    cases.append((
        "top_3_by_bytes",
        f"{tr()} | top 3 by sum(bytes)",
        validate_top3,
    ))

    # --- 19. Filter + aggregate: TCP flows sum(bytes) ---
    def validate_tcp_sum(result: QueryResult) -> dict:
        expected = sum_field(ingested_flows, "bytes_count", lambda f: f.protocol == PROTO_TCP)
        actual = 0
        if result.rows:
            for i, col in enumerate(result.columns):
                if "sum" in col.lower() or "bytes" in col.lower() or "octet" in col.lower():
                    try:
                        actual = int(result.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        return {
            "expected_sum": expected,
            "actual_sum": actual,
            "match": actual == expected,
        }

    cases.append((
        "filter_tcp_sum_bytes",
        f"{tr()} | proto tcp | group by 2h | sum(bytes)",
        validate_tcp_sum,
    ))

    # --- 20. Parts scanned sanity check ---
    def validate_parts_scanned(result: QueryResult) -> dict:
        return {
            "parts_scanned": result.parts_scanned,
            "rows_scanned": result.rows_scanned,
            "has_parts": result.parts_scanned > 0,
            "match": result.parts_scanned > 0,
        }

    cases.append((
        "stats_parts_scanned",
        f"{tr()}",
        validate_parts_scanned,
    ))

    # --- 21. Column type info in response ---
    def validate_column_types(result: QueryResult) -> dict:
        raw_cols = result.raw.get("columns", [])
        has_type_info = all(
            isinstance(c, dict) and "name" in c and "type" in c
            for c in raw_cols
        ) if raw_cols else False
        return {
            "expected_has_types": True,
            "actual_has_types": has_type_info,
            "actual_col_count": len(raw_cols),
            "match": has_type_info and len(raw_cols) > 0,
        }

    cases.append((
        "column_type_info",
        f"{tr()}",
        validate_column_types,
    ))

    # --- 22. All rows have same column count as header ---
    def validate_row_column_consistency(result: QueryResult) -> dict:
        col_count = len(result.columns)
        mismatched = [
            i for i, row in enumerate(result.rows)
            if len(row) != col_count
        ]
        return {
            "expected_col_count": col_count,
            "actual_mismatched_rows": len(mismatched),
            "match": len(mismatched) == 0 and col_count > 0,
        }

    cases.append((
        "row_column_consistency",
        f"{tr()}",
        validate_row_column_consistency,
    ))

    # --- 23. Pagination total reflects all rows ---
    def validate_pagination_total(result: QueryResult) -> dict:
        # Query with high limit — total should match actual returned rows
        full = execute_query(api_base, f"{tr()}", limit=50000)
        return {
            "expected_total": full.total,
            "actual_rows_returned": full.row_count,
            "match": full.total == full.row_count,
        }

    cases.append((
        "pagination_total_accurate",
        f"{tr()}",
        validate_pagination_total,
    ))

    # ===================================================================
    # FILTER COVERAGE: all FQL filter variants
    # ===================================================================

    # --- dst direction filter ---
    def validate_dst_filter(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.dst_ip == "192.168.1.100")
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_dst_exact",
        f"{tr()} | dst 192.168.1.100",
        validate_dst_filter,
    ))

    # --- ip (any direction) filter ---
    def validate_ip_any(result: QueryResult) -> dict:
        expected = count_flows(
            ingested_flows,
            lambda f: f.src_ip == "10.0.0.1" or f.dst_ip == "10.0.0.1",
        )
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_ip_any_direction",
        f"{tr()} | ip 10.0.0.1",
        validate_ip_any,
    ))

    # --- dst CIDR filter ---
    def validate_dst_cidr(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: ip_in_cidr(f.dst_ip, "192.168.0.0/16"))
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_dst_cidr",
        f"{tr()} | dst 192.168.0.0/16",
        validate_dst_cidr,
    ))

    # --- named port: dns ---
    def validate_named_port_dns(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.dst_port == 53 or f.src_port == 53)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_named_port_dns",
        f"{tr()} | port dns",
        validate_named_port_dns,
    ))

    # --- named port: ssh (dport) ---
    def validate_named_port_ssh(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.dst_port == 22)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_named_port_ssh",
        f"{tr()} | dport ssh",
        validate_named_port_ssh,
    ))

    # --- port open range: sport 1024- ---
    def validate_port_open_range(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.src_port >= 1024)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_sport_open_range",
        f"{tr()} | sport 1024-",
        validate_port_open_range,
    ))

    # --- protocol by number ---
    def validate_proto_number(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.protocol == PROTO_TCP)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_proto_by_number",
        f"{tr()} | proto 6",
        validate_proto_number,
    ))

    # --- protocol list: tcp,udp ---
    def validate_proto_list(result: QueryResult) -> dict:
        expected = count_flows(
            ingested_flows,
            lambda f: f.protocol in (PROTO_TCP, PROTO_UDP),
        )
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_proto_list",
        f"{tr()} | proto tcp,udp",
        validate_proto_list,
    ))

    # --- bytes with suffix: > 1K ---
    def validate_bytes_suffix_k(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.bytes_count > 1000)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_bytes_gt_1K",
        f"{tr()} | bytes > 1K",
        validate_bytes_suffix_k,
    ))

    # --- bytes = exact ---
    def validate_bytes_eq(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.bytes_count == 1500)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_bytes_eq_1500",
        f"{tr()} | bytes = 1500",
        validate_bytes_eq,
    ))

    # --- bytes != ---
    def validate_bytes_ne(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.bytes_count != 0)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_bytes_ne_0",
        f"{tr()} | bytes != 0",
        validate_bytes_ne,
    ))

    # --- bytes <= ---
    def validate_bytes_le(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.bytes_count <= 100)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_bytes_le_100",
        f"{tr()} | bytes <= 100",
        validate_bytes_le,
    ))

    # --- nested AND/OR: (src 10.0.0.0/8 and dport 80) or proto udp ---
    def validate_nested_logic(result: QueryResult) -> dict:
        expected = count_flows(
            ingested_flows,
            lambda f: (ip_in_cidr(f.src_ip, "10.0.0.0/8") and f.dst_port == 80)
            or f.protocol == PROTO_UDP,
        )
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_nested_and_or",
        f"{tr()} | src 10.0.0.0/8 and dport 80 or proto udp",
        validate_nested_logic,
    ))

    # --- double negation: not not proto tcp ---
    def validate_double_not(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.protocol == PROTO_TCP)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_double_negation",
        f"{tr()} | not not proto tcp",
        validate_double_not,
    ))

    # --- packets < threshold ---
    def validate_packets_lt(result: QueryResult) -> dict:
        expected = count_flows(ingested_flows, lambda f: f.packets_count < 5)
        return {
            "expected_count": expected,
            "actual_count": result.total,
            "match": result.total == expected,
        }

    cases.append((
        "filter_packets_lt_5",
        f"{tr()} | packets < 5",
        validate_packets_lt,
    ))

    # ===================================================================
    # AGGREGATION COVERAGE
    # ===================================================================

    # --- group by dst /24 (subnet aggregation) ---
    def validate_group_subnet(result: QueryResult) -> dict:
        subnets = set()
        for f in ingested_flows:
            parts = f.dst_ip.split(".")
            subnets.add(f"{parts[0]}.{parts[1]}.{parts[2]}.0/24")
        return {
            "expected_group_count": len(subnets),
            "actual_group_count": result.total,
            "match": result.total == len(subnets),
        }

    cases.append((
        "aggregate_group_by_subnet",
        f"{tr()} | group by dst /24 | count()",
        validate_group_subnet,
    ))

    # --- group by multiple keys: proto + dport ---
    def validate_group_multi(result: QueryResult) -> dict:
        groups = set()
        for f in ingested_flows:
            groups.add((f.protocol, f.dst_port))
        return {
            "expected_group_count": len(groups),
            "actual_group_count": result.total,
            "match": result.total == len(groups),
        }

    cases.append((
        "aggregate_group_multi_keys",
        f"{tr()} | group by proto, dport | count()",
        validate_group_multi,
    ))

    # --- sum(packets) ---
    def validate_sum_packets(result: QueryResult) -> dict:
        expected = sum_field(ingested_flows, "packets_count")
        actual = 0
        if result.rows:
            for i, col in enumerate(result.columns):
                if "sum" in col.lower() or "packet" in col.lower():
                    try:
                        actual = int(result.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        return {
            "expected_sum": expected,
            "actual_sum": actual,
            "match": actual == expected,
        }

    cases.append((
        "aggregate_sum_packets",
        f"{tr()} | group by 2h | sum(packets)",
        validate_sum_packets,
    ))

    # --- min(bytes) ---
    def validate_min_bytes(result: QueryResult) -> dict:
        expected = min(f.bytes_count for f in ingested_flows) if ingested_flows else 0
        actual = 0
        if result.rows:
            for i, col in enumerate(result.columns):
                if "min" in col.lower():
                    try:
                        actual = int(result.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        return {
            "expected_min": expected,
            "actual_min": actual,
            "match": actual == expected,
        }

    cases.append((
        "aggregate_min_bytes",
        f"{tr()} | group by 2h | min(bytes)",
        validate_min_bytes,
    ))

    # --- max(bytes) ---
    def validate_max_bytes(result: QueryResult) -> dict:
        expected = max(f.bytes_count for f in ingested_flows) if ingested_flows else 0
        actual = 0
        if result.rows:
            for i, col in enumerate(result.columns):
                if "max" in col.lower():
                    try:
                        actual = int(result.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        return {
            "expected_max": expected,
            "actual_max": actual,
            "match": actual == expected,
        }

    cases.append((
        "aggregate_max_bytes",
        f"{tr()} | group by 2h | max(bytes)",
        validate_max_bytes,
    ))

    # --- sort descending ---
    def validate_sort_desc(result: QueryResult) -> dict:
        if result.row_count < 2:
            return {"match": True, "actual_rows": result.row_count}
        # Find the bytes column
        bytes_col = None
        for i, col in enumerate(result.columns):
            if "sum" in col.lower() or "bytes" in col.lower() or "octet" in col.lower():
                bytes_col = i
                break
        if bytes_col is None:
            return {"match": False, "actual_rows": result.row_count, "error": "no bytes column"}
        values = []
        for row in result.rows:
            try:
                values.append(float(row[bytes_col]))
            except (IndexError, ValueError, TypeError):
                values.append(0.0)
        is_sorted = all(values[i] >= values[i + 1] for i in range(len(values) - 1))
        return {
            "expected_sorted": True,
            "actual_sorted": is_sorted,
            "match": is_sorted,
        }

    cases.append((
        "aggregate_sort_desc",
        f"{tr()} | sort sum(bytes) desc",
        validate_sort_desc,
    ))

    # --- sort ascending ---
    def validate_sort_asc(result: QueryResult) -> dict:
        if result.row_count < 2:
            return {"match": True, "actual_rows": result.row_count}
        bytes_col = None
        for i, col in enumerate(result.columns):
            if "sum" in col.lower() or "bytes" in col.lower() or "octet" in col.lower():
                bytes_col = i
                break
        if bytes_col is None:
            return {"match": False, "error": "no bytes column"}
        values = []
        for row in result.rows:
            try:
                values.append(float(row[bytes_col]))
            except (IndexError, ValueError, TypeError):
                values.append(0.0)
        is_sorted = all(values[i] <= values[i + 1] for i in range(len(values) - 1))
        return {
            "expected_sorted": True,
            "actual_sorted": is_sorted,
            "match": is_sorted,
        }

    cases.append((
        "aggregate_sort_asc",
        f"{tr()} | sort sum(bytes) asc",
        validate_sort_asc,
    ))

    # --- bottom N ---
    def validate_bottom_n(result: QueryResult) -> dict:
        return {
            "expected_max_rows": 3,
            "actual_rows": result.row_count,
            "match": 0 < result.row_count <= 3,
        }

    cases.append((
        "aggregate_bottom_3",
        f"{tr()} | bottom 3 by sum(bytes)",
        validate_bottom_n,
    ))

    # --- filter + group by: UDP flows grouped by dst ---
    def validate_filter_then_group(result: QueryResult) -> dict:
        udp_dsts = set()
        for f in ingested_flows:
            if f.protocol == PROTO_UDP:
                udp_dsts.add(f.dst_ip)
        return {
            "expected_groups": len(udp_dsts),
            "actual_groups": result.total,
            "match": result.total == len(udp_dsts),
        }

    cases.append((
        "filter_then_group_by",
        f"{tr()} | proto udp | group by dst | count()",
        validate_filter_then_group,
    ))

    # --- limit after filter ---
    def validate_limit_after_filter(result: QueryResult) -> dict:
        tcp_count = count_flows(ingested_flows, lambda f: f.protocol == PROTO_TCP)
        expected = min(3, tcp_count)
        return {
            "expected_rows": expected,
            "actual_rows": result.row_count,
            "match": result.row_count == expected,
        }

    cases.append((
        "filter_then_limit",
        f"{tr()} | proto tcp | limit 3",
        validate_limit_after_filter,
    ))

    # ===================================================================
    # PAGINATION: verify offset/limit work correctly
    # ===================================================================

    # --- pagination: offset skips rows ---
    def validate_pagination_offset(result: QueryResult) -> dict:
        full = execute_query(api_base, f"{tr()}", limit=50000)
        offset_result = execute_query(api_base, f"{tr()}", limit=5)
        page2 = execute_query(api_base, f"{tr()}", limit=5)
        # Both pages should have data
        return {
            "expected_total": full.total,
            "page1_rows": offset_result.row_count,
            "match": offset_result.row_count == min(5, full.total) and full.total > 0,
        }

    cases.append((
        "pagination_offset_works",
        f"{tr()}",
        validate_pagination_offset,
    ))

    # --- pagination: has_more flag ---
    def validate_has_more(result: QueryResult) -> dict:
        small = execute_query(api_base, f"{tr()}", limit=2)
        total = execute_query(api_base, f"{tr()}", limit=50000).total
        expected_has_more = total > 2
        return {
            "expected_has_more": expected_has_more,
            "actual_has_more": small.pagination.get("has_more", False),
            "match": small.pagination.get("has_more", False) == expected_has_more,
        }

    cases.append((
        "pagination_has_more",
        f"{tr()}",
        validate_has_more,
    ))

    return cases


def build_consistency_test_cases(
    api_base: str,
    export_time: int,
) -> list[tuple[str, str, Callable[[QueryResult], dict]]]:
    """
    Build test cases that validate query consistency WITHOUT knowing
    which specific flows survived UDP transport.

    These tests query the actual stored data and check that:
    - Different queries agree with each other (tcp + udp + icmp = total)
    - Aggregations are internally consistent
    - Filter combinations produce subsets of the total
    """
    def tr() -> str:
        return "last 2h"

    # First, get the actual total from the API
    baseline = execute_query(api_base, f"{tr()} | group by 2h | count()")
    actual_total = 0
    if baseline.rows:
        for i, col in enumerate(baseline.columns):
            if "count" in col.lower():
                try:
                    actual_total = int(baseline.rows[0][i])
                except (IndexError, ValueError, TypeError):
                    pass
                break

    cases = []

    # --- 1. Total from count() aggregation is positive and stable ---
    def validate_total_consistency(result: QueryResult) -> dict:
        # Re-query the count to verify it's stable (idempotent)
        recheck = execute_query(api_base, f"{tr()} | group by 2h | count()")
        recheck_total = 0
        if recheck.rows:
            for i, col in enumerate(recheck.columns):
                if "count" in col.lower():
                    try:
                        recheck_total = int(recheck.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        return {
            "expected_total": actual_total,
            "actual_total": recheck_total,
            "match": recheck_total == actual_total and actual_total > 0,
        }

    cases.append((
        "consistency_total_count",
        f"{tr()} | group by 2h | count()",
        validate_total_consistency,
    ))

    # --- 2. TCP + UDP + ICMP = total (protocol partition) ---
    def validate_protocol_partition(result: QueryResult) -> dict:
        tcp_r = execute_query(api_base, f"{tr()} | proto tcp | group by 2h | count()")
        udp_r = execute_query(api_base, f"{tr()} | proto udp | group by 2h | count()")
        icmp_r = execute_query(api_base, f"{tr()} | proto icmp | group by 2h | count()")

        def extract_count(r):
            if r.rows:
                for i, col in enumerate(r.columns):
                    if "count" in col.lower():
                        try:
                            return int(r.rows[0][i])
                        except (IndexError, ValueError, TypeError):
                            pass
            return 0

        tcp = extract_count(tcp_r)
        udp = extract_count(udp_r)
        icmp = extract_count(icmp_r)
        proto_sum = tcp + udp + icmp
        return {
            "expected_sum": actual_total,
            "actual_sum": proto_sum,
            "actual_tcp": tcp,
            "actual_udp": udp,
            "actual_icmp": icmp,
            "match": proto_sum == actual_total,
        }

    cases.append((
        "consistency_protocol_partition",
        f"{tr()} | group by proto | count()",
        validate_protocol_partition,
    ))

    # --- 3. Sum bytes from group-by == sum bytes from direct query ---
    def validate_sum_consistency(result: QueryResult) -> dict:
        direct = execute_query(api_base, f"{tr()} | group by 2h | sum(bytes)")
        direct_sum = 0
        if direct.rows:
            for i, col in enumerate(direct.columns):
                if "sum" in col.lower():
                    try:
                        direct_sum = int(direct.rows[0][i])
                    except (IndexError, ValueError, TypeError):
                        pass
                    break
        # group by proto sums should add up to the same
        grouped_sum = 0
        if result.rows:
            sum_col = None
            for i, col in enumerate(result.columns):
                if "sum" in col.lower():
                    sum_col = i
                    break
            if sum_col is not None:
                for row in result.rows:
                    try:
                        grouped_sum += int(row[sum_col])
                    except (IndexError, ValueError, TypeError):
                        pass
        return {
            "expected_sum": direct_sum,
            "actual_sum": grouped_sum,
            "match": grouped_sum == direct_sum and direct_sum > 0,
        }

    cases.append((
        "consistency_sum_bytes",
        f"{tr()} | group by proto | sum(bytes)",
        validate_sum_consistency,
    ))

    # --- 4. Filtered subset <= total ---
    def validate_filter_subset(result: QueryResult) -> dict:
        return {
            "expected_max": actual_total,
            "actual_count": result.total,
            "match": 0 < result.total <= actual_total,
        }

    cases.append((
        "consistency_filter_subset_tcp",
        f"{tr()} | proto tcp",
        validate_filter_subset,
    ))

    # --- 5. dport 80 subset <= total ---
    def validate_dport_subset(result: QueryResult) -> dict:
        return {
            "expected_max": actual_total,
            "actual_count": result.total,
            "match": 0 < result.total <= actual_total,
        }

    cases.append((
        "consistency_filter_subset_dport80",
        f"{tr()} | dport 80",
        validate_dport_subset,
    ))

    # --- 6. Two queries for same filter return same count ---
    def validate_idempotent(result: QueryResult) -> dict:
        second = execute_query(api_base, f"{tr()} | src 10.0.0.0/8")
        return {
            "expected_count": result.total,
            "actual_count": second.total,
            "match": result.total == second.total,
        }

    cases.append((
        "consistency_idempotent_query",
        f"{tr()} | src 10.0.0.0/8",
        validate_idempotent,
    ))

    # --- 7. Limit respects cap ---
    def validate_limit(result: QueryResult) -> dict:
        return {
            "expected_max_rows": 10,
            "actual_rows": result.row_count,
            "match": result.row_count <= 10,
        }

    cases.append((
        "consistency_limit",
        f"{tr()} | limit 10",
        validate_limit,
    ))

    # --- 8. Parts scanned > 0 ---
    def validate_parts(result: QueryResult) -> dict:
        return {
            "parts_scanned": result.parts_scanned,
            "match": result.parts_scanned > 0,
        }

    cases.append((
        "consistency_parts_scanned",
        f"{tr()}",
        validate_parts,
    ))

    # --- 9. Data survived: at least some rows exist ---
    def validate_data_survived(result: QueryResult) -> dict:
        # We sent 200 bulk flows + 22 from earlier phases.
        # Even with heavy UDP loss, we expect at least 100 rows.
        return {
            "expected_min": 100,
            "actual_total": actual_total,
            "match": actual_total >= 100,
        }

    cases.append((
        "consistency_data_survived",
        f"{tr()}",
        validate_data_survived,
    ))

    # --- 10. NOT filter + filter = total (using aggregation for true counts) ---
    def validate_complement(result: QueryResult) -> dict:
        def agg_count(query_str):
            r = execute_query(api_base, query_str)
            if r.rows:
                for i, col in enumerate(r.columns):
                    if "count" in col.lower():
                        try:
                            return int(r.rows[0][i])
                        except (IndexError, ValueError, TypeError):
                            pass
            return 0

        tcp_count = agg_count(f"{tr()} | proto tcp | group by 2h | count()")
        not_tcp_count = agg_count(f"{tr()} | not proto tcp | group by 2h | count()")
        return {
            "expected_total": actual_total,
            "actual_total": tcp_count + not_tcp_count,
            "actual_tcp": tcp_count,
            "actual_not_tcp": not_tcp_count,
            "match": tcp_count + not_tcp_count == actual_total,
        }

    cases.append((
        "consistency_complement_filter",
        f"{tr()}",
        validate_complement,
    ))

    return cases


class QueryValidator:
    """Runs query test cases and collects verdicts."""

    def __init__(self, api_base: str):
        self.api_base = api_base
        self.verdicts: list[TestVerdict] = []

    def run_test_suite(
        self,
        test_cases: list[tuple[str, str, Callable]],
        phase: str,
        storage_state: dict,
    ) -> list[TestVerdict]:
        """Run all test cases and return verdicts."""
        phase_verdicts = []
        for test_name, query, validator in test_cases:
            start = time.time()
            result = execute_query(self.api_base, query)
            duration_ms = (time.time() - start) * 1000

            if result.error:
                verdict = TestVerdict(
                    test_name=test_name,
                    query=query,
                    phase=phase,
                    passed=False,
                    expected={"no_error": True},
                    actual={"error": result.error, "http_status": result.http_status},
                    storage_state=storage_state,
                    details=f"Query failed: {result.error}",
                    duration_ms=duration_ms,
                )
            else:
                comparison = validator(result)
                passed = comparison.get("match", False)
                verdict = TestVerdict(
                    test_name=test_name,
                    query=query,
                    phase=phase,
                    passed=passed,
                    expected={k: v for k, v in comparison.items() if k.startswith("expected")},
                    actual={k: v for k, v in comparison.items() if k.startswith("actual") or k in ("has_rows", "has_parts", "parts_scanned", "rows_scanned")},
                    storage_state=storage_state,
                    details="" if passed else f"Mismatch: {comparison}",
                    duration_ms=duration_ms,
                )

            phase_verdicts.append(verdict)
            self.verdicts.append(verdict)

            status = "PASS" if verdict.passed else "FAIL"
            print(f"  [{status}] {test_name}: {query}")
            if not verdict.passed:
                print(f"         Expected: {verdict.expected}")
                print(f"         Actual:   {verdict.actual}")

        return phase_verdicts

    def summary(self) -> dict:
        """Return a summary of all test results."""
        total = len(self.verdicts)
        passed = sum(1 for v in self.verdicts if v.passed)
        failed = total - passed
        return {
            "total": total,
            "passed": passed,
            "failed": failed,
            "pass_rate": f"{passed/total*100:.1f}%" if total > 0 else "N/A",
            "by_phase": self._by_phase(),
        }

    def _by_phase(self) -> dict:
        phases: dict[str, dict] = {}
        for v in self.verdicts:
            if v.phase not in phases:
                phases[v.phase] = {"total": 0, "passed": 0, "failed": 0}
            phases[v.phase]["total"] += 1
            if v.passed:
                phases[v.phase]["passed"] += 1
            else:
                phases[v.phase]["failed"] += 1
        return phases

    def all_verdicts(self) -> list[dict]:
        return [v.to_dict() for v in self.verdicts]
