"""Synthetic F3 CLI and sealed WAL contract tests."""

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import textwrap
import unittest
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backup import capture_snapshot, verify
from backup_wal import WAL_ROOT, activate_wal, capture_wal, parse_products_setting, validate_wal


SOURCE = "synthetic"
PRODUCT = "fixture"
SEGMENT_ID = "00000000000000000001"
SEGMENT = Path(SOURCE, PRODUCT, SEGMENT_ID + ".wal")
MANIFEST = Path(SOURCE, PRODUCT + ".json")
WATERMARK = {SOURCE: {PRODUCT: "1"}}


def synthetic_segment():
    # Fixed artificial timestamps and values from the documented F1/F3 formats.
    event = dict(schema_version="1.0", channel="trades", source=SOURCE, product=PRODUCT,
                 instrument_id="SYNTH.XNAS", asset_kind="stock", venue="XNAS", exchange_ts="1700000000000000000",
                 receive_ts="1700000000000000001", first_available_ts="1700000000000000001",
                 epoch="1", seq="0", cursor="synthetic:1", revision="0", license="synthetic",
                 kind="trade", payload=dict(price="101.2300", quantity="5.00", side="unknown"))
    body = struct.pack("<QQ", 1, 1) + json.dumps(event).encode()
    length = struct.pack("<I", len(body))
    return b"FINWAL1\n" + length + hashlib.sha256(length + body).digest() + body


def synthetic_manifest(data):
    return dict(
        access=dict(source=SOURCE, product=PRODUCT, license="synthetic", display_live=True, record_local=True,
                    derive_non_display=True, backup_personal_drive=True),
        wal=dict(version=1, source=SOURCE, product=PRODUCT, sealed_through="1",
                 segments=[dict(id=SEGMENT_ID, path=SEGMENT.as_posix(), first="1", last="1",
                                bytes=str(len(data)), sha256=hashlib.sha256(data).hexdigest())]))


class BackupWal(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(dir="/tmp", prefix="fertile-f3-test-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.data = self.base / "data"
        self.stage = self.base / "stage"
        self.archive = self.base / "archive"
        self.binary = self.base / "finance-market-wal"
        self.stage.mkdir()
        self.segment = self.data / WAL_ROOT / SEGMENT
        self.segment.parent.mkdir(parents=True)
        self.payload = synthetic_segment()
        self.segment.write_bytes(self.payload)
        (self.segment.parent / "active.wal").write_bytes(b"unsealed synthetic contents")
        self.fixture_manifest = self.data / "test-manifest.json"
        self.write_manifest(synthetic_manifest(self.payload))
        self.binary.write_text("#!/usr/bin/env python3\n" + textwrap.dedent("""
            import json
            from pathlib import Path
            import sys

            assert sys.argv[1] == "--data-root"
            root, action, *args = sys.argv[2:]
            root = Path(root)
            (root / "cli.calls").open("a").write(action + "\\n")
            if action == "products":
                assert not args
                products = [{"source": "synthetic", "product": "fixture"}]
                if (root / "mixed-policy").exists():
                    products.append({"source": "synthetic", "product": "local-only"})
                print(json.dumps(products))
            elif action in ("manifest", "seal"):
                if args == ["--source", "synthetic", "--product", "local-only"]:
                    sys.exit("fixture product does not permit personal Drive backup")
                assert args == ["--source", "synthetic", "--product", "fixture"]
                print((root / "test-manifest.json").read_text())
            elif action == "restore":
                assert args[0] == "--manifest" and len(args) == 2
                item = json.loads(Path(args[1]).read_bytes())
                for segment in item["wal"]["segments"]:
                    assert (root / "finance-state/market-wal" / segment["path"]).is_file()
            else:
                sys.exit("incorrect fixture command")
        """))
        self.binary.chmod(0o700)

    def write_manifest(self, manifest):
        self.fixture_manifest.write_text(json.dumps(manifest))

    def test_capture_and_activate_sealed_prefix(self):
        self.assertEqual(capture_wal(self.binary, self.data, self.archive), WATERMARK)
        self.assertEqual((self.data / "cli.calls").read_text().splitlines(), ["products", "manifest"])
        files = {path.relative_to(self.archive) for path in self.archive.rglob("*") if path.is_file()}
        self.assertEqual(files, {Path("manifests") / MANIFEST, Path("segments") / SEGMENT})
        self.assertEqual(validate_wal(self.archive), WATERMARK)
        self.assertEqual(activate_wal(self.binary, self.stage, self.archive), WATERMARK)
        self.assertEqual((self.stage / WAL_ROOT / SEGMENT).read_bytes(), self.payload)
        self.assertEqual((self.stage / "cli.calls").read_text().splitlines(), ["restore"])

    def test_seal_requires_explicit_request(self):
        self.assertEqual(capture_wal(self.binary, self.data, self.archive, seal=True), WATERMARK)
        self.assertEqual((self.data / "cli.calls").read_text().splitlines(), ["products", "seal"])

    def test_rejects_product_without_cloud_permission(self):
        manifest = synthetic_manifest(self.payload)
        manifest["access"]["backup_personal_drive"] = False
        self.write_manifest(manifest)
        with self.assertRaisesRegex(ValueError, "lacks backup permission"):
            capture_wal(self.binary, self.data, self.archive)
        self.assertFalse((self.archive / "segments" / SEGMENT).exists())

    def test_mixed_products_require_explicit_authorized_selection(self):
        (self.data / "mixed-policy").touch()
        allowed = parse_products_setting("synthetic/fixture")
        self.assertEqual(capture_wal(self.binary, self.data, self.archive, products=allowed), WATERMARK)
        self.assertEqual((self.data / "cli.calls").read_text().splitlines(), ["products", "manifest"])
        denied = parse_products_setting("synthetic/fixture, synthetic/local-only")
        for selection in (denied, None):
            with self.subTest(selection=selection):
                other = Path(tempfile.mkdtemp(dir=self.base)) / "archive"
                with self.assertRaisesRegex(ValueError, "finance-market-wal manifest failed"):
                    capture_wal(self.binary, self.data, other, products=selection)
        missing = parse_products_setting("synthetic/missing")
        with self.assertRaisesRegex(ValueError, "product selection is empty or unavailable"):
            capture_wal(self.binary, self.data, self.base / "missing", products=missing)
        self.assertFalse((self.base / "missing").exists())

    def test_product_setting_rejects_ambiguous_entries(self):
        self.assertIsNone(parse_products_setting(None))
        self.assertEqual(parse_products_setting("one/first, two/second"), {("one", "first"), ("two", "second")})
        for value in ("", "one", "one/*", "one/first/", "one/first,", "one/first,one/first"):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "FERTILE_BACKUP_WAL_PRODUCTS"):
                parse_products_setting(value)

    def test_rejects_inaccurate_segment_set(self):
        changes = {"first": "2", "path": "../../../outside.wal", "last": "2",
                   "sha256": "0" * 64, "bytes": str(len(self.payload) + 1)}
        for field, value in changes.items():
            with self.subTest(field=field):
                manifest = synthetic_manifest(self.payload)
                manifest["wal"]["segments"][0][field] = value
                self.write_manifest(manifest)
                with self.assertRaisesRegex(ValueError, "F3 WAL"):
                    capture_wal(self.binary, self.data, self.base / ("archive_" + field))

    def test_restore_rejects_unlisted_files_and_corrupted_segments(self):
        capture_wal(self.binary, self.data, self.archive)
        original = self.archive / "segments" / SEGMENT
        unlisted = original.with_name("active.wal")
        unlisted.write_bytes(self.payload)
        with self.assertRaisesRegex(ValueError, "unexpected F3 WAL archive segments"):
            validate_wal(self.archive)
        unlisted.unlink()
        original.write_bytes(self.payload[:-1] + b"x")
        with self.assertRaisesRegex(ValueError, "digest mismatch"):
            activate_wal(self.binary, self.stage, self.archive)
        self.assertFalse((self.stage / WAL_ROOT).exists())

    def test_empty_sealed_prefix(self):
        manifest = synthetic_manifest(self.payload)
        manifest["wal"].update(sealed_through="0", segments=[])
        self.write_manifest(manifest)
        watermarks = {SOURCE: {PRODUCT: "0"}}
        self.assertEqual(capture_wal(self.binary, self.data, self.archive), watermarks)
        self.assertEqual(validate_wal(self.archive), watermarks)
        self.assertEqual(activate_wal(self.binary, self.stage, self.archive), watermarks)

    def test_remote_snapshot_only_includes_authorized_f3_seals(self):
        projection = self.data / "finance-state/market-projection"
        catalog_projection = self.data / "catalog/market-events"
        projection.mkdir()
        catalog_projection.mkdir(parents=True)
        (projection / "projection.sqlite3").write_bytes(b"synthetic projection index")
        (catalog_projection / "projected.parquet").write_bytes(b"synthetic projection")
        box = SimpleNamespace(root=self.base, data=self.data, trees={"finance-state": self.data / "finance-state",
                                                    "catalog": self.data / "catalog"},
                              exists=lambda _: False, db="test_db", source="replay", wal_bin=None,
                              wal_products="synthetic/fixture")
        with self.assertRaisesRegex(ValueError, "configure FERTILE_BACKUP_WAL_BIN"):
            capture_snapshot(box, self.stage, remote=True, recovery=True)
        box.wal_bin = self.binary
        snapshot = capture_snapshot(box, self.stage, remote=True, recovery=True)
        self.assertEqual(snapshot["wal"], WATERMARK)
        self.assertEqual(verify(self.stage), snapshot)
        self.assertTrue((self.stage / "wal/segments" / SEGMENT).is_file())
        self.assertFalse((self.stage / WAL_ROOT).exists())
        self.assertFalse((self.stage / "finance-state/market-projection").exists())
        self.assertFalse((self.stage / "catalog/market-events").exists())

    @unittest.skipUnless(os.environ.get("FERTILE_BACKUP_TEST_WAL"), "local F3 CLI binary not supplied")
    def test_local_f3_cli_restores_and_reads_synthetic_event(self):
        binary = os.environ["FERTILE_BACKUP_TEST_WAL"]
        capture_wal(self.binary, self.data, self.archive)
        self.assertEqual(activate_wal(binary, self.stage, self.archive), WATERMARK)
        destination = self.base / "recaptured"
        self.assertEqual(capture_wal(binary, self.stage, destination), WATERMARK)
        self.assertEqual(validate_wal(destination), WATERMARK)
        result = subprocess.check_output([binary, "--data-root", self.stage, "read",
                                          "--source", SOURCE, "--product", PRODUCT])
        event = json.loads(result)
        self.assertEqual(event["position"], "1")
        self.assertEqual(event["event"]["payload"], {"price": "101.2300", "quantity": "5.00", "side": "unknown"})

    @unittest.skipUnless(os.environ.get("FERTILE_BACKUP_TEST_WAL"), "local F3 CLI binary not supplied")
    def test_local_f3_cli_restores_empty_prefix(self):
        binary = os.environ["FERTILE_BACKUP_TEST_WAL"]
        manifest = synthetic_manifest(self.payload)
        manifest["wal"].update(sealed_through="0", segments=[])
        self.write_manifest(manifest)
        watermarks = {SOURCE: {PRODUCT: "0"}}
        self.assertEqual(capture_wal(self.binary, self.data, self.archive), watermarks)
        self.assertEqual(activate_wal(binary, self.stage, self.archive), watermarks)
        self.assertEqual(capture_wal(binary, self.stage, self.base / "recaptured"), watermarks)


if __name__ == "__main__":
    unittest.main(verbosity=2)
