"""Synthetic fixtures only: no real chats ever belong in this repo."""
import hashlib
import plistlib
import sqlite3
from pathlib import Path

import pytest

SMB = "AppDomainGroup-group.net.whatsapp.WhatsAppSMB.shared"
CONSUMER = "AppDomainGroup-group.net.whatsapp.WhatsApp.shared"

IOS_SCHEMA = """
CREATE TABLE Z_PRIMARYKEY (Z_ENT INTEGER PRIMARY KEY, Z_NAME VARCHAR, Z_SUPER INTEGER, Z_MAX INTEGER);
INSERT INTO Z_PRIMARYKEY VALUES (1,'WAChatSession',0,0),(2,'WAGroupMember',0,0),
                                (3,'WAMediaItem',0,0),(4,'WAMessage',0,0);
CREATE TABLE Z_MODELCACHE (Z_CONTENT BLOB);
CREATE TABLE ZWACHATSESSION (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCONTACTJID TEXT,
  ZPARTNERNAME TEXT, ZSESSIONTYPE INT, ZARCHIVED INT, ZHIDDEN INT, ZUNREADCOUNT INT,
  ZMESSAGECOUNTER INT, ZFLAGS INT, ZCONTACTABID INT, ZLASTMESSAGEDATE REAL,
  ZLASTMESSAGETEXT TEXT, ZLASTMESSAGE INT);
CREATE TABLE ZWAGROUPMEMBER (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT,
  ZMEMBERJID TEXT, ZCONTACTNAME TEXT, ZISACTIVE INT, ZISADMIN INT, ZCONTACTABID INT);
CREATE TABLE ZWAMEDIAITEM (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZMESSAGE INT,
  ZMEDIALOCALPATH TEXT, ZFILESIZE INT);
CREATE TABLE ZWAMESSAGE (Z_PK INTEGER PRIMARY KEY, Z_ENT INT, Z_OPT INT, ZCHATSESSION INT,
  ZGROUPMEMBER INT, ZISFROMME INT, ZMESSAGETYPE INT, ZFROMJID TEXT, ZTOJID TEXT,
  ZMESSAGEDATE REAL, ZSORT INT, ZSTANZAID TEXT, ZTEXT TEXT, ZFLAGS INT, ZDOCID INT,
  ZMESSAGESTATUS INT, ZMEDIAITEM INT);
"""

ANDROID_SCHEMA = """
CREATE TABLE jid (_id INTEGER PRIMARY KEY, raw_string TEXT, server TEXT);
CREATE TABLE chat (_id INTEGER PRIMARY KEY, jid_row_id INT, subject TEXT, archived INT);
CREATE TABLE message (_id INTEGER PRIMARY KEY, chat_row_id INT, from_me INT, key_id TEXT,
  timestamp INT, message_type INT, text_data TEXT, sender_jid_row_id INT);
"""


@pytest.fixture
def template(tmp_path) -> Path:
    path = tmp_path / "template.sqlite"
    db = sqlite3.connect(path)
    db.executescript(IOS_SCHEMA)
    db.commit()
    db.close()
    return path


@pytest.fixture
def msgstore(tmp_path) -> Path:
    path = tmp_path / "msgstore.db"
    db = sqlite3.connect(path)
    db.executescript(ANDROID_SCHEMA)
    db.executemany("INSERT INTO jid VALUES (?,?,?)", [
        (1, "15550001@s.whatsapp.net", "s.whatsapp.net"),
        (2, "15550002-1@g.us", "g.us"),
        (3, "15550003@s.whatsapp.net", "s.whatsapp.net"),
        (4, "status@broadcast", "broadcast"),
    ])
    db.executemany("INSERT INTO chat VALUES (?,?,?,?)",
                   [(1, 1, None, 0), (2, 2, "Team", 0), (4, 4, None, 0)])
    t = 1_700_000_000_000
    db.executemany("INSERT INTO message VALUES (?,?,?,?,?,?,?,?)", [
        (1, 1, 0, "A1", t, 0, "hello", 1),
        (2, 1, 1, "A2", t + 1000, 0, "hi back", None),
        (3, 1, 0, "A3", t + 2000, 7, None, 1),          # system notice: skipped
        (4, 1, 0, "A4", t + 3000, 1, "a photo", 1),     # image: text placeholder
        (5, 2, 0, "B1", t + 4000, 0, "group hello", 3),
        (6, 2, 1, "B2", t + 5000, 0, "group reply", None),
        (7, 4, 0, "S1", t + 6000, 0, "status update", 1),  # status chat: skipped
    ])
    db.commit()
    db.close()
    return path


def manifest_record(size: int, digest: bytes) -> bytes:
    return plistlib.dumps({"$objects": ["$null", {"Size": size, "Digest": digest, "LastModified": 1}]},
                          fmt=plistlib.FMT_BINARY)


def make_backup(root: Path, files: dict[str, bytes]) -> Path:
    """Build a minimal unencrypted iOS backup. `files` maps domain -> ChatStorage.sqlite bytes."""
    backup = root / "00000000-0000000000000000"
    backup.mkdir(parents=True)
    (backup / "Manifest.plist").write_bytes(plistlib.dumps({"IsEncrypted": False}))
    manifest = sqlite3.connect(backup / "Manifest.db")
    manifest.execute("CREATE TABLE Files (fileID TEXT, domain TEXT, relativePath TEXT, flags INT, file BLOB)")
    for domain, data in files.items():
        fid = hashlib.sha1(f"{domain}-ChatStorage.sqlite".encode()).hexdigest()
        (backup / fid[:2]).mkdir(exist_ok=True)
        (backup / fid[:2] / fid).write_bytes(data)
        manifest.execute("INSERT INTO Files VALUES (?,?,?,?,?)",
                         (fid, domain, "ChatStorage.sqlite", 1,
                          manifest_record(len(data), hashlib.sha1(data).digest())))
    manifest.commit()
    manifest.close()
    return backup
