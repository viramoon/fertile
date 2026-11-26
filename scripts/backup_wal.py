"""Copy only the immutable files named by finance-market-wal manifests."""

import hashlib
import json
from pathlib import Path
import re
import shutil
import subprocess


WAL_ROOT = Path("finance-state/market-wal")
IDENTIFIER = re.compile(r"[a-z0-9][a-z0-9._-]{0,63}")
DECIMAL = re.compile(r"0|[1-9][0-9]{0,19}")
SHA256 = re.compile(r"[0-9a-f]{64}")
POLICY_FLAGS = ("display_live", "record_local", "derive_non_display", "backup_personal_drive")


def require(condition, message):
    if not condition:
        raise ValueError(message)


def identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value)


def parse_products_setting(value):
    """Parse the optional exact F3 product selection."""
    if value is None:
        return None
    products = set()
    for entry in value.split(","):
        source, _, product = entry.strip().partition("/")
        require(identifier(source) and identifier(product), "invalid FERTILE_BACKUP_WAL_PRODUCTS entry")
        require((source, product) not in products, "duplicate FERTILE_BACKUP_WAL_PRODUCTS entry")
        products.add((source, product))
    return products


def position(value):
    require(isinstance(value, str) and DECIMAL.fullmatch(value), "invalid F3 WAL integer")
    result = int(value)
    require(result < 2**64, "F3 WAL integer is out of range")
    return result


def cli(binary, data_root, action, *args):
    require(Path(binary).is_absolute(), "F3 WAL executable must use an absolute path")
    require(data_root.is_absolute() and data_root == data_root.resolve() and data_root.is_dir(),
            "F3 WAL data root must be a concrete directory")
    try:
        return subprocess.run([binary, "--data-root", data_root, action, *args], check=True,
                              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=300).stdout
    except (OSError, subprocess.SubprocessError) as error:
        raise ValueError(f"finance-market-wal {action} failed") from error


def parse_json(content):
    try:
        return json.loads(content)
    except (UnicodeDecodeError, ValueError) as error:
        raise ValueError("invalid F3 WAL JSON") from error


def product_key(entry):
    require(isinstance(entry, dict) and identifier(entry.get("source")) and identifier(entry.get("product")),
            "invalid F3 WAL source or product")
    return entry["source"], entry["product"]


def manifest_segments(manifest, expected_key):
    require(isinstance(manifest, dict), "invalid F3 WAL manifest")
    access, wal = manifest.get("access"), manifest.get("wal")
    require(product_key(access) == product_key(wal) == expected_key, "F3 WAL product mismatch")
    require(identifier(access.get("license")) and all(type(access.get(flag)) is bool for flag in POLICY_FLAGS)
            and access["record_local"] and access["backup_personal_drive"],
            "F3 WAL product lacks backup permission")
    segments = wal.get("segments")
    require(type(wal.get("version")) is int and wal["version"] == 1 and isinstance(segments, list),
            "unsupported F3 WAL manifest format")
    sealed_through = position(wal.get("sealed_through"))
    previous_last = 0
    files = {}
    for segment in segments:
        require(isinstance(segment, dict), "invalid F3 WAL segment")
        first, last, size = (position(segment.get(key)) for key in ("first", "last", "bytes"))
        require(first == previous_last + 1 and last >= first, "non-contiguous F3 WAL segment range")
        require(8 < size <= 64 * 1024 * 1024, "invalid F3 WAL segment size")
        segment_id = f"{first:020d}"
        path = Path(*expected_key, f"{segment_id}.wal")
        require(segment.get("id") == segment_id and segment.get("path") == path.as_posix(),
                "invalid F3 WAL segment path")
        sha256 = segment.get("sha256")
        require(isinstance(sha256, str) and SHA256.fullmatch(sha256), "invalid F3 WAL segment digest")
        files[path] = (size, sha256)
        previous_last = last
    require(previous_last == sealed_through, "F3 WAL segments do not reach the sealed watermark")
    return files


def check_file(path, size):
    require(path.is_file() and path == path.resolve() and path.stat().st_size == size,
            "F3 WAL segment is missing or has the wrong size")


def verify_segment(path, size, sha256):
    check_file(path, size)
    with path.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    require(digest == sha256, "F3 WAL segment digest mismatch")


def capture_wal(binary, data_root, destination, *, seal=False, products=None):
    """Snapshot the F3 sealed prefixes and return per-product decimal watermarks."""
    wal_root = data_root / WAL_ROOT
    require(wal_root == wal_root.resolve(), "F3 WAL root must not contain symlinks")
    entries = parse_json(cli(binary, data_root, "products"))
    require(isinstance(entries, list), "invalid F3 WAL products list")
    discovered = {product_key(entry) for entry in entries}
    require(len(discovered) == len(entries), "duplicate F3 WAL product")
    if products is not None:
        require(products and products <= discovered, "configured F3 WAL product selection is empty or unavailable")
        discovered = products
    destination.mkdir()
    (destination / "manifests").mkdir()
    (destination / "segments").mkdir()
    watermarks = {}
    for source, product in sorted(discovered):
        content = cli(binary, data_root, "seal" if seal else "manifest", "--source", source, "--product", product)
        manifest = parse_json(content)
        files = manifest_segments(manifest, (source, product))
        for path, (size, sha256) in files.items():
            original = wal_root / path
            target = destination / "segments" / path
            check_file(original, size)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(original, target)
            verify_segment(target, size, sha256)
        path = destination / "manifests" / source / (product + ".json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        watermarks.setdefault(source, {})[product] = manifest["wal"]["sealed_through"]
    return watermarks


def archive_inventory(directory):
    require(directory.is_dir() and directory == directory.resolve(), "invalid F3 WAL archive directory")
    manifests, segments = (directory / name for name in ("manifests", "segments"))
    require(all(path.is_dir() and not path.is_symlink() for path in (manifests, segments))
            and set(directory.iterdir()) == {manifests, segments}, "invalid F3 WAL archive layout")
    inventory = {}
    for root in (manifests, segments):
        files = set()
        for path in root.rglob("*"):
            require(not path.is_symlink() and (path.is_file() or path.is_dir()), "unsafe F3 WAL archive entry")
            if path.is_file():
                files.add(path.relative_to(root))
        inventory[root.name] = files
    return inventory


def read_archive(directory):
    directory = directory.absolute()
    inventory = archive_inventory(directory)
    segment_files = set()
    watermarks = {}
    for path in sorted(inventory["manifests"]):
        require(len(path.parts) == 2 and path.suffix == ".json", "invalid F3 WAL manifest path")
        source, product = path.parts[0], path.stem
        manifest = parse_json((directory / "manifests" / path).read_bytes())
        for segment, (size, sha256) in manifest_segments(manifest, (source, product)).items():
            verify_segment(directory / "segments" / segment, size, sha256)
            segment_files.add(segment)
        watermarks.setdefault(source, {})[product] = manifest["wal"]["sealed_through"]
    require(segment_files == inventory["segments"], "unexpected F3 WAL archive segments")
    return watermarks, inventory


def validate_wal(directory):
    """Validate the F3 archive without the finance executable."""
    watermarks, _ = read_archive(directory)
    return watermarks


def activate_wal(binary, data_root, directory):
    """Install the validated F3 prefixes in an isolated restore staging root."""
    watermarks, inventory = read_archive(directory)
    require(data_root.is_absolute() and data_root == data_root.resolve() and data_root.is_dir(),
            "F3 WAL restore needs a concrete staging data root")
    wal_root = data_root / WAL_ROOT
    require(wal_root == wal_root.resolve() and not wal_root.exists(), "F3 WAL staging directory is occupied")
    wal_root.mkdir(parents=True)
    for path in sorted(inventory["segments"]):
        target = wal_root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(directory / "segments" / path, target)
    for path in sorted(inventory["manifests"]):
        cli(binary, data_root, "restore", "--manifest", directory / "manifests" / path)
    return watermarks
