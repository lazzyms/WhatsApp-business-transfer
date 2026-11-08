#!/usr/bin/env python3
"""Locate / extract / replace ChatStorage.sqlite inside an UNENCRYPTED iOS backup.

  inject_backup.py extract BACKUP_DIR OUT.sqlite [--domain D]
  inject_backup.py inject  BACKUP_DIR NEW.sqlite  [--domain D]

BACKUP_DIR is the per-device folder (contains Manifest.db and Manifest.plist),
or its parent; a single child with Manifest.db is auto-detected.
Originals (Manifest.db and every file replaced/removed) are saved to
<BACKUP_DIR>/../../_originals_<timestamp>/ before anything is modified.
"""
from __future__ import annotations

import argparse
import hashlib
import random
import plistlib
import shutil
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

DB_NAME = "ChatStorage.sqlite"
SIDECARS = (DB_NAME + "-wal", DB_NAME + "-shm")


class BackupError(Exception):
    pass


def find_backup(path: Path) -> Path:
    if (path / "Manifest.db").exists():
        return path
    kids = [p for p in path.glob("*/") if (p / "Manifest.db").exists()]
    if len(kids) == 1:
        return kids[0]
    raise BackupError(f"No single backup with Manifest.db under {path} (found {len(kids)}).")


def require_unencrypted(backup: Path) -> None:
    try:
        info = plistlib.loads((backup / "Manifest.plist").read_bytes())
    except (OSError, plistlib.InvalidFileException) as e:
        raise BackupError(f"Cannot read Manifest.plist: {e}")
    if info.get("IsEncrypted"):
        raise BackupError(
            "Backup is encrypted. Run `idevicebackup2 encryption off` (device must be "
            "connected; it will ask for the passcode), then take a new backup.")


def file_path(backup: Path, file_id: str) -> Path:
    return backup / file_id[:2] / file_id


def whatsapp_files(manifest: sqlite3.Connection, domain: str | None) -> dict[str, tuple[str, str]]:
    """Return {relativePath: (fileID, domain)} for ChatStorage.sqlite and sidecars."""
    rows = manifest.execute(
        "SELECT fileID, domain, relativePath FROM Files WHERE relativePath IN (?,?,?)",
        (DB_NAME, *SIDECARS)).fetchall()
    domains = {d for _, d, rel in rows if rel == DB_NAME}
    if domain:
        domains = {d for d in domains if d == domain}
    else:  # never fall back to consumer WhatsApp: only a Business domain, or --domain
        found = sorted(domains)
        domains = {d for d in domains if "SMB" in d or "Business" in d}
        if not domains:
            raise BackupError(
                f"No WhatsApp Business data in this backup (only found: {found or 'nothing'}). "
                "WhatsApp Business is not installed/registered on the iPhone, so the backup has "
                "nothing to replace. Install it, register the number, then take a new backup.")
    if len(domains) != 1:
        raise BackupError(
            f"Expected exactly one WhatsApp ChatStorage.sqlite domain, found {sorted(domains) or 'none'}. "
            "Is WhatsApp Business installed and registered on the iPhone? "
            "Use --domain to choose one explicitly.")
    d = domains.pop()
    return {rel: (fid, dom) for fid, dom, rel in rows if dom == d}


def open_manifest(backup: Path) -> sqlite3.Connection:
    return sqlite3.connect(backup / "Manifest.db")


def extract(backup: Path, out: Path, domain: str | None = None) -> str:
    """Write a clean single-file copy of the device's ChatStorage.sqlite to `out`."""
    require_unencrypted(backup)
    with open_manifest(backup) as m:
        files = whatsapp_files(m, domain)
    if DB_NAME not in files:
        raise BackupError("ChatStorage.sqlite not in backup.")
    with tempfile.TemporaryDirectory() as tmp:
        for rel, (fid, _) in files.items():
            src = file_path(backup, fid)
            if src.exists():
                shutil.copy2(src, Path(tmp) / rel)
        conn = sqlite3.connect(Path(tmp) / DB_NAME)
        dst = sqlite3.connect(out)
        conn.backup(dst)
        dst.close()
        conn.close()
    return files[DB_NAME][1]


def _update_record(blob: bytes, size: int, digest: bytes) -> bytes:
    plist = plistlib.loads(blob)
    for obj in plist["$objects"]:
        if isinstance(obj, dict) and "Size" in obj:
            obj["Size"] = size
            obj["Digest"] = digest
            obj["LastModified"] = int(time.time())
            return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
    raise BackupError("Manifest file record has no MBFile entry (unexpected format).")


def _clone_record(blob: bytes, size: int, digest: bytes | None, mode: int) -> bytes:
    """Build a Manifest 'file' record by cloning an existing one (keeps UserID, ProtectionClass...)."""
    plist = plistlib.loads(blob)
    now = int(time.time())
    for obj in plist["$objects"]:
        if isinstance(obj, dict) and "Size" in obj:
            obj.update(Size=size, Mode=mode, LastModified=now, LastStatusChange=now, Birth=now,
                       InodeNumber=random.getrandbits(40))
            if digest is None:
                obj.pop("Digest", None)
            else:
                obj["Digest"] = digest
            return plistlib.dumps(plist, fmt=plistlib.FMT_BINARY)
    raise BackupError("Manifest file record has no MBFile entry (unexpected format).")


def add_media(backup: Path, m: sqlite3.Connection, domain: str, template_blob: bytes,
              media: list[tuple[Path, str]]) -> None:
    """Copy media files into the backup and register them (plus parent dirs) in Manifest.db."""
    known = {r[0] for r in m.execute("SELECT relativePath FROM Files WHERE domain=?", (domain,))}

    def register(rel: str, flags: int, blob: bytes) -> str:
        fid = hashlib.sha1(f"{domain}-{rel}".encode()).hexdigest()
        m.execute("DELETE FROM Files WHERE fileID=?", (fid,))
        m.execute("INSERT INTO Files (fileID, domain, relativePath, flags, file) VALUES (?,?,?,?,?)",
                  (fid, domain, rel, flags, blob))
        return fid

    for src, rel in media:
        parts = rel.split("/")
        for i in range(1, len(parts)):  # parent directories
            d = "/".join(parts[:i])
            if d not in known:
                register(d, 2, _clone_record(template_blob, 0, None, 0o40755))
                known.add(d)
        data = src.read_bytes()
        fid = register(rel, 1, _clone_record(template_blob, len(data), hashlib.sha1(data).digest(), 0o100644))
        dest = file_path(backup, fid)
        dest.parent.mkdir(exist_ok=True)
        dest.write_bytes(data)
        known.add(rel)


def inject(backup: Path, new_db: Path, domain: str | None = None,
           media: list[tuple[Path, str]] | None = None,
           originals_root: Path | None = None) -> Path:
    """Replace ChatStorage.sqlite (and add `media` files) in the backup. Returns the originals folder.

    Originals go to `originals_root` (default: two levels above the backup folder). It must
    sit OUTSIDE the backup tree, or find_backup() would see a second Manifest.db."""
    require_unencrypted(backup)
    data = new_db.read_bytes()
    check = sqlite3.connect(f"file:{new_db}?mode=ro", uri=True)
    if check.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise BackupError("New database fails integrity_check; not injecting.")
    check.close()

    manifest_path = backup / "Manifest.db"
    with open_manifest(backup) as m:
        files = whatsapp_files(m, domain)
    if DB_NAME not in files:
        raise BackupError("ChatStorage.sqlite not in backup.")

    # 1. Save originals before touching anything.
    saved = (originals_root or backup.parent.parent) / f"_originals_{time.strftime('%Y%m%d-%H%M%S')}"
    saved.mkdir()
    shutil.copy2(manifest_path, saved / "Manifest.db")
    for rel, (fid, _) in files.items():
        src = file_path(backup, fid)
        if src.exists():
            shutil.copy2(src, saved / f"{fid}__{rel}")

    # 2. Replace the DB, drop stale WAL/SHM (they belong to the old DB).
    fid, dom = files[DB_NAME]
    target = file_path(backup, fid)
    target.parent.mkdir(exist_ok=True)
    target.write_bytes(data)
    digest = hashlib.sha1(data).digest()

    m = open_manifest(backup)
    try:
        with m:
            (blob,) = m.execute("SELECT file FROM Files WHERE fileID=?", (fid,)).fetchone()
            m.execute("UPDATE Files SET file=? WHERE fileID=?",
                      (_update_record(blob, len(data), digest), fid))
            if media:
                add_media(backup, m, dom, blob, media)
            for rel in SIDECARS:
                if rel in files:
                    side_id = files[rel][0]
                    m.execute("DELETE FROM Files WHERE fileID=?", (side_id,))
                    file_path(backup, side_id).unlink(missing_ok=True)
    finally:
        m.close()

    # 3. Read back and verify.
    with open_manifest(backup) as m:
        (blob,) = m.execute("SELECT file FROM Files WHERE fileID=?", (fid,)).fetchone()
    rec = next(o for o in plistlib.loads(blob)["$objects"] if isinstance(o, dict) and "Size" in o)
    on_disk = target.read_bytes()
    if rec["Size"] != len(on_disk) or rec["Digest"] != hashlib.sha1(on_disk).digest():
        raise BackupError(f"Verification failed; restore originals from {saved}")
    return saved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", choices=("extract", "inject"))
    ap.add_argument("backup", type=Path)
    ap.add_argument("db", type=Path)
    ap.add_argument("--domain")
    a = ap.parse_args()
    try:
        b = find_backup(a.backup)
        if a.cmd == "extract":
            print(f"Extracted from domain {extract(b, a.db, a.domain)}")
        else:
            print(f"Injected. Originals saved in {inject(b, a.db, a.domain)}")
    except (BackupError, sqlite3.Error, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
