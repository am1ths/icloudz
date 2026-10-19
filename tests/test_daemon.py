import threading
import time
from types import SimpleNamespace

from icloudz import daemon
from icloudz import state as st
from icloudz.drive import UploadResult


def test_poll_downloads_files_concurrently(monkeypatch, tmp_path):
    items = [
        {
            "path": f"file-{i}.txt",
            "date_modified": "2024-01-01T00:00:00+00:00",
            "size": 4,
            "node": object(),
            "is_dir": False,
        }
        for i in range(4)
    ]
    refreshed = False

    def refresh_root():
        nonlocal refreshed
        refreshed = True

    api = SimpleNamespace(drive=SimpleNamespace(refresh_root=refresh_root))

    def list_remote(*args, **kwargs):
        assert refreshed
        assert kwargs["max_workers"] == 3
        return items

    monkeypatch.setattr(daemon, "list_remote", list_remote)
    monkeypatch.setattr(daemon.st, "get", lambda *args: None)
    monkeypatch.setattr(daemon.st, "all_tracked", lambda *args: [])
    monkeypatch.setattr(daemon.st, "record_file", lambda *args: None)
    monkeypatch.setattr(daemon, "mark_poll_start", lambda paths: None)
    monkeypatch.setattr(daemon, "mark_poll_done", lambda paths: None)

    active = 0
    max_active = 0
    lock = threading.Lock()

    def fake_download(node, dest):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        try:
            time.sleep(0.05)
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(b"data")
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr(daemon, "download_file", fake_download)

    count = daemon._poll_remote(
        api=api,
        local_dir=tmp_path,
        remote_path="/",
        max_workers=3,
    )

    assert count == 4
    assert max_active > 1


def test_poll_propagates_local_deletion_after_restart(monkeypatch, tmp_db, tmp_path):
    item = {
        "path": "deleted.txt",
        "date_modified": "2024-01-01T00:00:00+00:00",
        "size": 7,
        "node": object(),
        "is_dir": False,
    }
    st.record_file("deleted.txt", 100.0, 7, item["date_modified"], 7, "checksum")
    api = SimpleNamespace(drive=SimpleNamespace(refresh_root=lambda: None))
    deleted = []
    monkeypatch.setattr(daemon, "list_remote", lambda *args, **kwargs: [item])
    monkeypatch.setattr(daemon, "delete_remote", lambda api, path: deleted.append(path))

    count = daemon._poll_remote(api, tmp_path, "/", conflict="newest-wins")

    assert count == 0
    assert deleted == ["/deleted.txt"]
    assert st.get("deleted.txt") is None


def test_poll_restores_dirty_local_file_after_remote_deletion(
    monkeypatch, tmp_db, tmp_path
):
    local = tmp_path / "important.txt"
    local.write_text("new local contents")
    stat = local.stat()
    st.record_file("important.txt", stat.st_mtime - 10, stat.st_size,
                   "2024-01-01T00:00:00+00:00", stat.st_size, "old checksum")
    api = SimpleNamespace(drive=SimpleNamespace(refresh_root=lambda: None))
    uploaded = []
    monkeypatch.setattr(daemon, "list_remote", lambda *args, **kwargs: [])
    monkeypatch.setattr(daemon, "ensure_remote_dir", lambda *args: None)
    monkeypatch.setattr(
        daemon, "upload_file", lambda api, path, remote: uploaded.append(path.name)
        or UploadResult(1_700_000_000.0, "checksum")
    )

    daemon._poll_remote(api, tmp_path, "/", conflict="newest-wins")

    assert local.read_text() == "new local contents"
    assert uploaded == ["important.txt"]
    assert st.get("important.txt")["remote_mtime"] is not None
