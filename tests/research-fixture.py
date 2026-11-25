"""Create only the offline integration fixture in the selected research root."""

import csv
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory

from fertile_finance.research_catalog import import_catalog, research_root, sessions


def main():
    calendar = sessions(date(2024, 11, 1), date(2024, 12, 20))
    with TemporaryDirectory(prefix="fertile-research-fixture-") as tmp:
        path = Path(tmp) / "daily.csv"
        with path.open("w", newline="") as f:
            rows = csv.writer(f)
            rows.writerow(["date", "open", "high", "low", "close", "volume"])
            for i, session in enumerate(calendar):
                price = 100 + (i if i <= 25 else 50 - i)
                rows.writerow((session, price, price, price, price, 0 if i == 20 else 100_000))
        result = import_catalog(research_root(), path, instrument="FIXTURE.XNYS", source="Offline integration fixture",
                                kind="fixture", asset_type="stock")
    print(f"{result['instrument']}: {result['observations']} fixture sessions, {result['source_sha256']}")


if __name__ == "__main__":
    main()
