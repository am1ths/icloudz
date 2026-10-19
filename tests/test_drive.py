import hashlib
from types import SimpleNamespace

import pytest

from icloudz import drive


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_content(self, chunk_size):
        yield b"file "
        yield b"contents"


class _DownloadNode:
    def open(self, stream=False):
        assert stream is True
        return _Response()


def test_download_returns_checksum_without_rereading_file(tmp_path):
    dest = tmp_path / "nested" / "file.txt"

    checksum = drive.download_file(_DownloadNode(), dest)

    assert dest.read_bytes() == b"file contents"
    assert checksum == hashlib.sha256(b"file contents").hexdigest()


def test_download_does_not_follow_predictable_part_symlink(tmp_path):
    dest = tmp_path / "file.txt"
    outside = tmp_path / "outside.txt"
    outside.write_text("keep")
    dest.with_suffix(".txt.part").symlink_to(outside)

    drive.download_file(_DownloadNode(), dest)

    assert outside.read_text() == "keep"
    assert dest.read_bytes() == b"file contents"


def test_upload_uses_named_file_wrapper_and_preserves_basename(tmp_path):
    path = tmp_path / "file.txt"
    path.write_bytes(b"upload contents")
    uploaded = {}

    class _Response:
        def json(self):
            return {"singleFile": {"uploaded": True}}

    class _Session:
        def post(self, url, data, headers):
            uploaded["url"] = url
            uploaded["content_type"] = headers["Content-Type"]
            chunks = []
            while True:
                chunk = data.read(5)
                if not chunk:
                    break
                chunks.append(chunk)
            uploaded["body"] = b"".join(chunks)
            return _Response()

    class _Connection:
        session = _Session()

        def _get_upload_contentws_url(self, file_object, zone):
            uploaded["name"] = file_object.name
            uploaded["size"] = file_object.seek(0, 2)
            file_object.seek(0)
            return "document-id", "https://example.invalid/upload"

        def _raise_if_error(self, response):
            pass

        def _update_contentws(self, folder_id, content, document_id, file_object,
                              zone, **kwargs):
            uploaded["metadata"] = (folder_id, document_id, content, kwargs)

    root = SimpleNamespace(
        connection=_Connection(),
        data={"docwsid": "root-id", "zone": "drive"},
    )
    api = SimpleNamespace(drive=SimpleNamespace(root=root))
    result = drive.upload_file(api, path, "/")

    assert uploaded["name"] == "file.txt"
    assert b"upload contents" in uploaded["body"]
    assert "multipart/form-data" in uploaded["content_type"]
    assert uploaded["metadata"][0:2] == ("root-id", "document-id")
    assert result.checksum == hashlib.sha256(b"upload contents").hexdigest()
    assert isinstance(result.uploaded_at, float)


def test_parallel_walk_propagates_folder_errors(monkeypatch):
    def fail(*args):
        raise RuntimeError("folder request failed")

    monkeypatch.setattr(drive, "_get_children", fail)

    with pytest.raises(RuntimeError, match="folder request failed"):
        drive._walk_parallel(object(), "")


def test_safe_local_path_rejects_parent_traversal(tmp_path):
    with pytest.raises(ValueError, match="Unsafe path"):
        drive.safe_local_path(tmp_path, "../outside.txt")


def test_safe_local_path_rejects_symlink_escape(tmp_path):
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="escapes local directory"):
        drive.safe_local_path(tmp_path, "link/file.txt")
