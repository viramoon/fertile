"""Generate the nine-equity ReplaySource input for a disposable test stack."""

import argparse
import csv
import json
from pathlib import Path


EQUITIES = (
    "QQQ.XNAS", "SPY.ARCA", "AAPL.XNAS", "MSFT.XNAS", "NVDA.XNAS",
    "AMZN.XNAS", "GOOGL.XNAS", "META.XNAS", "TSLA.XNAS",
)


def write_replay_fixture(directory):
    directory = directory.resolve(strict=True)
    candles = directory / "replay-nine.csv"
    config = directory / "replay-nine.yaml"
    # Three deterministic bars, shared by all nine instruments. Price offsets
    # are integers in billionths of a unit, starting at 100.123456789.
    with candles.open("x", newline="", encoding="utf-8") as file:
        rows = csv.writer(file)
        rows.writerow(("open", "high", "low", "close", "volume"))
        for step in range(3):
            price = 123_456_789 + step * 20
            rows.writerow((f"100.{price:09d}", f"100.{price + 11:09d}",
                           f"100.{price - 7:09d}", f"100.{price + 5:09d}", 100 + step))
    # JSON is valid YAML and keeps the absolute path escaped for the collector.
    with config.open("x", encoding="utf-8") as file:
        json.dump({
            "bind": "127.0.0.1:0",
            "source": "replay",
            "instruments": EQUITIES,
            "bar_spec": "1-MINUTE-LAST",
            "max_age_seconds": 120,
            "flush_ms": 750,
            "shutdown_seconds": 20,
            "history_chunk_seconds": 3600,
            "catalog": "catalog",
            "replay": {"fixture": str(candles), "interval_ms": 15_000},
        }, file, indent=2)
        file.write("\n")
    return config


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path, help="existing disposable test directory")
    print(write_replay_fixture(parser.parse_args().directory))
