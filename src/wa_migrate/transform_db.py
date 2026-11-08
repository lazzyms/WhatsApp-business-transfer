#!/usr/bin/env python3
"""Transform a decrypted Android WhatsApp msgstore.db into iOS ChatStorage.sqlite.

The iOS database is NOT built from scratch. The output starts as a copy of a
template ChatStorage.sqlite taken from your own iPhone backup (WhatsApp Business
installed and registered on the iPhone), so the CoreData schema, model metadata
and Z_PRIMARYKEY bookkeeping are exactly what that iOS app version expects.
Entity ids (Z_ENT) and columns are read from the template, never hard-coded.

Scope (deliberate): text, and a text placeholder/caption for media. Media files
are not migrated. System notices, deleted/revoked messages and status updates
are skipped.

Usage:
  transform_db.py inspect TEMPLATE.sqlite
  transform_db.py convert MSGSTORE.db TEMPLATE.sqlite OUT.sqlite [--allow-nonempty]
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import uuid
from pathlib import Path

MAC_EPOCH_OFFSET = 978307200  # seconds between 1970-01-01 and 2001-01-01

# Android message_type -> placeholder text. Types not listed and not 0 are skipped
# (system notices, revoked messages, etc.). Media becomes plain text on iOS.
MEDIA_PLACEHOLDER = {
    1: "image", 2: "audio", 3: "video", 4: "contact card", 5: "location",
    9: "document", 13: "GIF", 16: "live location", 20: "sticker",
}

REQUIRED_ANDROID = {
    "message": {"_id", "chat_row_id", "from_me", "key_id", "timestamp",
                "message_type", "text_data", "sender_jid_row_id"},
    "chat": {"_id", "jid_row_id", "subject"},
    "jid": {"_id", "raw_string"},
}
# Android message_type -> iOS ZMESSAGETYPE, for media whose file we can copy.
MEDIA_IOS_TYPE = {1: 1, 2: 3, 3: 2, 9: 8, 13: 2}


class TransformError(Exception):
    pass


def mac_time(ms: int | None) -> float:
    return (ms or 0) / 1000.0 - MAC_EPOCH_OFFSET


def columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in db.execute(f'PRAGMA table_info("{table}")')}


def insert(db: sqlite3.Connection, table: str, have: set[str], row: dict) -> int:
    row = {k: v for k, v in row.items() if k in have}
    cols = ",".join(row)
    cur = db.execute(f'INSERT INTO "{table}" ({cols}) VALUES ({",".join("?" * len(row))})',
                     list(row.values()))
    return cur.lastrowid


def entity_ids(ios: sqlite3.Connection) -> dict[str, int]:
    found = dict(ios.execute("SELECT Z_NAME, Z_ENT FROM Z_PRIMARYKEY"))
    missing = [n for n in ("WAMessage", "WAChatSession") if n not in found]
    if missing:
        raise TransformError(f"Template Z_PRIMARYKEY lacks entities: {missing}")
    return found


def inspect(template: Path) -> None:
    db = sqlite3.connect(f"file:{template}?mode=ro", uri=True)
    print("Z_PRIMARYKEY (Z_ENT, Z_NAME, Z_SUPER, Z_MAX):")
    for r in db.execute("SELECT Z_ENT, Z_NAME, Z_SUPER, Z_MAX FROM Z_PRIMARYKEY ORDER BY Z_ENT"):
        print("  ", r)
    for t in ("ZWAMESSAGE", "ZWACHATSESSION", "ZWAGROUPMEMBER"):
        cols = sorted(columns(db, t))
        if not cols:
            print(f"\n{t}: (absent)")
            continue
        n = db.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
        print(f"\n{t}: {n} rows, {len(cols)} columns\n  " + ", ".join(cols))


def check_android(src: sqlite3.Connection) -> int:
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for t, need in REQUIRED_ANDROID.items():
        if t not in tables:
            raise TransformError(
                f"Android DB has no '{t}' table. Only the modern msgstore schema "
                "(WhatsApp 2.21+) is supported.")
        gap = need - columns(src, t)
        if gap:
            raise TransformError(f"Android '{t}' table lacks columns: {sorted(gap)}")
    return src.execute("SELECT count(*) FROM message").fetchone()[0]


def convert(msgstore: Path, template: Path, out: Path, allow_nonempty: bool = False,
            media_root: Path | None = None) -> dict:
    """media_root: folder containing the pulled Android 'Media' dir (paths in the DB
    are relative to its parent, e.g. 'Media/WhatsApp Business Images/IMG-1.jpg').
    stats['media_files'] lists (source Path, iOS path relative to the app container)."""
    if out.exists():
        raise TransformError(f"Refusing to overwrite existing {out}")
    src = sqlite3.connect(f"file:{msgstore}?mode=ro", uri=True)
    src.row_factory = sqlite3.Row
    total = check_android(src)
    if total == 0:
        raise TransformError("Android message table is empty.")

    # Copy the template via the backup API: yields a single consistent file (WAL merged).
    tpl = sqlite3.connect(f"file:{template}?mode=ro", uri=True)
    ios = sqlite3.connect(out)
    tpl.backup(ios)
    tpl.close()
    ios.execute("PRAGMA journal_mode=WAL")  # same as the app writes; single file after close

    try:
        stats = _convert(src, ios, allow_nonempty, media_root)
        ios.commit()
        if ios.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise TransformError("Output failed integrity_check")
    except Exception:
        ios.close()
        out.unlink(missing_ok=True)
        raise
    ios.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    ios.close()
    stats["android_total"] = total
    return stats


def _convert(src: sqlite3.Connection, ios: sqlite3.Connection, allow_nonempty: bool,
             media_root: Path | None) -> dict:
    ent = entity_ids(ios)
    msg_cols, ses_cols = columns(ios, "ZWAMESSAGE"), columns(ios, "ZWACHATSESSION")
    mem_cols = columns(ios, "ZWAGROUPMEMBER")
    use_members = "WAGroupMember" in ent and bool(mem_cols)

    existing = ios.execute("SELECT count(*) FROM ZWAMESSAGE").fetchone()[0]
    if existing and not allow_nonempty:
        raise TransformError(
            f"Template already holds {existing} messages. Use a freshly installed, "
            "empty WhatsApp Business backup, or pass --allow-nonempty to append.")

    sessions = {r[0]: r[1] for r in ios.execute(
        "SELECT ZCONTACTJID, Z_PK FROM ZWACHATSESSION WHERE ZCONTACTJID IS NOT NULL")}
    members: dict[tuple[int, str], int] = {}
    stats = {"chats": 0, "messages": 0, "skipped": 0, "media": 0, "media_files": []}
    item_cols = columns(ios, "ZWAMEDIAITEM")
    use_media = bool(media_root) and "WAMediaItem" in ent and bool(item_cols)
    mm = columns(src, "message_media")
    def mcol(c, alias):
        return f"mm.{c} AS {alias}" if c in mm else f"NULL AS {alias}"
    media_join = ("LEFT JOIN message_media mm ON mm.message_row_id = m._id" if mm else "")
    media_sel = ", ".join(mcol(c, a) for c, a in (
        ("file_path", "media_path"), ("file_size", "media_size"),
        ("media_duration", "media_dur"), ("media_name", "media_name")))

    chats = src.execute(
        """SELECT c._id, c.subject, c.archived, j.raw_string AS jid
           FROM chat c JOIN jid j ON j._id = c.jid_row_id
           WHERE j.raw_string NOT LIKE 'status@%' AND j.server NOT IN ('broadcast')
           ORDER BY c._id""" if "server" in columns(src, "jid") else
        """SELECT c._id, c.subject, c.archived, j.raw_string AS jid
           FROM chat c JOIN jid j ON j._id = c.jid_row_id
           WHERE j.raw_string NOT LIKE 'status@%' ORDER BY c._id""").fetchall()

    for chat in chats:
        jid = chat["jid"]
        is_group = jid.endswith("@g.us")
        rows = src.execute(
            f"""SELECT m.key_id, m.from_me, m.timestamp, m.message_type, m.text_data,
                      sj.raw_string AS sender, {media_sel}
               FROM message m LEFT JOIN jid sj ON sj._id = m.sender_jid_row_id
               {media_join}
               WHERE m.chat_row_id = ? ORDER BY m.timestamp, m._id""",
            (chat["_id"],)).fetchall()
        usable = []
        for r in rows:
            text = r["text_data"]
            kind = r["message_type"]
            media = None
            if kind == 0:
                if not text:
                    continue
            elif kind in MEDIA_IOS_TYPE and use_media and r["media_path"] \
                    and (media_root / r["media_path"]).is_file():
                media = {"src": media_root / r["media_path"], "type": MEDIA_IOS_TYPE[kind],
                         "size": r["media_size"], "dur": r["media_dur"], "name": r["media_name"]}
            elif kind in MEDIA_PLACEHOLDER:
                label = f"<{MEDIA_PLACEHOLDER[kind]} not transferred>"
                text = f"{label} {text}" if text else label
            else:
                continue
            usable.append((r, text, media))
        stats["skipped"] += len(rows) - len(usable)
        if not usable:
            continue

        last_ts = mac_time(usable[-1][0]["timestamp"])
        pk = sessions.get(jid)
        if pk is None:
            pk = insert(ios, "ZWACHATSESSION", ses_cols, {
                "Z_ENT": ent["WAChatSession"], "Z_OPT": 1,
                "ZCONTACTJID": jid, "ZPARTNERNAME": (chat["subject"] or "") if is_group else "",
                "ZSESSIONTYPE": 1 if is_group else 0,
                "ZARCHIVED": int(bool(chat["archived"])), "ZHIDDEN": 0,
                "ZUNREADCOUNT": 0, "ZMESSAGECOUNTER": len(usable),
                # CoreData scalars are non-optional: values below are what real iOS rows hold.
                "ZCONTACTABID": 0, "ZFLAGS": 256, "ZIDENTITYVERIFICATIONEPOCH": -1,
                "ZIDENTITYVERIFICATIONSTATE": 0, "ZREMOVED": 0,
                "ZSPOTLIGHTSTATUS": 1 if is_group else -5,
                "ZLASTMESSAGEDATE": last_ts, "ZLASTMESSAGETEXT": (usable[-1][1] or "")[:200],
            })
            sessions[jid] = pk
            stats["chats"] += 1

        for sort, (r, text, media) in enumerate(usable, start=1):
            from_me = int(bool(r["from_me"]))
            sender = r["sender"] or (None if from_me or is_group else jid)
            member_pk = None
            if use_members and is_group and not from_me and sender:
                member_pk = members.get((pk, sender))
                if member_pk is None:
                    member_pk = insert(ios, "ZWAGROUPMEMBER", mem_cols, {
                        "Z_ENT": ent["WAGroupMember"], "Z_OPT": 1,
                        "ZCHATSESSION": pk, "ZMEMBERJID": sender, "ZCONTACTABID": 0,
                        "ZISACTIVE": 1, "ZISADMIN": 0, "ZCONTACTNAME": ""})
                    members[(pk, sender)] = member_pk
            date = mac_time(r["timestamp"])
            msg_pk = insert(ios, "ZWAMESSAGE", msg_cols, {
                "Z_ENT": ent["WAMessage"], "Z_OPT": 1,
                "ZCHATSESSION": pk, "ZGROUPMEMBER": member_pk,
                "ZISFROMME": from_me, "ZMESSAGETYPE": media["type"] if media else 0,
                "ZFROMJID": None if from_me else jid,
                "ZTOJID": jid if from_me else None,
                "ZMESSAGEDATE": date,
                "ZSORT": sort, "ZSTANZAID": r["key_id"], "ZTEXT": text,
                "ZFLAGS": 16777216, "ZDOCID": sort, "ZDATAITEMVERSION": 3,
                "ZGROUPEVENTTYPE": 2, "ZMESSAGESTATUS": 6 if from_me else 0,
                "ZSPOTLIGHTSTATUS": -32768, "ZMESSAGEERRORSTATUS": 0, "ZENCRETRYCOUNT": 0,
                "ZFILTEREDRECIPIENTCOUNT": 0, "ZCHILDMESSAGESDELIVEREDCOUNT": 0,
                "ZCHILDMESSAGESPLAYEDCOUNT": 0, "ZCHILDMESSAGESREADCOUNT": 0,
            })
            stats["messages"] += 1
            last_pk = msg_pk
            if media:
                h = uuid.uuid4()
                local = f"Media/{jid}/{h.hex[0]}/{h.hex[1]}/{str(h).upper()}{media['src'].suffix.lower()}"
                item_pk = insert(ios, "ZWAMEDIAITEM", item_cols, {
                    "Z_ENT": ent["WAMediaItem"], "Z_OPT": 1, "ZMESSAGE": msg_pk,
                    "ZMEDIALOCALPATH": local, "ZFILESIZE": media["size"] or media["src"].stat().st_size,
                    "ZMOVIEDURATION": media["dur"] or 0, "ZTITLE": media["name"], "ZMEDIAORIGIN": 0,
                    "ZASPECTRATIO": 0.0, "ZHACCURACY": 0.0, "ZLATITUDE": 0.0, "ZLONGITUDE": 0.0})
                ios.execute("UPDATE ZWAMESSAGE SET ZMEDIAITEM=? WHERE Z_PK=?", (item_pk, msg_pk)) \
                    if "ZMEDIAITEM" in msg_cols else None
                stats["media"] += 1
                stats["media_files"].append((media["src"], "Message/" + local))
        ios.execute("UPDATE ZWACHATSESSION SET ZLASTMESSAGE=? WHERE Z_PK=?", (last_pk, pk))

    # Keep CoreData's primary-key allocator ahead of what we inserted.
    for name, table in (("WAMessage", "ZWAMESSAGE"), ("WAChatSession", "ZWACHATSESSION"),
                        ("WAGroupMember", "ZWAGROUPMEMBER"),
                        ("WAMediaItem", "ZWAMEDIAITEM")):
        if name in ent:
            ios.execute("UPDATE Z_PRIMARYKEY SET Z_MAX=(SELECT IFNULL(MAX(Z_PK),0) FROM "
                        f"{table}) WHERE Z_NAME=?", (name,))
    return stats


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("inspect", help="print template schema and Z_PRIMARYKEY")
    p.add_argument("template", type=Path)
    p = sub.add_parser("convert", help="build ChatStorage.sqlite")
    p.add_argument("msgstore", type=Path)
    p.add_argument("template", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--allow-nonempty", action="store_true")
    p.add_argument("--media-root", type=Path, help="folder containing the pulled Android 'Media' dir")
    a = ap.parse_args()
    try:
        if a.cmd == "inspect":
            inspect(a.template)
        else:
            s = convert(a.msgstore, a.template, a.out, a.allow_nonempty, a.media_root)
            print(f"Converted {s['messages']} of {s['android_total']} Android messages "
                  f"into {s['chats']} new chats ({s['skipped']} skipped: system/revoked/empty).")
    except (TransformError, sqlite3.Error, OSError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
