"""
IPFIX packet generator for e2e testing.

Builds RFC 7011-compliant IPFIX messages covering:
- Template sets (set ID 2)
- Option template sets (set ID 3)
- Data sets (set ID >= 256)
- Enterprise (vendor) field specifiers
- Variable-length fields
- Multiple observation domains
- Template withdrawal
- Multiple records per data set
- Multiple templates per template set
"""

import struct
import socket
import time
from dataclasses import dataclass, field
from typing import Optional


# --- Constants (RFC 7011) ---
IPFIX_VERSION = 0x000A
HEADER_LEN = 16
SET_HEADER_LEN = 4
TEMPLATE_SET_ID = 2
OPTIONS_TEMPLATE_SET_ID = 3
MIN_DATA_SET_ID = 256
ENTERPRISE_BIT = 0x8000
VARIABLE_LENGTH = 0xFFFF

# --- IANA IE IDs ---
IE_OCTET_DELTA_COUNT = 1        # u64, 8 bytes
IE_PACKET_DELTA_COUNT = 2       # u64, 8 bytes
IE_PROTOCOL_IDENTIFIER = 4     # u8, 1 byte
IE_IP_CLASS_OF_SERVICE = 5      # u8, 1 byte
IE_TCP_CONTROL_BITS = 6         # u16, 2 bytes
IE_SOURCE_TRANSPORT_PORT = 7    # u16, 2 bytes
IE_SOURCE_IPV4_ADDRESS = 8      # ipv4, 4 bytes
IE_INGRESS_INTERFACE = 10       # u32, 4 bytes
IE_DEST_TRANSPORT_PORT = 11     # u16, 2 bytes
IE_DEST_IPV4_ADDRESS = 12       # ipv4, 4 bytes
IE_EGRESS_INTERFACE = 14        # u32, 4 bytes
IE_SOURCE_IPV6_ADDRESS = 27     # ipv6, 16 bytes
IE_DEST_IPV6_ADDRESS = 28       # ipv6, 16 bytes
IE_SOURCE_MAC_ADDRESS = 56      # mac, 6 bytes
IE_FLOW_END_REASON = 59         # u8, 1 byte
IE_DEST_MAC_ADDRESS = 80        # mac, 6 bytes
IE_VLAN_ID = 58                 # u16, 2 bytes
IE_FLOW_START_SECONDS = 150     # u32, 4 bytes
IE_FLOW_END_SECONDS = 151       # u32, 4 bytes

# Protocols
PROTO_TCP = 6
PROTO_UDP = 17
PROTO_ICMP = 1

# TCP flags
TCP_SYN = 0x0002
TCP_ACK = 0x0010
TCP_FIN = 0x0001
TCP_RST = 0x0004
TCP_SYN_ACK = TCP_SYN | TCP_ACK


@dataclass
class FieldSpec:
    """IPFIX field specifier."""
    element_id: int
    length: int
    enterprise_id: Optional[int] = None

    def to_bytes(self) -> bytes:
        if self.enterprise_id is not None:
            ie_id = self.element_id | ENTERPRISE_BIT
            return struct.pack("!HHI", ie_id, self.length, self.enterprise_id)
        return struct.pack("!HH", self.element_id, self.length)

    @property
    def wire_len(self) -> int:
        return 8 if self.enterprise_id is not None else 4


@dataclass
class Template:
    """IPFIX template definition."""
    template_id: int
    fields: list[FieldSpec]

    @property
    def record_length(self) -> int:
        """Minimum data record length for this template (fixed-length fields)."""
        return sum(f.length for f in self.fields if f.length != VARIABLE_LENGTH)


@dataclass
class FlowRecord:
    """A decoded flow record for tracking expected query results."""
    src_ip: str
    dst_ip: str
    src_port: int
    dst_port: int
    protocol: int
    bytes_count: int
    packets_count: int
    tcp_flags: int = 0
    tos: int = 0
    ingress_if: int = 0
    egress_if: int = 0
    vlan_id: int = 0
    flow_start: int = 0
    flow_end: int = 0
    flow_end_reason: int = 0
    src_mac: str = "00:00:00:00:00:00"
    dst_mac: str = "00:00:00:00:00:00"
    # System columns (set by sender context)
    exporter_ip: str = "127.0.0.1"
    exporter_port: int = 0
    export_time: int = 0
    observation_domain: int = 0
    # Metadata
    template_id: int = 0
    phase: str = ""


def build_header(length: int, export_time: int, seq: int, domain: int) -> bytes:
    """Build 16-byte IPFIX message header."""
    return struct.pack("!HHIII", IPFIX_VERSION, length, export_time, seq, domain)


def build_template_set(templates: list[Template]) -> bytes:
    """Build a template set (set ID = 2) containing one or more templates."""
    body = b""
    for tmpl in templates:
        body += struct.pack("!HH", tmpl.template_id, len(tmpl.fields))
        for fs in tmpl.fields:
            body += fs.to_bytes()
    length = SET_HEADER_LEN + len(body)
    header = struct.pack("!HH", TEMPLATE_SET_ID, length)
    return header + body


def build_option_template_set(
    template_id: int,
    scope_fields: list[FieldSpec],
    option_fields: list[FieldSpec],
) -> bytes:
    """Build an options template set (set ID = 3)."""
    all_fields = scope_fields + option_fields
    body = struct.pack("!HHH", template_id, len(all_fields), len(scope_fields))
    for fs in all_fields:
        body += fs.to_bytes()
    length = SET_HEADER_LEN + len(body)
    header = struct.pack("!HH", OPTIONS_TEMPLATE_SET_ID, length)
    return header + body


def build_template_withdrawal(template_id: int) -> bytes:
    """Build a template withdrawal (field count = 0)."""
    body = struct.pack("!HH", template_id, 0)
    length = SET_HEADER_LEN + len(body)
    header = struct.pack("!HH", TEMPLATE_SET_ID, length)
    return header + body


def build_data_set(template_id: int, records: list[bytes]) -> bytes:
    """Build a data set containing raw record bytes."""
    body = b"".join(records)
    length = SET_HEADER_LEN + len(body)
    header = struct.pack("!HH", template_id, length)
    return header + body


def encode_variable_length(data: bytes) -> bytes:
    """Encode variable-length field per RFC 7011 Section 7."""
    if len(data) < 255:
        return struct.pack("!B", len(data)) + data
    return struct.pack("!BH", 255, len(data)) + data


def build_message(sets: list[bytes], export_time: int, seq: int, domain: int) -> bytes:
    """Assemble a complete IPFIX message from sets."""
    body = b"".join(sets)
    length = HEADER_LEN + len(body)
    return build_header(length, export_time, seq, domain) + body


def ip_to_bytes(ip: str) -> bytes:
    """Convert dotted-quad IPv4 to 4 bytes."""
    return socket.inet_aton(ip)


def ipv6_to_bytes(ip: str) -> bytes:
    """Convert IPv6 string to 16 bytes."""
    return socket.inet_pton(socket.AF_INET6, ip)


def mac_to_bytes(mac: str) -> bytes:
    """Convert colon-separated MAC to 6 bytes."""
    return bytes(int(x, 16) for x in mac.split(":"))


# ---------------------------------------------------------------------------
# Template definitions
# ---------------------------------------------------------------------------

# Template 256: Basic IPv4 5-tuple with counters
TEMPLATE_BASIC_IPV4 = Template(
    template_id=256,
    fields=[
        FieldSpec(IE_SOURCE_IPV4_ADDRESS, 4),
        FieldSpec(IE_DEST_IPV4_ADDRESS, 4),
        FieldSpec(IE_PROTOCOL_IDENTIFIER, 1),
        FieldSpec(IE_SOURCE_TRANSPORT_PORT, 2),
        FieldSpec(IE_DEST_TRANSPORT_PORT, 2),
        FieldSpec(IE_OCTET_DELTA_COUNT, 8),
        FieldSpec(IE_PACKET_DELTA_COUNT, 8),
    ],
)

# Template 257: Extended IPv4 with TCP flags, ToS, interfaces, timestamps
TEMPLATE_EXTENDED_IPV4 = Template(
    template_id=257,
    fields=[
        FieldSpec(IE_SOURCE_IPV4_ADDRESS, 4),
        FieldSpec(IE_DEST_IPV4_ADDRESS, 4),
        FieldSpec(IE_PROTOCOL_IDENTIFIER, 1),
        FieldSpec(IE_SOURCE_TRANSPORT_PORT, 2),
        FieldSpec(IE_DEST_TRANSPORT_PORT, 2),
        FieldSpec(IE_OCTET_DELTA_COUNT, 8),
        FieldSpec(IE_PACKET_DELTA_COUNT, 8),
        FieldSpec(IE_TCP_CONTROL_BITS, 2),
        FieldSpec(IE_IP_CLASS_OF_SERVICE, 1),
        FieldSpec(IE_INGRESS_INTERFACE, 4),
        FieldSpec(IE_EGRESS_INTERFACE, 4),
        FieldSpec(IE_FLOW_START_SECONDS, 4),
        FieldSpec(IE_FLOW_END_SECONDS, 4),
        FieldSpec(IE_FLOW_END_REASON, 1),
    ],
)

# Template 258: IPv4 with MACs and VLAN
TEMPLATE_L2_IPV4 = Template(
    template_id=258,
    fields=[
        FieldSpec(IE_SOURCE_IPV4_ADDRESS, 4),
        FieldSpec(IE_DEST_IPV4_ADDRESS, 4),
        FieldSpec(IE_PROTOCOL_IDENTIFIER, 1),
        FieldSpec(IE_SOURCE_TRANSPORT_PORT, 2),
        FieldSpec(IE_DEST_TRANSPORT_PORT, 2),
        FieldSpec(IE_OCTET_DELTA_COUNT, 8),
        FieldSpec(IE_PACKET_DELTA_COUNT, 8),
        FieldSpec(IE_SOURCE_MAC_ADDRESS, 6),
        FieldSpec(IE_DEST_MAC_ADDRESS, 6),
        FieldSpec(IE_VLAN_ID, 2),
    ],
)

# Template 259: IPv6 basic
TEMPLATE_BASIC_IPV6 = Template(
    template_id=259,
    fields=[
        FieldSpec(IE_SOURCE_IPV6_ADDRESS, 16),
        FieldSpec(IE_DEST_IPV6_ADDRESS, 16),
        FieldSpec(IE_PROTOCOL_IDENTIFIER, 1),
        FieldSpec(IE_SOURCE_TRANSPORT_PORT, 2),
        FieldSpec(IE_DEST_TRANSPORT_PORT, 2),
        FieldSpec(IE_OCTET_DELTA_COUNT, 8),
        FieldSpec(IE_PACKET_DELTA_COUNT, 8),
    ],
)

ALL_TEMPLATES = [
    TEMPLATE_BASIC_IPV4,
    TEMPLATE_EXTENDED_IPV4,
    TEMPLATE_L2_IPV4,
    TEMPLATE_BASIC_IPV6,
]


def encode_record_basic_ipv4(rec: FlowRecord) -> bytes:
    """Encode a FlowRecord into template 256 wire bytes."""
    return (
        ip_to_bytes(rec.src_ip)
        + ip_to_bytes(rec.dst_ip)
        + struct.pack("!B", rec.protocol)
        + struct.pack("!H", rec.src_port)
        + struct.pack("!H", rec.dst_port)
        + struct.pack("!Q", rec.bytes_count)
        + struct.pack("!Q", rec.packets_count)
    )


def encode_record_extended_ipv4(rec: FlowRecord) -> bytes:
    """Encode a FlowRecord into template 257 wire bytes."""
    return (
        ip_to_bytes(rec.src_ip)
        + ip_to_bytes(rec.dst_ip)
        + struct.pack("!B", rec.protocol)
        + struct.pack("!H", rec.src_port)
        + struct.pack("!H", rec.dst_port)
        + struct.pack("!Q", rec.bytes_count)
        + struct.pack("!Q", rec.packets_count)
        + struct.pack("!H", rec.tcp_flags)
        + struct.pack("!B", rec.tos)
        + struct.pack("!I", rec.ingress_if)
        + struct.pack("!I", rec.egress_if)
        + struct.pack("!I", rec.flow_start)
        + struct.pack("!I", rec.flow_end)
        + struct.pack("!B", rec.flow_end_reason)
    )


def encode_record_l2_ipv4(rec: FlowRecord) -> bytes:
    """Encode a FlowRecord into template 258 wire bytes."""
    return (
        ip_to_bytes(rec.src_ip)
        + ip_to_bytes(rec.dst_ip)
        + struct.pack("!B", rec.protocol)
        + struct.pack("!H", rec.src_port)
        + struct.pack("!H", rec.dst_port)
        + struct.pack("!Q", rec.bytes_count)
        + struct.pack("!Q", rec.packets_count)
        + mac_to_bytes(rec.src_mac)
        + mac_to_bytes(rec.dst_mac)
        + struct.pack("!H", rec.vlan_id)
    )


ENCODERS = {
    256: encode_record_basic_ipv4,
    257: encode_record_extended_ipv4,
    258: encode_record_l2_ipv4,
}


# ---------------------------------------------------------------------------
# Test data generation
# ---------------------------------------------------------------------------

def generate_phase1_flows(export_time: int) -> list[FlowRecord]:
    """
    Phase 1: Basic IPv4 flows with known, deterministic data.

    10 flows covering:
    - TCP (HTTP, HTTPS, SSH)
    - UDP (DNS)
    - ICMP
    - Various subnets (10.0.0.0/8, 192.168.0.0/16, 172.16.0.0/12)
    - Range of byte/packet counts for aggregation testing
    """
    flows = [
        # HTTP request: 10.0.0.1 -> 192.168.1.100:80
        FlowRecord(
            src_ip="10.0.0.1", dst_ip="192.168.1.100",
            src_port=45000, dst_port=80, protocol=PROTO_TCP,
            bytes_count=1500, packets_count=10,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # HTTP response: 192.168.1.100:80 -> 10.0.0.1
        FlowRecord(
            src_ip="192.168.1.100", dst_ip="10.0.0.1",
            src_port=80, dst_port=45000, protocol=PROTO_TCP,
            bytes_count=52000, packets_count=40,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # HTTPS: 10.0.0.2 -> 172.16.0.50:443
        FlowRecord(
            src_ip="10.0.0.2", dst_ip="172.16.0.50",
            src_port=50000, dst_port=443, protocol=PROTO_TCP,
            bytes_count=3200, packets_count=25,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # DNS query: 10.0.0.1 -> 8.8.8.8:53
        FlowRecord(
            src_ip="10.0.0.1", dst_ip="8.8.8.8",
            src_port=33000, dst_port=53, protocol=PROTO_UDP,
            bytes_count=64, packets_count=1,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # DNS response: 8.8.8.8:53 -> 10.0.0.1
        FlowRecord(
            src_ip="8.8.8.8", dst_ip="10.0.0.1",
            src_port=53, dst_port=33000, protocol=PROTO_UDP,
            bytes_count=128, packets_count=1,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # SSH: 10.0.0.3 -> 192.168.1.1:22
        FlowRecord(
            src_ip="10.0.0.3", dst_ip="192.168.1.1",
            src_port=55000, dst_port=22, protocol=PROTO_TCP,
            bytes_count=8000, packets_count=100,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # ICMP ping: 10.0.0.1 -> 192.168.1.1
        FlowRecord(
            src_ip="10.0.0.1", dst_ip="192.168.1.1",
            src_port=0, dst_port=0, protocol=PROTO_ICMP,
            bytes_count=84, packets_count=1,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # Large transfer: 10.0.0.5 -> 172.16.0.10:8080
        FlowRecord(
            src_ip="10.0.0.5", dst_ip="172.16.0.10",
            src_port=60000, dst_port=8080, protocol=PROTO_TCP,
            bytes_count=5_000_000, packets_count=3500,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # Small flow: 192.168.1.50 -> 10.0.0.1:443
        FlowRecord(
            src_ip="192.168.1.50", dst_ip="10.0.0.1",
            src_port=44000, dst_port=443, protocol=PROTO_TCP,
            bytes_count=200, packets_count=3,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
        # Another DNS: 10.0.0.2 -> 1.1.1.1:53
        FlowRecord(
            src_ip="10.0.0.2", dst_ip="1.1.1.1",
            src_port=34000, dst_port=53, protocol=PROTO_UDP,
            bytes_count=72, packets_count=1,
            export_time=export_time, observation_domain=1,
            template_id=256, phase="phase1",
        ),
    ]
    return flows


def generate_phase2_flows(export_time: int) -> list[FlowRecord]:
    """
    Phase 2: Extended IPv4 flows with TCP flags, ToS, interfaces, timestamps.

    Tests template 257 with richer metadata. Some flows overlap with phase 1
    sources/destinations to test aggregation across templates.
    """
    base = export_time - 60  # flow started 60s before export
    flows = [
        # SYN scan detection: many SYNs, no ACKs
        FlowRecord(
            src_ip="10.0.0.99", dst_ip="192.168.1.100",
            src_port=40000, dst_port=80, protocol=PROTO_TCP,
            bytes_count=60, packets_count=1,
            tcp_flags=TCP_SYN, tos=0, ingress_if=1, egress_if=2,
            flow_start=base, flow_end=base + 1, flow_end_reason=3,
            export_time=export_time, observation_domain=1,
            template_id=257, phase="phase2",
        ),
        FlowRecord(
            src_ip="10.0.0.99", dst_ip="192.168.1.100",
            src_port=40001, dst_port=443, protocol=PROTO_TCP,
            bytes_count=60, packets_count=1,
            tcp_flags=TCP_SYN, tos=0, ingress_if=1, egress_if=2,
            flow_start=base, flow_end=base + 1, flow_end_reason=3,
            export_time=export_time, observation_domain=1,
            template_id=257, phase="phase2",
        ),
        FlowRecord(
            src_ip="10.0.0.99", dst_ip="192.168.1.100",
            src_port=40002, dst_port=22, protocol=PROTO_TCP,
            bytes_count=60, packets_count=1,
            tcp_flags=TCP_SYN, tos=0, ingress_if=1, egress_if=2,
            flow_start=base, flow_end=base + 1, flow_end_reason=3,
            export_time=export_time, observation_domain=1,
            template_id=257, phase="phase2",
        ),
        # Normal established TCP with SYN+ACK
        FlowRecord(
            src_ip="10.0.0.1", dst_ip="192.168.1.100",
            src_port=46000, dst_port=80, protocol=PROTO_TCP,
            bytes_count=25000, packets_count=20,
            tcp_flags=TCP_SYN_ACK, tos=0x20, ingress_if=1, egress_if=3,
            flow_start=base, flow_end=base + 30, flow_end_reason=1,
            export_time=export_time, observation_domain=1,
            template_id=257, phase="phase2",
        ),
        # High-priority traffic (ToS=0xB8 = EF)
        FlowRecord(
            src_ip="10.0.0.10", dst_ip="172.16.0.20",
            src_port=5060, dst_port=5060, protocol=PROTO_UDP,
            bytes_count=500, packets_count=5,
            tcp_flags=0, tos=0xB8, ingress_if=2, egress_if=4,
            flow_start=base, flow_end=base + 10, flow_end_reason=1,
            export_time=export_time, observation_domain=1,
            template_id=257, phase="phase2",
        ),
        # RST terminated flow
        FlowRecord(
            src_ip="192.168.1.200", dst_ip="10.0.0.1",
            src_port=80, dst_port=47000, protocol=PROTO_TCP,
            bytes_count=100, packets_count=2,
            tcp_flags=TCP_RST, tos=0, ingress_if=3, egress_if=1,
            flow_start=base + 5, flow_end=base + 6, flow_end_reason=2,
            export_time=export_time, observation_domain=1,
            template_id=257, phase="phase2",
        ),
        # FIN-terminated long flow
        FlowRecord(
            src_ip="10.0.0.1", dst_ip="172.16.0.50",
            src_port=51000, dst_port=443, protocol=PROTO_TCP,
            bytes_count=150000, packets_count=120,
            tcp_flags=TCP_FIN | TCP_ACK, tos=0, ingress_if=1, egress_if=2,
            flow_start=base, flow_end=base + 55, flow_end_reason=1,
            export_time=export_time, observation_domain=1,
            template_id=257, phase="phase2",
        ),
    ]
    return flows


def generate_phase3_flows(export_time: int) -> list[FlowRecord]:
    """
    Phase 3: L2 flows with MACs and VLANs (template 258).
    Also includes flows from a SECOND observation domain (domain=2)
    to test multi-domain handling.
    """
    flows = [
        # VLAN 100 traffic
        FlowRecord(
            src_ip="10.1.0.1", dst_ip="10.1.0.2",
            src_port=12345, dst_port=80, protocol=PROTO_TCP,
            bytes_count=4096, packets_count=8,
            src_mac="aa:bb:cc:00:01:01", dst_mac="aa:bb:cc:00:01:02",
            vlan_id=100,
            export_time=export_time, observation_domain=1,
            template_id=258, phase="phase3",
        ),
        # VLAN 200 traffic
        FlowRecord(
            src_ip="10.2.0.1", dst_ip="10.2.0.2",
            src_port=22000, dst_port=443, protocol=PROTO_TCP,
            bytes_count=8192, packets_count=16,
            src_mac="aa:bb:cc:00:02:01", dst_mac="aa:bb:cc:00:02:02",
            vlan_id=200,
            export_time=export_time, observation_domain=1,
            template_id=258, phase="phase3",
        ),
        # VLAN 100, same src/dst as phase1 to test cross-template aggregation
        FlowRecord(
            src_ip="10.0.0.1", dst_ip="192.168.1.100",
            src_port=45001, dst_port=80, protocol=PROTO_TCP,
            bytes_count=2048, packets_count=5,
            src_mac="aa:bb:cc:00:00:01", dst_mac="dd:ee:ff:00:01:64",
            vlan_id=100,
            export_time=export_time, observation_domain=1,
            template_id=258, phase="phase3",
        ),
        # Domain 2 flows — same template IDs but different domain
        FlowRecord(
            src_ip="10.0.0.1", dst_ip="192.168.1.100",
            src_port=45002, dst_port=80, protocol=PROTO_TCP,
            bytes_count=3000, packets_count=7,
            src_mac="11:22:33:44:55:01", dst_mac="11:22:33:44:55:02",
            vlan_id=300,
            export_time=export_time, observation_domain=2,
            template_id=258, phase="phase3",
        ),
        FlowRecord(
            src_ip="10.99.0.1", dst_ip="10.99.0.2",
            src_port=8080, dst_port=9090, protocol=PROTO_TCP,
            bytes_count=1024, packets_count=2,
            src_mac="11:22:33:44:55:03", dst_mac="11:22:33:44:55:04",
            vlan_id=300,
            export_time=export_time, observation_domain=2,
            template_id=258, phase="phase3",
        ),
    ]
    return flows


def generate_phase4_bulk_flows(export_time: int, count: int = 200) -> list[FlowRecord]:
    """
    Phase 4: Bulk flows to trigger multiple flushes and merges.

    Generates `count` flows with varying IPs to stress the storage engine.
    Uses template 256 (basic IPv4).
    """
    flows = []
    for i in range(count):
        a = (i >> 8) & 0xFF
        b = i & 0xFF
        flows.append(FlowRecord(
            src_ip=f"10.{a}.{b}.1",
            dst_ip=f"172.16.{a}.{b}",
            src_port=10000 + (i % 50000),
            dst_port=[80, 443, 22, 53, 8080, 3306, 5432, 8443][i % 8],
            protocol=[PROTO_TCP, PROTO_UDP][i % 2],
            bytes_count=100 * (i + 1),
            packets_count=max(1, i % 100),
            export_time=export_time,
            observation_domain=1,
            template_id=256,
            phase="phase4",
        ))
    return flows


class IpfixSender:
    """Sends IPFIX packets over UDP."""

    def __init__(self, host: str, port: int):
        self.host = host
        self.port = port
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.seq = 0
        self._templates_sent: dict[tuple[int, int], bool] = {}  # (domain, tmpl_id) -> sent

    def close(self):
        self.sock.close()

    def _send_raw(self, data: bytes):
        self.sock.sendto(data, (self.host, self.port))

    def send_templates(self, templates: list[Template], domain: int, export_time: int):
        """Send template definitions for given observation domain."""
        tmpl_set = build_template_set(templates)
        self.seq += 1
        msg = build_message([tmpl_set], export_time, self.seq, domain)
        self._send_raw(msg)
        for t in templates:
            self._templates_sent[(domain, t.template_id)] = True

    def send_flows(
        self,
        flows: list[FlowRecord],
        export_time: int,
        max_records_per_set: int = 20,
    ) -> list[FlowRecord]:
        """
        Send flows as IPFIX data sets. Groups by (domain, template_id).
        Returns the flows with exporter metadata filled in.
        """
        # Ensure templates are sent first for each domain
        domains_needing_templates: dict[int, set[int]] = {}
        for f in flows:
            key = (f.observation_domain, f.template_id)
            if key not in self._templates_sent:
                domains_needing_templates.setdefault(f.observation_domain, set()).add(f.template_id)

        tmpl_lookup = {t.template_id: t for t in ALL_TEMPLATES}
        for domain, tmpl_ids in domains_needing_templates.items():
            templates = [tmpl_lookup[tid] for tid in tmpl_ids if tid in tmpl_lookup]
            if templates:
                self.send_templates(templates, domain, export_time)
                time.sleep(0.2)  # Delay for template registration before data

        # Group flows by (domain, template_id)
        groups: dict[tuple[int, int], list[FlowRecord]] = {}
        for f in flows:
            key = (f.observation_domain, f.template_id)
            groups.setdefault(key, []).append(f)

        sent_flows = []
        for (domain, tmpl_id), group_flows in groups.items():
            encoder = ENCODERS.get(tmpl_id)
            if not encoder:
                continue

            # Split into chunks
            for i in range(0, len(group_flows), max_records_per_set):
                chunk = group_flows[i : i + max_records_per_set]
                records = [encoder(f) for f in chunk]
                data_set = build_data_set(tmpl_id, records)
                self.seq += 1
                msg = build_message([data_set], export_time, self.seq, domain)
                self._send_raw(msg)

                for f in chunk:
                    f.export_time = export_time
                    f.observation_domain = domain
                    sent_flows.append(f)

                time.sleep(0.03)  # Pace to avoid UDP drops

        return sent_flows

    def send_template_withdrawal(self, template_id: int, domain: int, export_time: int):
        """Send a template withdrawal message."""
        withdrawal_set = build_template_withdrawal(template_id)
        self.seq += 1
        msg = build_message([withdrawal_set], export_time, self.seq, domain)
        self._send_raw(msg)
        self._templates_sent.pop((domain, template_id), None)

    def send_option_template(
        self,
        template_id: int,
        scope_fields: list[FieldSpec],
        option_fields: list[FieldSpec],
        domain: int,
        export_time: int,
    ):
        """Send an option template set."""
        opt_set = build_option_template_set(template_id, scope_fields, option_fields)
        self.seq += 1
        msg = build_message([opt_set], export_time, self.seq, domain)
        self._send_raw(msg)


if __name__ == "__main__":
    # Quick self-test: generate and validate packet sizes
    now = int(time.time())
    flows = generate_phase1_flows(now)
    print(f"Phase 1: {len(flows)} flows")
    for f in flows:
        rec = encode_record_basic_ipv4(f)
        assert len(rec) == TEMPLATE_BASIC_IPV4.record_length, f"Bad record length: {len(rec)}"
    print("All phase 1 records encode correctly")

    flows2 = generate_phase2_flows(now)
    print(f"Phase 2: {len(flows2)} flows")
    for f in flows2:
        rec = encode_record_extended_ipv4(f)
        assert len(rec) == TEMPLATE_EXTENDED_IPV4.record_length
    print("All phase 2 records encode correctly")

    flows3 = generate_phase3_flows(now)
    print(f"Phase 3: {len(flows3)} flows")

    flows4 = generate_phase4_bulk_flows(now, 200)
    print(f"Phase 4: {len(flows4)} flows")
    print("Self-test passed")
