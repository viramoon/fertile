"""Run using the installed release's native Nautilus wheel."""
import json
import sys
from collections import Counter
from pathlib import Path

from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

MINUTE = 60_000_000_000


def verify(root, start, end):
    catalog = ParquetDataCatalog(str(root))
    result = {}
    for instrument, first in start.items():
        last = end[instrument]
        if first % MINUTE or last % MINUTE or last < first:
            raise ValueError(f"{instrument}: invalid closed-minute interval")
        bars = catalog.bars(bar_types=[f"{instrument}-1-MINUTE-LAST-EXTERNAL"], start=first, end=last)
        counts = Counter(b.ts_event for b in bars if first <= b.ts_event <= last)
        expected = range(first, last + 1, MINUTE)
        if len(counts) != len(expected) or any(counts[ns] != 1 for ns in expected):
            raise ValueError(f"{instrument}: expected one bar per minute in [{first},{last}], got {sorted(counts.items())}")
        result[instrument] = {"start_ns": first, "end_ns": last, "unique_minutes": len(expected)}
    return result


if __name__ == "__main__":
    window = json.load(sys.stdin)
    print(json.dumps(verify(Path(sys.argv[1]), window["start"], window["end"])))
