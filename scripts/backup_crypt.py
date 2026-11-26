"""Encrypted backup transport through a deployment-local rclone profile."""

from configparser import ConfigParser, Error as ConfigError
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess


NAMED_REMOTE = re.compile(r"([A-Za-z0-9_][A-Za-z0-9_-]*):(.*)")
CRYPT_OPTIONS = {
    "type", "remote", "password", "password2", "description", "filename_encryption",
    "directory_name_encryption", "no_data_encryption", "filename_encoding", "strict_names",
}
DRIVE_OPTIONS = {
    "type", "token", "scope", "client_id", "client_secret", "root_folder_id", "use_trash",
    "team_drive", "description",
}


class CryptError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def named_remote(value):
    match = NAMED_REMOTE.fullmatch(value)
    if not match:
        raise CryptError("configuration", "Set a named rclone remote.")
    return match.groups()


def relative_path(value):
    if (not isinstance(value, str) or not value or any(part in {"", ".", ".."} for part in value.split("/"))
            or any(character in value for character in ":\\\r\n\0")):
        raise CryptError("configuration", "Invalid relative backup path.")
    return value


def private_config(path):
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise CryptError("key_missing", "The local rclone configuration is missing.") from None
    except OSError:
        raise CryptError("configuration", "Cannot access the local rclone configuration.") from None
    if (not path.is_absolute() or not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) not in {0o400, 0o600}):
        raise CryptError("configuration", "The rclone configuration needs an owner-only regular file.")


def load_config(path):
    private_config(path)
    parser = ConfigParser(interpolation=None, delimiters=("=",), empty_lines_in_values=False)
    parser.optionxform = str
    try:
        contents = path.read_text(encoding="utf-8")
        if any(line[:1].isspace() and line.strip() and not line.lstrip().startswith(("#", ";"))
               for line in contents.splitlines()):
            raise ValueError("multiline configuration")
        parser.read_string(contents)
        if parser.defaults():
            raise ValueError("default configuration")
    except (OSError, ValueError, ConfigError):
        raise CryptError("configuration", "Invalid local rclone configuration.") from None
    return parser


class CryptRemote:
    def __init__(self, binary: str, config: Path, remote: str):
        self.binary = binary
        self.config = config
        self.name, self.prefix = named_remote(remote)
        if self.prefix:
            relative_path(self.prefix)
        crypt, storage, base = self._profiles()
        self.crypt_profile = dict(crypt)
        self.storage_profile = {key: value for key, value in storage.items() if key != "token"}
        self.namespace_checked = False
        self.local_root = None
        if storage.get("type") == "local":
            path = Path(base)
            if not path.is_absolute() or path == Path("/") or path != path.resolve():
                raise CryptError("configuration", "A local test remote needs a dedicated absolute directory.")
            self.local_root = path
        elif base:
            relative_path(base)

    def _profiles(self):
        profile = load_config(self.config)
        if not profile.has_section(self.name):
            raise CryptError("configuration", "Named rclone crypt remote is missing.")
        crypt = profile[self.name]
        if (crypt.get("type") != "crypt" or not crypt.keys() <= CRYPT_OPTIONS
                or crypt.get("filename_encryption", "standard") != "standard"
                or crypt.get("directory_name_encryption", "true") != "true"
                or crypt.get("no_data_encryption", "false") != "false"
                or crypt.get("strict_names", "true") != "true"):
            raise CryptError("configuration", "The backup remote requires rclone crypt with full encryption.")
        if not crypt.get("password") or not crypt.get("password2"):
            raise CryptError("key_missing", "The rclone crypt password or salt is missing.")
        backend, base = named_remote(crypt.get("remote", ""))
        if not profile.has_section(backend):
            raise CryptError("configuration", "The underlying rclone remote is missing.")
        storage = profile[backend]
        if storage.get("type") == "local" and storage.keys() <= {"type", "description"}:
            return crypt, storage, base
        if (storage.get("type") != "drive" or not storage.keys() <= DRIVE_OPTIONS
                or storage.get("team_drive") or storage.get("scope", "drive") not in {"drive", "drive.file"}):
            raise CryptError("configuration", "The crypt remote requires a private Drive or local test backend.")
        if not storage.get("token"):
            raise CryptError("key_missing", "The local Google Drive OAuth token is missing.")
        return crypt, storage, base

    def _remote(self, relative=""):
        if relative:
            relative_path(relative)
        path = "/".join(part for part in (self.prefix, relative) if part)
        return f"{self.name}:{path}"

    def _run(self, command, *paths, flags=(), missing=()):
        crypt, storage, _ = self._profiles()
        if (dict(crypt) != self.crypt_profile
                or {key: value for key, value in storage.items() if key != "token"} != self.storage_profile):
            raise CryptError("configuration", "The encrypted remote configuration changed during the operation.")
        if not self.namespace_checked:
            # A wrong key can make an encrypted prefix appear missing. List
            # the dedicated wrapped root before using or creating a prefix.
            self._invoke("lsjson", f"{self.name}:", flags=("--no-modtime", "--no-mimetype"), missing=(3,))
            self.namespace_checked = True
        return self._invoke(command, *paths, flags=flags, missing=missing)

    def _invoke(self, command, *paths, flags, missing):
        # Explicit arguments and a minimal environment prevent inherited rclone
        # settings from selecting credentials, disabling crypt, or logging names.
        env = {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C.UTF-8"}
        try:
            result = subprocess.run(
                [self.binary, command, "--config", self.config, "--crypt-strict-names",
                 "--contimeout", "15s", "--timeout", "2m", "--low-level-retries", "3", "--retries", "2",
                 *flags, "--", *paths],
                env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            raise CryptError("remote_error", "Cannot start the configured rclone executable.") from None
        if result.returncode in missing:
            return None
        if result.returncode:
            raise CryptError("remote_error", f"Encrypted remote operation failed ({command}).")
        return result.stdout

    def _list(self, relative, *flags):
        data = self._run("lsjson", self._remote(relative), flags=("--no-modtime", "--no-mimetype", *flags),
                         missing=(3,))
        try:
            rows = [] if data is None else json.loads(data)
            if not isinstance(rows, list):
                raise ValueError("invalid listing")
            names = set()
            for row in rows:
                name = row["Path"]
                relative_path(name)
                if name in names:
                    raise ValueError("duplicate remote entry")
                names.add(name)
            return rows
        except (ValueError, TypeError, KeyError, CryptError):
            raise CryptError("remote_listing", "Invalid or duplicate encrypted remote entries.") from None

    def list_dirs(self, relative=""):
        return sorted(row["Path"] for row in self._list(relative, "--dirs-only"))

    def list_files(self, relative):
        files = {}
        for row in self._list(relative, "--recursive", "--files-only"):
            size = row.get("Size")
            if type(size) is not int or size < 0:
                raise CryptError("remote_listing", "An encrypted remote object has an invalid size.")
            files[row["Path"]] = size
        return files

    def upload(self, local: Path, relative: str):
        self._run("copyto", local.absolute(), self._remote(relative_path(relative)), flags=("--immutable",))

    def upload_tree(self, local: Path, relative: str):
        self._run("copy", local.absolute(), self._remote(relative_path(relative)), flags=("--immutable",))

    def download(self, relative: str, local: Path):
        self._run("copyto", self._remote(relative_path(relative)), local.absolute())

    def download_tree(self, relative: str, local: Path):
        self._run("copy", self._remote(relative_path(relative)), local.absolute())

    def delete(self, relative: str):
        self._run("deletefile", self._remote(relative_path(relative)), missing=(4,))

    def purge(self, relative: str):
        path = self._remote(relative_path(relative))
        # Local rclone purge can return a generic error for a missing directory.
        # Check its type and existence before asking the backend to purge it.
        data = self._run("lsjson", path, flags=("--stat",), missing=(3, 4))
        if data is None:
            return
        try:
            is_directory = json.loads(data).get("IsDir") is True
        except (ValueError, AttributeError):
            is_directory = False
        if not is_directory:
            raise CryptError("remote_listing", "The encrypted purge target must be a directory.")
        self._run("purge", path, missing=(3,))

    def capacity(self, required_bytes: int):
        try:
            if self.local_root is not None:
                directory = self.local_root
                while not directory.exists():
                    directory = directory.parent
                free = shutil.disk_usage(directory).free
            else:
                data = self._run("about", self._remote(), flags=("--json",))
                free = json.loads(data).get("free")
        except (OSError, ValueError, AttributeError):
            raise CryptError("capacity", "Cannot check encrypted remote capacity.") from None
        if type(free) is not int or free < required_bytes:
            raise CryptError("capacity", "Insufficient or unknown encrypted remote capacity.")
