import io
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from pyicloud import PyiCloudService

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


def list_remote(api: PyiCloudService, remote_path: str = "/", recursive: bool = False,
                depth: int | None = None) -> list[dict]:
    """Return a list of items under remote_path."""
    node = resolve_node(api, remote_path) if remote_path != "/" else api.drive.root
    if recursive:
        return _walk_parallel(node, "", max_depth=depth)
    return _walk(node, "", recursive=False)


def _walk(node, prefix: str, recursive: bool = False, depth: int | None = None,
          current_depth: int = 0) -> list[dict]:
    results = []
    try:
        children = node.get_children()
    except (KeyError, NotADirectoryError):
        return results
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


def _walk_parallel(root_node, root_prefix: str, max_depth: int | None = None) -> list[dict]:
    """Parallel recursive walk — fetches folder children concurrently."""
    results = []
    pending = [(root_node, root_prefix, 0)]

    while pending:
        batch = pending
        pending = []

        with ThreadPoolExecutor(max_workers=_LIST_WORKERS) as pool:
            futures = {pool.submit(_get_children, node, prefix): (node, prefix, d)
                       for node, prefix, d in batch}
            for future in as_completed(futures):
                _, _, d = futures[future]
                try:
                    children_items, subdirs = future.result()
                    results.extend(children_items)
                    if max_depth is None or d < max_depth:
                        pending.extend((n, p, d + 1) for n, p in subdirs)
                except Exception:
                    pass

    return results


def _get_children(node, prefix: str) -> tuple[list[dict], list[tuple]]:
    items = []
    subdirs = []
    try:
        children = node.get_children()
    except (KeyError, NotADirectoryError):
        return items, subdirs

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


def download_file(node, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    try:
        with node.open(stream=True) as r:
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        tmp.replace(dest)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise


def upload_file(api: PyiCloudService, local_path: Path, remote_dir: str) -> None:
    node = resolve_node(api, remote_dir) if remote_dir != "/" else api.drive.root
    with open(local_path, "rb") as raw:
        buf = io.BytesIO(raw.read())
        buf.name = local_path.name
        node.upload(buf)


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
            node.mkdir(part)
            node._children = None
            node = node[part]
