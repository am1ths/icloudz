import sqlite3
import hashlib
import threading
from pathlib import Path

DB_PATH = Path.home() / ".config" / "icloudz" / "state.db"

_local = threading.local()


def _conn() -> sqlite3.Connection:
    if getattr(_local, "conn", None) is None:
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(DB_PATH, check_same_thread=False)
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA busy_timeout=5000")
        con.execute("""
            CREATE TABLE IF NOT EXISTS files (
                pair         TEXT NOT NULL DEFAULT 'default',
                path         TEXT NOT NULL,
                local_mtime  REAL,
                local_size   INTEGER,
                remote_mtime TEXT,
                remote_size  INTEGER,
                checksum     TEXT,
                PRIMARY KEY (pair, path)
            )
        """)
        # migrate: old schema had no pair column and TEXT PRIMARY KEY on path
        cols = {r[1] for r in con.execute("PRAGMA table_info(files)").fetchall()}
        if "pair" not in cols:
            con.execute("ALTER TABLE files ADD COLUMN pair TEXT NOT NULL DEFAULT 'default'")
        # migrate: if PRIMARY KEY was just path, rebuild the table with the new PK
        pk_cols = [r[5] for r in con.execute("PRAGMA table_info(files)").fetchall() if r[5]]
        if pk_cols == [1]:  # only one PK column (path was the only PK in old schema)
            con.executescript("""
                ALTER TABLE files RENAME TO files_old;
                CREATE TABLE files (
                    pair         TEXT NOT NULL DEFAULT 'default',
                    path         TEXT NOT NULL,
                    local_mtime  REAL,
                    local_size   INTEGER,
                    remote_mtime TEXT,
                    remote_size  INTEGER,
                    checksum     TEXT,
                    PRIMARY KEY (pair, path)
                );
                INSERT INTO files SELECT 'default', path, local_mtime, local_size,
                    remote_mtime, remote_size, checksum FROM files_old;
                DROP TABLE files_old;
            """)
        con.commit()
        _local.conn = con
    return _local.conn


def record_file(path: str, local_mtime: float, local_size: int,
                remote_mtime: str, remote_size: int | None, checksum: str,
                pair: str = "default") -> None:
    _conn().execute("""
        INSERT INTO files (pair, path, local_mtime, local_size, remote_mtime, remote_size, checksum)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(pair, path) DO UPDATE SET
            local_mtime=excluded.local_mtime,
            local_size=excluded.local_size,
            remote_mtime=excluded.remote_mtime,
            remote_size=excluded.remote_size,
            checksum=excluded.checksum
    """, (pair, path, local_mtime, local_size, remote_mtime, remote_size, checksum))
    _conn().commit()


def record_local(path: str, mtime: float, size: int, checksum: str,
                 pair: str = "default") -> None:
    _conn().execute("""
        INSERT INTO files (pair, path, local_mtime, local_size, checksum)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(pair, path) DO UPDATE SET
            local_mtime=excluded.local_mtime,
            local_size=excluded.local_size,
            checksum=excluded.checksum
    """, (pair, path, mtime, size, checksum))
    _conn().commit()


def record_remote(path: str, mtime: str, size: int, pair: str = "default") -> None:
    _conn().execute("""
        INSERT INTO files (pair, path, remote_mtime, remote_size)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(pair, path) DO UPDATE SET
            remote_mtime=excluded.remote_mtime,
            remote_size=excluded.remote_size
    """, (pair, path, mtime, size))
    _conn().commit()


def get(path: str, pair: str = "default") -> dict | None:
    row = _conn().execute(
        "SELECT * FROM files WHERE pair=? AND path=?", (pair, path)
    ).fetchone()
    if row is None:
        return None
    cols = ["pair", "path", "local_mtime", "local_size", "remote_mtime", "remote_size", "checksum"]
    return dict(zip(cols, row))


def delete(path: str, pair: str = "default") -> None:
    _conn().execute("DELETE FROM files WHERE pair=? AND path=?", (pair, path))
    _conn().commit()


def all_tracked(pair: str | None = None) -> list[dict]:
    cols = ["pair", "path", "local_mtime", "local_size", "remote_mtime", "remote_size", "checksum"]
    if pair:
        rows = _conn().execute("SELECT * FROM files WHERE pair=?", (pair,)).fetchall()
    else:
        rows = _conn().execute("SELECT * FROM files").fetchall()
    return [dict(zip(cols, r)) for r in rows]


def file_checksum(local_path: Path) -> str:
    h = hashlib.sha256()
    with open(local_path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def needs_upload(path: str, local_mtime: float, local_size: int,
                 pair: str = "default") -> bool:
    tracked = get(path, pair)
    if tracked is None:
        return True
    return tracked.get("local_mtime") != local_mtime or tracked.get("local_size") != local_size
