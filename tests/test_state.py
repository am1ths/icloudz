import pytest
from icloudz import state as st


def test_record_and_get(tmp_db):
    st.record_file("docs/a.txt", 1000.0, 512, "2024-01-01T00:00:00", 512, "abc123")
    row = st.get("docs/a.txt")
    assert row["path"] == "docs/a.txt"
    assert row["local_size"] == 512
    assert row["checksum"] == "abc123"
    assert row["pair"] == "default"


def test_get_missing(tmp_db):
    assert st.get("nonexistent.txt") is None


def test_delete(tmp_db):
    st.record_local("file.txt", 1.0, 100, "abc")
    st.delete("file.txt")
    assert st.get("file.txt") is None


def test_pair_isolation(tmp_db):
    st.record_local("same.txt", 1.0, 100, "aaa", pair="pairA")
    st.record_local("same.txt", 2.0, 200, "bbb", pair="pairB")
    a = st.get("same.txt", pair="pairA")
    b = st.get("same.txt", pair="pairB")
    assert a["local_size"] == 100
    assert b["local_size"] == 200


def test_all_tracked_filter(tmp_db):
    st.record_local("x.txt", 1.0, 1, "x", pair="p1")
    st.record_local("y.txt", 2.0, 2, "y", pair="p2")
    assert len(st.all_tracked("p1")) == 1
    assert len(st.all_tracked()) == 2


def test_needs_upload_new(tmp_db):
    assert st.needs_upload("new.txt", 1.0, 100) is True


def test_needs_upload_unchanged(tmp_db):
    st.record_local("f.txt", 1.0, 100, "abc")
    assert st.needs_upload("f.txt", 1.0, 100) is False


def test_needs_upload_changed(tmp_db):
    st.record_local("f.txt", 1.0, 100, "abc")
    assert st.needs_upload("f.txt", 2.0, 100) is True


def test_migration_from_old_schema(tmp_db):
    """Simulate an old single-PK schema and verify migration."""
    import sqlite3
    con = sqlite3.connect(tmp_db)
    con.execute("DROP TABLE IF EXISTS files")
    con.execute("""
        CREATE TABLE files (
            path         TEXT PRIMARY KEY,
            local_mtime  REAL,
            local_size   INTEGER,
            remote_mtime TEXT,
            remote_size  INTEGER,
            checksum     TEXT
        )
    """)
    con.execute("INSERT INTO files VALUES ('old.txt', 1.0, 42, NULL, NULL, 'abc')")
    con.commit()
    con.close()

    # reset thread-local so state module re-opens and migrates
    import threading
    st._local = threading.local()

    row = st.get("old.txt")
    assert row is not None
    assert row["pair"] == "default"
    assert row["local_size"] == 42
