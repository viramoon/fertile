"""Exercise rclone crypt using only synthetic data and a disposable local remote."""

import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from backup_crypt import CryptError, CryptRemote


class CryptRemoteTest(unittest.TestCase):
    def setUp(self):
        self.rclone = os.getenv("FERTILE_TEST_RCLONE", "rclone")
        temp = tempfile.TemporaryDirectory(prefix="fertile-crypt-test-", dir="/tmp")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.raw = self.root / "raw"
        self.config = self.root / "test-rclone.conf"
        self.password = self.generate_password()
        self.salt = self.generate_password()
        self.write_config()
        self.remote = CryptRemote(self.rclone, self.config, "vault:")

    def generate_password(self):
        return subprocess.run(
            [self.rclone, "obscure", "--config", str(self.config), "-"],
            input=secrets.token_urlsafe(32) + "\n", env={"PATH": os.defpath, "LC_ALL": "C.UTF-8"},
            text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
        ).stdout.strip()

    def write_config(self, *, salt=True, password=None, options="", backend="type = local"):
        self.config.write_text(
            f"[raw]\n{backend}\n[vault]\ntype = crypt\nremote = raw:{self.raw}\n"
            f"password = {password or self.password}\n"
            + (f"password2 = {self.salt}\n" if salt else "") + options,
            encoding="utf-8",
        )
        self.config.chmod(0o600)

    def test_encrypts_object_content_and_file_and_directory_names(self):
        plaintext = b"synthetic-backup-record:test-rclone-crypt\n"
        src = self.root / "source"
        src.mkdir()
        (src / "readable-object.txt").write_bytes(plaintext)
        with patch.dict(os.environ, {"RCLONE_CONFIG_VAULT_NO_DATA_ENCRYPTION": "true",
                                     "RCLONE_CRYPT_DIRECTORY_NAME_ENCRYPTION": "false",
                                     "RCLONE_CONFIG_VAULT_FILENAME_ENCRYPTION": "off"}):
            self.remote.upload_tree(src, "readable-directory")
        self.assertEqual(self.remote.list_dirs(), ["readable-directory"])
        self.assertEqual(self.remote.list_files("readable-directory"), {"readable-object.txt": len(plaintext)})
        self.assertEqual(len(list(self.raw.rglob("*"))), 2)
        for entry in self.raw.rglob("*"):
            self.assertNotIn("readable-", entry.name)
            if entry.is_file():
                self.assertNotIn(plaintext, entry.read_bytes())
        restored = self.root / "restored"
        self.remote.download_tree("readable-directory", restored)
        self.assertEqual((restored / "readable-object.txt").read_bytes(), plaintext)

    def test_immutable_upload_and_idempotent_cleanup(self):
        self.assertEqual(self.remote.list_dirs(), [])
        self.assertEqual(self.remote.list_files("missing"), {})
        src = self.root / "marker"
        dst = self.root / "restored-marker"
        src.write_bytes(b"first publish")
        self.remote.upload(src, "points/first/commit")
        src.write_bytes(b"this larger version cannot overwrite")
        with self.assertRaises(CryptError):
            self.remote.upload(src, "points/first/commit")
        self.remote.download("points/first/commit", dst)
        self.assertEqual(dst.read_bytes(), b"first publish")
        self.remote.delete("points/first/commit")
        self.remote.delete("points/first/commit")
        self.assertEqual(self.remote.list_files("points/first"), {})
        self.remote.purge("points/first")
        self.remote.purge("points/first")
        self.assertEqual(self.remote.list_dirs("points"), [])
        with self.assertRaises(CryptError):
            self.remote.purge("")

    def test_requires_configured_password_and_salt(self):
        self.config.unlink()
        with self.assertRaisesRegex(CryptError, "configuration is missing") as missing:
            CryptRemote(self.rclone, self.config, "vault:")
        self.assertEqual(missing.exception.code, "key_missing")
        self.write_config(salt=False)
        with self.assertRaisesRegex(CryptError, "password or salt is missing") as missing:
            CryptRemote(self.rclone, self.config, "vault:")
        self.assertEqual(missing.exception.code, "key_missing")

    def test_rejects_plaintext_backends_and_encryption_downgrades(self):
        with self.assertRaisesRegex(CryptError, "requires rclone crypt"):
            CryptRemote(self.rclone, self.config, "raw:")
        for options in ("filename_encryption = off\n", "filename_encryption = obfuscate\n",
                        "directory_name_encryption = false\n", "no_data_encryption = true\n"):
            with self.subTest(options=options):
                self.write_config(options=options)
                with self.assertRaisesRegex(CryptError, "full encryption"):
                    CryptRemote(self.rclone, self.config, "vault:")

    def test_rejects_config_permissions_and_symlinks(self):
        self.config.chmod(0o640)
        with self.assertRaisesRegex(CryptError, "owner-only regular file"):
            self.remote.list_dirs()
        self.config.chmod(0o600)
        alias = self.root / "config-alias"
        alias.symlink_to(self.config)
        with self.assertRaisesRegex(CryptError, "owner-only regular file"):
            CryptRemote(self.rclone, alias, "vault:")

    def test_wrong_key_listing_fails(self):
        src = self.root / "marker"
        src.write_bytes(b"synthetic private payload")
        prefixed = CryptRemote(self.rclone, self.config, "vault:isolated-root")
        prefixed.upload(src, "points/first/commit")
        raw_names = set(self.raw.rglob("*"))
        self.write_config(password=self.generate_password())
        for target in ("vault:", "vault:isolated-root"):
            with self.subTest(target=target):
                other_key = CryptRemote(self.rclone, self.config, target)
                with self.assertRaisesRegex(CryptError, "operation failed"):
                    other_key.list_files("points")
                with self.assertRaisesRegex(CryptError, "operation failed"):
                    other_key.upload(src, "points/second/commit")
        self.assertEqual(set(self.raw.rglob("*")), raw_names)

    def test_refuses_configuration_changes_mid_operation(self):
        src = self.root / "marker"
        src.write_bytes(b"synthetic private payload")
        self.write_config(password=self.generate_password())
        with self.assertRaisesRegex(CryptError, "configuration changed"):
            self.remote.upload(src, "points/first/commit")
        self.assertFalse(self.raw.exists())

    def test_insufficient_local_capacity(self):
        # Initially the destination directory has yet to be created.
        self.remote.capacity(1)
        free = shutil.disk_usage(self.root).free
        with self.assertRaisesRegex(CryptError, "capacity") as insufficient:
            self.remote.capacity(free + 1024 ** 3)
        self.assertEqual(insufficient.exception.code, "capacity")
        self.assertFalse(self.raw.exists())

    def test_redacts_subprocess_output(self):
        stub = self.root / "rclone-failure"
        stub.write_text("#!/bin/sh\nprintf 'test-secret-marker' >&2\nexit 1\n", encoding="utf-8")
        stub.chmod(0o700)
        failing = CryptRemote(str(stub), self.config, "vault:")
        with self.assertRaises(CryptError) as failure:
            failing.list_dirs()
        self.assertNotIn("test-secret-marker", str(failure.exception))
        self.assertEqual(str(failure.exception), "Encrypted remote operation failed (lsjson).")


if __name__ == "__main__":
    unittest.main()
