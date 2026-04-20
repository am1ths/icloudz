import pytest
import tempfile
from pathlib import Path


@pytest.fixture
def tmp_db(monkeypatch, tmp_path):
    """Redirect state DB to a temp file for each test."""
    db_path = tmp_path / "state.db"
    import icloudz.state as state_mod
    monkeypatch.setattr(state_mod, "DB_PATH", db_path)
    # reset thread-local connection so next call opens the new DB
    import threading
    state_mod._local = threading.local()
    yield db_path
    # close connection if open
    conn = getattr(state_mod._local, "conn", None)
    if conn:
        conn.close()
        state_mod._local.conn = None


@pytest.fixture
def tmp_config(monkeypatch, tmp_path):
    """Redirect config to a temp directory."""
    import icloudz.config as cfg_mod
    monkeypatch.setattr(cfg_mod, "CONFIG_DIR", tmp_path)
    monkeypatch.setattr(cfg_mod, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(cfg_mod, "STATUS_FILE", tmp_path / "status.json")
    yield tmp_path
