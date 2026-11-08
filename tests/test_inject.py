import hashlib
import plistlib
import sqlite3

import pytest

from conftest import CONSUMER, SMB, make_backup
from wa_migrate import inject_backup, transform_db


def test_inject_replaces_only_the_business_database(msgstore, template, tmp_path):
    new_db = tmp_path / "new.sqlite"
    transform_db.convert(msgstore, template, new_db)
    consumer_bytes = b"consumer data stays untouched"
    work = tmp_path / "work"
    backup = make_backup(work / "ios_backup", {SMB: template.read_bytes(), CONSUMER: consumer_bytes})

    saved = inject_backup.inject(inject_backup.find_backup(work / "ios_backup"), new_db, originals_root=work)

    # the injected file is byte-identical and the Manifest size/digest match it
    manifest = sqlite3.connect(backup / "Manifest.db")
    fid, blob = manifest.execute("SELECT fileID, file FROM Files WHERE domain=?", (SMB,)).fetchone()
    on_disk = (backup / fid[:2] / fid).read_bytes()
    record = next(o for o in plistlib.loads(blob)["$objects"] if isinstance(o, dict) and "Size" in o)
    assert on_disk == new_db.read_bytes()
    assert record["Size"] == len(on_disk) and record["Digest"] == hashlib.sha1(on_disk).digest()
    # consumer WhatsApp is left alone
    cfid = manifest.execute("SELECT fileID FROM Files WHERE domain=?", (CONSUMER,)).fetchone()[0]
    assert (backup / cfid[:2] / cfid).read_bytes() == consumer_bytes
    # originals saved outside the backup tree, so the backup is still found uniquely
    assert (saved / "Manifest.db").exists() and saved.parent == work
    assert inject_backup.find_backup(work / "ios_backup") == backup


def test_extract_roundtrip(msgstore, template, tmp_path):
    new_db = tmp_path / "new.sqlite"
    transform_db.convert(msgstore, template, new_db)
    backup = make_backup(tmp_path / "ios_backup", {SMB: new_db.read_bytes()})
    out = tmp_path / "extracted.sqlite"
    assert inject_backup.extract(backup, out) == SMB
    assert sqlite3.connect(out).execute("SELECT count(*) FROM ZWAMESSAGE").fetchone() == (5,)


def test_never_falls_back_to_consumer_whatsapp(template, tmp_path):
    backup = make_backup(tmp_path / "ios_backup", {CONSUMER: template.read_bytes()})
    with pytest.raises(inject_backup.BackupError, match="No WhatsApp Business data"):
        inject_backup.extract(backup, tmp_path / "x.sqlite")


def test_encrypted_backup_is_refused(template, tmp_path):
    backup = make_backup(tmp_path / "ios_backup", {SMB: template.read_bytes()})
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": True}))
    with pytest.raises(inject_backup.BackupError, match="encrypted"):
        inject_backup.extract(backup, tmp_path / "x.sqlite")
