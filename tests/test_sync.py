import pytest

from icloudz import state as st
from icloudz import sync
from icloudz.drive import UploadResult
from icloudz.errors import TransferError


def test_pull_preserves_changed_local_file_when_remote_deleted(monkeypatch, tmp_db, tmp_path):
    local = tmp_path / "important.txt"
    local.write_text("new local version")
    stat = local.stat()
    st.record_file("important.txt", stat.st_mtime - 10, stat.st_size, "2024-01-01T00:00:00+00:00",
                   stat.st_size, "old checksum")
    monkeypatch.setattr(sync, "_fetch_remote", lambda *args, **kwargs: [])

    sync.pull(object(), "/", tmp_path)

    assert local.read_text() == "new local version"
    assert st.get("important.txt") is None


def test_remote_wins_skips_unchanged_remote_file(monkeypatch, tmp_db, tmp_path):
    local = tmp_path / "same.txt"
    local.write_text("same contents")
    stat = local.stat()
    remote_date = "2024-01-01T00:00:00+00:00"
    st.record_file("same.txt", stat.st_mtime, stat.st_size, remote_date,
                   stat.st_size, "checksum")
    item = {
        "path": "same.txt",
        "date_modified": remote_date,
        "size": stat.st_size,
        "node": object(),
        "is_dir": False,
    }
    monkeypatch.setattr(sync, "_fetch_remote", lambda *args, **kwargs: [item])
    monkeypatch.setattr(sync, "download_file", lambda *args: (_ for _ in ()).throw(AssertionError()))

    sync.pull(object(), "/", tmp_path, pair={"conflict": "remote-wins"})

    assert local.read_text() == "same contents"


def test_sync_local_wins_uploads_local_copy_when_remote_changes(
    monkeypatch, tmp_db, tmp_path
):
    local = tmp_path / "same.txt"
    local.write_text("local version")
    stat = local.stat()
    st.record_file("same.txt", stat.st_mtime, stat.st_size,
                   "2024-01-01T00:00:00+00:00", stat.st_size, "checksum")
    item = {
        "path": "same.txt",
        "date_modified": "2024-01-02T00:00:00+00:00",
        "size": stat.st_size,
        "node": object(),
        "is_dir": False,
    }
    uploaded = []
    monkeypatch.setattr(sync, "_fetch_remote", lambda *args, **kwargs: [item])
    monkeypatch.setattr(sync, "ensure_remote_dir", lambda *args: None)
    monkeypatch.setattr(
        sync, "upload_file", lambda *args: uploaded.append("same.txt")
        or UploadResult(1_700_000_000.0, "checksum")
    )
    monkeypatch.setattr(sync, "download_file", lambda *args: (_ for _ in ()).throw(AssertionError()))

    sync.pull(
        object(), "/", tmp_path,
        pair={"name": "default", "conflict": "local-wins"},
        resolve_local_wins=True,
    )

    assert uploaded == ["same.txt"]
    assert st.get("same.txt")["remote_mtime"] is not None


def test_sync_propagates_local_deletion(monkeypatch, tmp_db, tmp_path):
    st.record_file("deleted.txt", 100.0, 7, "2024-01-01T00:00:00+00:00", 7, "checksum")
    item = {
        "path": "deleted.txt",
        "date_modified": "2024-01-01T00:00:00+00:00",
        "size": 7,
        "node": object(),
        "is_dir": False,
    }
    deleted = []
    monkeypatch.setattr(sync, "_fetch_remote", lambda *args, **kwargs: [item])
    monkeypatch.setattr(sync, "delete_remote", lambda api, path: deleted.append(path))
    monkeypatch.setattr(sync, "download_file", lambda *args: (_ for _ in ()).throw(AssertionError()))

    sync.pull(object(), "/", tmp_path, pair={"name": "default"},
              propagate_local_deletes=True)

    assert deleted == ["/deleted.txt"]
    assert st.get("deleted.txt") is None


def test_push_respects_selective_paths(monkeypatch, tmp_db, tmp_path):
    (tmp_path / "Work").mkdir()
    (tmp_path / "Work" / "a.txt").write_text("upload")
    (tmp_path / "Other").mkdir()
    (tmp_path / "Other" / "b.txt").write_text("skip")
    uploaded = []
    monkeypatch.setattr(sync, "ensure_remote_dir", lambda *args: None)
    monkeypatch.setattr(
        sync, "upload_file", lambda api, path, remote: uploaded.append(path.name)
        or UploadResult(1_700_000_000.0, "checksum")
    )

    sync.push(
        object(), tmp_path, "/Documents",
        pair={"name": "work", "selective": ["/Documents/Work"]},
    )

    assert uploaded == ["a.txt"]


def test_push_returns_failure_when_any_transfer_fails(monkeypatch, tmp_db, tmp_path):
    (tmp_path / "failed.txt").write_text("upload")
    monkeypatch.setattr(sync, "ensure_remote_dir", lambda *args: None)
    monkeypatch.setattr(sync, "upload_file", lambda *args: (_ for _ in ()).throw(OSError("offline")))

    with pytest.raises(TransferError, match="failed to upload 1 file"):
        sync.push(object(), tmp_path, "/")
