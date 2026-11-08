import sqlite3

import pytest

from wa_migrate import transform_db


def rows(path, sql):
    db = sqlite3.connect(path)
    try:
        return db.execute(sql).fetchall()
    finally:
        db.close()


def test_convert_counts_and_skips(msgstore, template, tmp_path):
    out = tmp_path / "out.sqlite"
    stats = transform_db.convert(msgstore, template, out)
    # 5 usable (3 one-to-one incl. image placeholder + 2 group); system notice and status chat skipped.
    assert stats["messages"] == 5 and stats["chats"] == 2 and stats["android_total"] == 7
    texts = [r[0] for r in rows(out, "SELECT ZTEXT FROM ZWAMESSAGE ORDER BY Z_PK")]
    assert "<image not transferred> a photo" in texts
    assert rows(out, "PRAGMA integrity_check") == [("ok",)]


def test_every_chat_is_consistent(msgstore, template, tmp_path):
    out = tmp_path / "out.sqlite"
    transform_db.convert(msgstore, template, out)
    # sort numbers are unique and 1..n per chat
    assert rows(out, "SELECT count(*) FROM (SELECT 1 FROM ZWAMESSAGE GROUP BY ZCHATSESSION, ZSORT "
                     "HAVING count(*) > 1)") == [(0,)]
    # ZLASTMESSAGE points at that chat's newest message
    assert rows(out, "SELECT count(*) FROM ZWACHATSESSION s JOIN ZWAMESSAGE m ON m.Z_PK = s.ZLASTMESSAGE "
                     "WHERE m.ZCHATSESSION != s.Z_PK OR m.ZSORT != (SELECT max(ZSORT) FROM ZWAMESSAGE "
                     "WHERE ZCHATSESSION = s.Z_PK)") == [(0,)]
    # CoreData scalars must never be NULL (a NULL makes WhatsApp show 'Recover chat history')
    assert rows(out, "SELECT count(*) FROM ZWACHATSESSION WHERE ZPARTNERNAME IS NULL OR ZFLAGS IS NULL "
                     "OR ZCONTACTABID IS NULL") == [(0,)]
    assert rows(out, "SELECT count(*) FROM ZWAMESSAGE WHERE ZFLAGS IS NULL OR ZDOCID IS NULL "
                     "OR ZSORT IS NULL") == [(0,)]


def test_primary_key_allocator_stays_ahead(msgstore, template, tmp_path):
    out = tmp_path / "out.sqlite"
    transform_db.convert(msgstore, template, out)
    got = dict(rows(out, "SELECT Z_NAME, Z_MAX FROM Z_PRIMARYKEY"))
    assert got["WAMessage"] == 5 and got["WAChatSession"] == 2 and got["WAGroupMember"] == 1


def test_group_member_is_the_sender(msgstore, template, tmp_path):
    out = tmp_path / "out.sqlite"
    transform_db.convert(msgstore, template, out)
    assert rows(out, "SELECT ZMEMBERJID FROM ZWAGROUPMEMBER") == [("15550003@s.whatsapp.net",)]


def test_refuses_nonempty_template_unless_allowed(msgstore, template, tmp_path):
    db = sqlite3.connect(template)
    db.execute("INSERT INTO ZWAMESSAGE (Z_ENT, ZTEXT) VALUES (4, 'welcome')")
    db.commit()
    db.close()
    with pytest.raises(transform_db.TransformError, match="already holds"):
        transform_db.convert(msgstore, template, tmp_path / "a.sqlite")
    transform_db.convert(msgstore, template, tmp_path / "b.sqlite", allow_nonempty=True)


def test_never_overwrites_output(msgstore, template, tmp_path):
    out = tmp_path / "out.sqlite"
    out.write_bytes(b"x")
    with pytest.raises(transform_db.TransformError, match="Refusing to overwrite"):
        transform_db.convert(msgstore, template, out)
    assert out.read_bytes() == b"x"


def test_rejects_old_android_schema(template, tmp_path):
    old = tmp_path / "old.db"
    db = sqlite3.connect(old)
    db.execute("CREATE TABLE messages (_id INTEGER)")
    db.commit()
    db.close()
    with pytest.raises(transform_db.TransformError, match="no 'message' table"):
        transform_db.convert(old, template, tmp_path / "out.sqlite")
