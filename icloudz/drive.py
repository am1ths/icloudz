import hashlib
import mimetypes
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from typing import NamedTuple
from pyicloud import PyiCloudService
from requests_toolbelt.multipart.encoder import MultipartEncoder

_LIST_WORKERS = 4


def resolve_node(api: PyiCloudService, remote_path: str):
    parts = [p for p in PurePosixPath(remote_path).parts if p != "/"]
    node = api.drive.root
    for part in parts:
        try:
            node = node[part]
        except KeyError:
            raise KeyError(f"Remote path not found: {remote_path!r} (missing: {part!r})")
    return node


def safe_local_path(local_root: Path, rel: str) -> Path:
    relative = PurePosixPath(rel)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Unsafe path from iCloud: {rel!r}")
    root = local_root.resolve()
    dest = root.joinpath(*relative.parts)
    if not dest.resolve().is_relative_to(root):
        raise ValueError(f"Path escapes local directory: {rel!r}")
    return dest


def list_remote(api: PyiCloudService, remote_path: str = "/", recursive: bool = False,
                depth: int | None = None, max_workers: int = _LIST_WORKERS) -> list[dict]:
    """Return a list of items under remote_path."""
    node = resolve_node(api, remote_path) if remote_path != "/" else api.drive.root
    if recursive:
        return _walk_parallel(node, "", max_depth=depth, max_workers=max_workers)
    return _walk(node, "", recursive=False)


def _walk(node, prefix: str, recursive: bool = False, depth: int | None = None,
          current_depth: int = 0) -> list[dict]:
    results = []
    children = node.get_children()
    for child in children:
        name = child.name
        rel = f"{prefix}/{name}" if prefix else name
        if child.type in ("folder", "app_library"):
            results.append({
                "path": rel,
                "size": None,
                "date_modified": str(child.date_modified),
                "node": child,
                "is_dir": True,
            })
            if recursive and (depth is None or current_depth < depth):
                results.extend(_walk(child, rel, recursive=True, depth=depth,
                                     current_depth=current_depth + 1))
        else:
            results.append({
                "path": rel,
                "size": child.size,
                "date_modified": str(child.date_modified),
                "node": child,
                "is_dir": False,
            })
    return results


def _walk_parallel(root_node, root_prefix: str, max_depth: int | None = None,
                   max_workers: int = _LIST_WORKERS) -> list[dict]:
    """Parallel recursive walk — fetches folder children concurrently."""
    results = []
    pending = [(root_node, root_prefix, 0)]

    with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
        while pending:
            batch = pending
            pending = []
            futures = {pool.submit(_get_children, node, prefix): (node, prefix, d)
                       for node, prefix, d in batch}
            for future in as_completed(futures):
                _, _, d = futures[future]
                children_items, subdirs = future.result()
                results.extend(children_items)
                if max_depth is None or d < max_depth:
                    pending.extend((n, p, d + 1) for n, p in subdirs)

    return results


def _get_children(node, prefix: str) -> tuple[list[dict], list[tuple]]:
    items = []
    subdirs = []
    children = node.get_children()

    for child in children:
        name = child.name
        rel = f"{prefix}/{name}" if prefix else name
        if child.type in ("folder", "app_library"):
            items.append({
                "path": rel,
                "size": None,
                "date_modified": str(child.date_modified),
                "node": child,
                "is_dir": True,
            })
            subdirs.append((child, rel))
        else:
            items.append({
                "path": rel,
                "size": child.size,
                "date_modified": str(child.date_modified),
                "node": child,
                "is_dir": False,
            })
    return items, subdirs


def download_file(node, dest: Path) -> str:
    dest.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{dest.name}.", suffix=".part",
                                    dir=dest.parent)
    tmp = Path(tmp_name)
    checksum = hashlib.sha256()
    try:
        with node.open(stream=True) as r:
            with os.fdopen(fd, "wb") as f:
                fd = -1
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
                    checksum.update(chunk)
        tmp.replace(dest)
    except Exception:
        if fd >= 0:
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    return checksum.hexdigest()


class _NamedFile:
    """Expose a basename and checksum while streaming a local file."""

    def __init__(self, file, name: str):
        self._file = file
        self.name = name
        self._checksum = hashlib.sha256()

    def __getattr__(self, name):
        return getattr(self._file, name)

    def read(self, size: int = -1) -> bytes:
        data = self._file.read(size)
        self._checksum.update(data)
        return data

    def seek(self, offset: int, whence: int = os.SEEK_SET) -> int:
        position = self._file.seek(offset, whence)
        if position == 0:
            self._checksum = hashlib.sha256()
        return position

    @property
    def checksum(self) -> str:
        return self._checksum.hexdigest()


class UploadResult(NamedTuple):
    uploaded_at: float
    checksum: str


def upload_file(api: PyiCloudService, local_path: Path, remote_dir: str) -> UploadResult:
    node = resolve_node(api, remote_dir) if remote_dir != "/" else api.drive.root
    uploaded_at = time.time()
    with open(local_path, "rb") as raw:
        file_object = _NamedFile(raw, local_path.name)
        connection = node.connection
        zone = node.data["zone"]
        document_id, content_url = connection._get_upload_contentws_url(
            file_object=file_object, zone=zone
        )
        content_type = mimetypes.guess_type(file_object.name)[0] or ""
        file_field = ((file_object.name, file_object, content_type)
                      if content_type else (file_object.name, file_object))
        encoder = MultipartEncoder(fields={
            file_object.name: file_field,
        })
        response = connection.session.post(
            content_url,
            data=encoder,
            headers={"Content-Type": encoder.content_type},
        )
        connection._raise_if_error(response)
        content_response = response.json()["singleFile"]
        connection._update_contentws(
            node.data["docwsid"], content_response, document_id, file_object,
            zone=zone, mtime=uploaded_at,
        )
        return UploadResult(uploaded_at, file_object.checksum)


def delete_remote(api: PyiCloudService, remote_path: str) -> None:
    """Delete a file at remote_path (best-effort)."""
    node = resolve_node(api, remote_path)
    node.delete()


def ensure_remote_dir(api: PyiCloudService, remote_path: str) -> None:
    """Create remote directory tree if it doesn't exist."""
    parts = [p for p in PurePosixPath(remote_path).parts if p not in ("/", "")]
    node = api.drive.root
    for part in parts:
        try:
            node = node[part]
        except KeyError:
            try:
                node.mkdir(part)
            except Exception:
                # Another parallel upload may have created this directory.
                node = node[part]
            else:
                node._children = None
                node = node[part]
