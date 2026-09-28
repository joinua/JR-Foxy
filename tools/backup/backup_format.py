"""Shared backup format and offline SQLite checks. Python 3.11+."""
import contextlib
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import stat
import tempfile
import time
import zipfile

REQUIRED_TABLES = {"profiles", "admins", "clan_members", "candidates"}
NAME_RE = re.compile(r"JR-Foxy_\d{4}-\d{2}-\d{2}_\d{2}-\d{2}-\d{2}_UTC[+-]\d{4}_[0-9a-f]{8}\.zip\.age")
LIMITS = {"data/jrfoxy.db": 512*1024*1024, "config/.env": 1024*1024,
          "config/docker-compose.yml": 1024*1024, "manifest.json": 4*1024*1024,
          "RESTORE.md": 256*1024}

class BackupError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def now():
    return dt.datetime.now(dt.timezone.utc)


def read_json(path, default=None):
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8-sig"))


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".json-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def validate_record(record):
    name = record.get("filename", "")
    ident = record.get("backup_id", "")
    if not isinstance(name, str) or not NAME_RE.fullmatch(name):
        raise BackupError("FORMAT", "Invalid archive name")
    if not isinstance(ident, str) or not re.fullmatch(r"[0-9a-f]{32}", ident) or not name.endswith("_"+ident[:8]+".zip.age"):
        raise BackupError("FORMAT", "Invalid archive identifier")
    if type(record.get("size")) is not int or not 100 <= record["size"] <= 600*1024*1024:
        raise BackupError("FORMAT", "Invalid encrypted archive size")
    if not re.fullmatch(r"[0-9a-f]{64}", record.get("sha256", "")):
        raise BackupError("FORMAT", "Invalid archive checksum")
    if dt.datetime.fromisoformat(record["created_at_utc"]).utcoffset() is None:
        raise BackupError("FORMAT", "Archive timestamp lacks timezone")
    return record


def check_encrypted(path, record):
    validate_record(record)
    if not path.is_file() or path.stat().st_size != record["size"] or digest(path) != record["sha256"]:
        raise BackupError("CHECKSUM", "Encrypted archive verification failed")


def newest(records, count=6):
    return sorted(records, key=lambda r:(dt.datetime.fromisoformat(r["created_at_utc"]),r["backup_id"]), reverse=True)[:count]

def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()

def snapshot(source, destination):
    if not source.is_file() or source.stat().st_size == 0:
        raise RuntimeError("The source database is missing or empty")
    deadline = time.monotonic() + 180

    def progress(status, remaining, total):
        if time.monotonic() > deadline:
            raise TimeoutError("SQLite snapshot exceeded 180 seconds")

    with contextlib.closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as src:
        with contextlib.closing(sqlite3.connect(destination)) as dst:
            src.backup(dst, pages=256, progress=progress, sleep=0.1)
    # Finalize only the private copy's journal mode; never alter the live DB.
    with contextlib.closing(sqlite3.connect(destination)) as copy:
        copy.execute("PRAGMA journal_mode=DELETE").fetchone()
    for suffix in ("-wal", "-shm", "-journal"):
        if Path(str(destination) + suffix).exists():
            raise RuntimeError("Snapshot unexpectedly depends on a sidecar file")
    with contextlib.closing(sqlite3.connect(destination.as_uri() + "?mode=ro", uri=True)) as copy:
        if copy.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity_check failed")
        if copy.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("SQLite foreign_key_check failed")
        tables = {row[0] for row in copy.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        if not REQUIRED_TABLES.issubset(tables):
            raise RuntimeError("Expected JR-Foxy tables are missing")
        counts = {}
        for name in sorted(tables):
            quoted = '"' + name.replace('"', '""') + '"'
            counts[name] = copy.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0]
    return counts

def no_duplicate_keys(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RuntimeError("Duplicate JSON keys in manifest")
        result[key] = value
    return result

def verify_zip(bundle, work, record, recipient):
    with zipfile.ZipFile(bundle) as archive:
        infos = archive.infolist()
        if len(infos) != len(LIMITS) or {i.filename for i in infos} != set(LIMITS):
            raise RuntimeError("Unexpected, missing or duplicate ZIP entries")
        for info in infos:
            mode = stat.S_IFMT(info.external_attr >> 16)
            if mode not in (0, stat.S_IFREG) or info.is_dir() or info.flag_bits & 1:
                raise RuntimeError("ZIP contains an unsupported entry type")
            if info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED):
                raise RuntimeError("ZIP uses unsupported compression")
            if not 0 < info.file_size <= LIMITS[info.filename]:
                raise RuntimeError("ZIP entry exceeds the permitted size")
        manifest = json.loads(archive.read("manifest.json"), object_pairs_hook=no_duplicate_keys)
        if (manifest.get("format_version") != 1
                or manifest.get("producer") not in {"jr_foxy_first_backup.py", "jr-foxy-backup"}
                or manifest.get("recipient") != recipient):
            raise RuntimeError("Unexpected backup format or recipient")
        backup_id = manifest.get("backup_id", "")
        if not re.fullmatch(r"[0-9a-f]{32}", backup_id) or backup_id != record["backup_id"]:
            raise RuntimeError("Backup identifier does not match the selected archive")
        created = dt.datetime.fromisoformat(manifest["created_at_kyiv"])
        expected_name = "JR-Foxy_" + created.strftime("%Y-%m-%d_%H-%M-%S_UTC%z_") + backup_id[:8] + ".zip.age"
        if expected_name != record["filename"]:
            raise RuntimeError("Backup time does not match the filename")
        files = manifest.get("files", {})
        if set(files) != set(LIMITS) - {"manifest.json"}:
            raise RuntimeError("Manifest file list is incomplete")
        database = work / "verified.db"
        for name, metadata in files.items():
            size, sha = metadata.get("size"), metadata.get("sha256", "")
            if type(size) is not int or size != archive.getinfo(name).file_size:
                raise RuntimeError("Manifest and ZIP file sizes differ")
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise RuntimeError("Invalid file checksum in manifest")
            checksum = hashlib.sha256()
            copied = 0
            destination = database.open("xb") if name == "data/jrfoxy.db" else contextlib.nullcontext()
            with archive.open(name) as source, destination as output:
                while block := source.read(1024 * 1024):
                    copied += len(block)
                    if copied > size:
                        raise RuntimeError("ZIP entry expanded beyond its declared size")
                    checksum.update(block)
                    if output is not None:
                        output.write(block)
            if copied != size or checksum.hexdigest() != sha:
                raise RuntimeError("An internal file failed size or SHA256 verification")
    deadline = time.monotonic() + 120
    with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro&immutable=1", uri=True)) as db:
        db.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
        db.execute("PRAGMA trusted_schema=OFF")
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeError("SQLite integrity_check failed")
        if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("SQLite foreign_key_check failed")
        tables = {row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )}
        expected_counts = manifest["sqlite"]["table_counts"]
        if not REQUIRED_TABLES.issubset(tables) or tables != set(expected_counts):
            raise RuntimeError("Database tables differ from the manifest")
        if (manifest["sqlite"]["integrity_check"] != "ok"
                or manifest["sqlite"]["foreign_key_check"] != "ok"):
            raise RuntimeError("Server verification results are missing")
        for name in sorted(tables):
            quoted = '"' + name.replace('"', '""') + '"'
            count = db.execute("SELECT COUNT(*) FROM " + quoted).fetchone()[0]
            if type(expected_counts[name]) is not int or count != expected_counts[name]:
                raise RuntimeError("Database row counts differ from the manifest")
    return manifest
