"""Generate the nine-equity ReplaySource input and check its persisted bars."""

import argparse
import csv
from decimal import Decimal
import json
from pathlib import Path
import time


EQUITIES = (
    "QQQ.XNAS", "SPY.ARCA", "AAPL.XNAS", "MSFT.XNAS", "NVDA.XNAS",
    "AMZN.XNAS", "GOOGL.XNAS", "META.XNAS", "TSLA.XNAS",
)
BAR_SPEC = "1-MINUTE-LAST"
INTERVAL_MS = 15_000
CANDLE_COUNT = 3
CANDLE_FIELDS = ("open", "high", "low", "close", "volume")


def fixture_candles():
    # Three deterministic bars, shared by all nine instruments. Price offsets
    # are integers in billionths of a unit, starting at 100.123456789.
    for step in range(CANDLE_COUNT):
        price = 123_456_789 + step * 20
        yield (f"100.{price:09d}", f"100.{price + 11:09d}",
               f"100.{price - 7:09d}", f"100.{price + 5:09d}", 100 + step)


def write_replay_fixture(directory):
    directory = directory.resolve(strict=True)
    candles = directory / "replay-nine.csv"
    config = directory / "replay-nine.yaml"
    with candles.open("x", newline="", encoding="utf-8") as file:
        rows = csv.writer(file)
        rows.writerow(CANDLE_FIELDS)
        rows.writerows(fixture_candles())
    # JSON is valid YAML and keeps the absolute path escaped for the collector.
    with config.open("x", encoding="utf-8") as file:
        json.dump({
            "bind": "127.0.0.1:0",
            "source": "replay",
            "instruments": EQUITIES,
            "bar_spec": BAR_SPEC,
            "max_age_seconds": 120,
            "flush_ms": 750,
            "shutdown_seconds": 20,
            "history_chunk_seconds": 3600,
            "catalog": "catalog",
            "replay": {"fixture": str(candles), "interval_ms": INTERVAL_MS},
        }, file, indent=2)
        file.write("\n")
    return config


def check_persisted_bars(bars, origin):
    interval_ns = INTERVAL_MS * 1_000_000
    expected = {
        (f"{instrument}-{BAR_SPEC}-EXTERNAL", origin + slot * interval_ns):
        tuple(map(Decimal, candle))
        for slot, candle in enumerate(fixture_candles())
        for instrument in EQUITIES
    }
    for bar in bars:
        key = (str(bar.bar_type), bar.ts_event)
        if key not in expected:
            raise ValueError(f"unexpected or duplicate replay bar for {key[0]} at {key[1]}")
        for field, value in zip(CANDLE_FIELDS, expected.pop(key), strict=True):
            if getattr(bar, field).as_decimal() != value:
                raise ValueError(f"wrong replay {field} for {key[0]} at {key[1]}")
    if expected:
        raise ValueError(f"missing {len(expected)} persisted replay bars")


def verify_replay_catalog(data_root):
    from nautilus_trader.persistence.catalog.parquet import ParquetDataCatalog

    data_root = data_root.resolve(strict=True)
    catalog = data_root / "catalog"
    provenance = json.loads((catalog / ".finance-collector/source.json").read_text(encoding="utf-8"))
    if (provenance["source"], provenance["spec"]) != ("replay", BAR_SPEC):
        raise ValueError("expected an isolated replay catalog")
    state = data_root / "finance-state/collector"
    origin = json.loads((state / "replay-origin.json").read_text(encoding="utf-8"))
    last = origin + (CANDLE_COUNT - 1) * INTERVAL_MS * 1_000_000
    deadline = time.monotonic() + 45
    while True:
        durable = json.loads((state / "watermarks.json").read_text(encoding="utf-8"))
        if all(durable.get(instrument, 0) >= last for instrument in EQUITIES):
            break
        if time.monotonic() >= deadline:
            raise ValueError("replay did not commit three bars for all nine equities before the deadline")
        time.sleep(0.2)
    bar_types = [f"{instrument}-{BAR_SPEC}-EXTERNAL" for instrument in EQUITIES]
    bars = ParquetDataCatalog(catalog).bars(bar_types=bar_types, start=origin, end=last)
    check_persisted_bars(bars, origin)
    return len(bars)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="existing disposable fixture directory or replay data root")
    parser.add_argument("--verify-catalog", action="store_true", help="read persisted bars from the replay data root")
    args = parser.parse_args()
    if args.verify_catalog:
        print(f"Verified exact prices and volume for {verify_replay_catalog(args.directory)} persisted replay bars")
    else:
        print(write_replay_fixture(args.directory))
