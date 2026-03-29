"""
Storage directory monitor for e2e testing.

Watches the flowcus storage directory for:
- New parts being flushed (meta.bin appears)
- Merge activity (generation number changes)
- Part deletion (old generation cleaned up after merge)

Produces a timeline of StorageEvent objects that the test orchestrator
uses to understand system state at each query phase.
"""

import os
import struct
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class EventType(str, Enum):
    PART_FLUSHED = "PART_FLUSHED"
    MERGE_DETECTED = "MERGE_DETECTED"
    PART_DELETED = "PART_DELETED"
    SNAPSHOT = "SNAPSHOT"


@dataclass
class PartInfo:
    """Parsed info from a part directory name."""
    path: str
    hour_dir: str           # e.g., "2026/03/24/14"
    generation: int
    min_ts: int
    max_ts: int
    seq: int
    row_count: int = 0      # Read from meta.bin if available
    has_meta: bool = False
    has_columns: bool = False

    @classmethod
    def from_dirname(cls, hour_dir: str, dirname: str, full_path: str) -> Optional["PartInfo"]:
        """Parse a part directory name.
        Versioned: {ver}_{gen:05}_{min_ts}_{max_ts}_{seq:06} (5 segments)
        Legacy:    {gen:05}_{min_ts}_{max_ts}_{seq:06}        (4 segments)
        """
        parts = dirname.split("_")
        try:
            if len(parts) == 5:
                # Versioned format: ver_gen_min_max_seq
                return cls(
                    path=full_path,
                    hour_dir=hour_dir,
                    generation=int(parts[1]),
                    min_ts=int(parts[2]),
                    max_ts=int(parts[3]),
                    seq=int(parts[4]),
                )
            if len(parts) == 4:
                # Legacy format: gen_min_max_seq
                return cls(
                    path=full_path,
                    hour_dir=hour_dir,
                    generation=int(parts[0]),
                    min_ts=int(parts[1]),
                    max_ts=int(parts[2]),
                    seq=int(parts[3]),
                )
        except ValueError:
            pass
        return None

    @property
    def name(self) -> str:
        return os.path.basename(self.path)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "hour_dir": self.hour_dir,
            "generation": self.generation,
            "min_ts": self.min_ts,
            "max_ts": self.max_ts,
            "seq": self.seq,
            "row_count": self.row_count,
            "has_meta": self.has_meta,
            "has_columns": self.has_columns,
        }


@dataclass
class StorageEvent:
    """A timestamped event from the storage directory."""
    timestamp: float
    event_type: EventType
    part: Optional[PartInfo] = None
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = {
            "timestamp": self.timestamp,
            "event_type": self.event_type.value,
            "details": self.details,
        }
        if self.part:
            d["part"] = self.part.to_dict()
        return d


def read_meta_row_count(meta_path: str) -> int:
    """Read row count from meta.bin (offset 8, u64 LE in the 256-byte header)."""
    try:
        with open(meta_path, "rb") as f:
            data = f.read(256)
        if len(data) < 256:
            return 0
        # Magic "FMTA" at offset 0 (4 bytes), then version (4 bytes), then row_count (8 bytes)
        magic = data[0:4]
        if magic != b"FMTA":
            return 0
        row_count = struct.unpack("<Q", data[8:16])[0]
        return row_count
    except (OSError, struct.error):
        return 0


class StorageMonitor:
    """
    Monitors the flowcus storage directory for changes.

    Usage:
        monitor = StorageMonitor("/data/storage/flows")
        monitor.scan()          # Initial scan
        time.sleep(5)
        events = monitor.scan() # Returns new events since last scan
        snapshot = monitor.snapshot()
    """

    def __init__(self, flows_dir: str):
        self.flows_dir = flows_dir
        self.known_parts: dict[str, PartInfo] = {}  # path -> PartInfo
        self.events: list[StorageEvent] = []
        self._scan_count = 0

    def _discover_parts(self) -> dict[str, PartInfo]:
        """Walk the storage directory and find all part directories."""
        found = {}
        flows_path = Path(self.flows_dir)
        if not flows_path.exists():
            return found

        # Walk: flows/{YYYY}/{MM}/{DD}/{HH}/{part_dir}/
        for year_dir in sorted(flows_path.iterdir()):
            if not year_dir.is_dir():
                continue
            for month_dir in sorted(year_dir.iterdir()):
                if not month_dir.is_dir():
                    continue
                for day_dir in sorted(month_dir.iterdir()):
                    if not day_dir.is_dir():
                        continue
                    for hour_dir in sorted(day_dir.iterdir()):
                        if not hour_dir.is_dir():
                            continue
                        hour_rel = str(hour_dir.relative_to(flows_path))
                        for part_dir in sorted(hour_dir.iterdir()):
                            if not part_dir.is_dir():
                                continue
                            info = PartInfo.from_dirname(
                                hour_rel, part_dir.name, str(part_dir)
                            )
                            if info is None:
                                continue

                            meta_path = part_dir / "meta.bin"
                            columns_dir = part_dir / "columns"
                            info.has_meta = meta_path.exists()
                            info.has_columns = columns_dir.exists() and any(columns_dir.iterdir()) if columns_dir.exists() else False

                            if info.has_meta:
                                info.row_count = read_meta_row_count(str(meta_path))

                            found[str(part_dir)] = info
        return found

    def scan(self) -> list[StorageEvent]:
        """
        Scan storage directory and return new events since last scan.
        Call periodically to build the timeline.
        """
        self._scan_count += 1
        now = time.time()
        current_parts = self._discover_parts()
        new_events = []

        # Detect new parts (flushed or merged)
        for path, info in current_parts.items():
            if path not in self.known_parts:
                if info.generation > 0:
                    event_type = EventType.MERGE_DETECTED
                else:
                    event_type = EventType.PART_FLUSHED
                new_events.append(StorageEvent(
                    timestamp=now,
                    event_type=event_type,
                    part=info,
                    details={
                        "generation": info.generation,
                        "row_count": info.row_count,
                        "hour_dir": info.hour_dir,
                    },
                ))

        # Detect deleted parts (old gen cleaned up after merge)
        for path, info in self.known_parts.items():
            if path not in current_parts:
                new_events.append(StorageEvent(
                    timestamp=now,
                    event_type=EventType.PART_DELETED,
                    part=info,
                    details={
                        "generation": info.generation,
                        "row_count": info.row_count,
                        "hour_dir": info.hour_dir,
                    },
                ))

        self.known_parts = current_parts
        self.events.extend(new_events)
        return new_events

    def snapshot(self) -> StorageEvent:
        """Take a point-in-time snapshot of all current parts."""
        parts_by_hour: dict[str, list[dict]] = {}
        total_rows = 0
        max_gen = 0

        for info in self.known_parts.values():
            parts_by_hour.setdefault(info.hour_dir, []).append(info.to_dict())
            total_rows += info.row_count
            max_gen = max(max_gen, info.generation)

        event = StorageEvent(
            timestamp=time.time(),
            event_type=EventType.SNAPSHOT,
            details={
                "total_parts": len(self.known_parts),
                "total_rows": total_rows,
                "max_generation": max_gen,
                "hours": len(parts_by_hour),
                "parts_by_hour": parts_by_hour,
            },
        )
        self.events.append(event)
        return event

    def wait_for_parts(self, min_parts: int, timeout: float = 30.0, poll: float = 0.5) -> bool:
        """Wait until at least `min_parts` exist in storage."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.scan()
            if len(self.known_parts) >= min_parts:
                return True
            time.sleep(poll)
        return False

    def wait_for_generation(self, min_gen: int, timeout: float = 60.0, poll: float = 1.0) -> bool:
        """Wait until at least one part has generation >= min_gen."""
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.scan()
            for info in self.known_parts.values():
                if info.generation >= min_gen:
                    return True
            time.sleep(poll)
        return False

    def total_rows(self) -> int:
        """Sum of row counts across all current parts."""
        return sum(info.row_count for info in self.known_parts.values())

    def generation_summary(self) -> dict[int, int]:
        """Count of parts per generation."""
        gens: dict[int, int] = {}
        for info in self.known_parts.values():
            gens[info.generation] = gens.get(info.generation, 0) + 1
        return gens

    def get_timeline(self) -> list[dict]:
        """Return all events as serializable dicts."""
        return [e.to_dict() for e in self.events]
