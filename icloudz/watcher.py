import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from . import state as st
from .drive import upload_file, ensure_remote_dir, delete_remote, safe_local_path
from .filters import should_exclude, should_include

log = logging.getLogger(__name__)

# Flag set while a poll is downloading — watcher skips conflicting files
_poll_active: set[str] = set()
_poll_lock = threading.Lock()


def mark_poll_start(paths: set[str]) -> None:
    with _poll_lock:
        _poll_active.update(paths)


def mark_poll_done(paths: set[str]) -> None:
    with _poll_lock:
        _poll_active.difference_update(paths)


class _Handler(FileSystemEventHandler):
    def __init__(self, api, local_dir: Path, remote_path: str,
                 pair_name: str = "default", excludes: list[str] | None = None,
                 selective: list[str] | None = None):
        self.api = api
        self.local_dir = local_dir
        self.remote_path = remote_path
        self.pair_name = pair_name
        self.excludes = excludes or []
        self.selective = selective or []
        self._debounce: dict[str, float] = {}
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()

    def _rel(self, abs_path: str) -> str | None:
        try:
            return str(Path(abs_path).relative_to(self.local_dir))
        except ValueError:
            return None

    def _schedule(self, abs_path: str) -> None:
        with self._lock:
            self._debounce[abs_path] = time.monotonic() + 1.5

    def flush(self, pool: ThreadPoolExecutor | None = None, force: bool = False) -> None:
        now = time.monotonic()
        with self._lock:
            ready = [p for p, t in self._debounce.items() if force or t <= now]
            for p in ready:
                del self._debounce[p]
        if pool:
            for abs_path in ready:
                pool.submit(self._upload, abs_path)
        else:
            for abs_path in ready:
                self._upload(abs_path)

    def _upload(self, abs_path: str) -> None:
        path = Path(abs_path)
        if not path.exists() or path.is_dir():
            return
        rel = self._rel(abs_path)
        if rel is None:
            return
        if should_exclude(rel, self.excludes) or not should_include(
            rel, self.selective, self.remote_path
        ):
            return
        try:
            safe_local_path(self.local_dir, rel)
        except ValueError:
            log.warning("[%s] refusing path outside sync root: %s", self.pair_name, rel)
            return
        with _poll_lock:
            if rel in _poll_active:
                log.debug("skip upload — poll in progress for %s", rel)
                self._schedule(abs_path)
                return
        with self._in_flight_lock:
            if rel in self._in_flight:
                self._schedule(abs_path)
                return
            self._in_flight.add(rel)
        try:
            s = path.stat()
            if not st.needs_upload(rel, s.st_mtime, s.st_size, self.pair_name):
                return
            remote_dir = str(PurePosixPath(self.remote_path) / PurePosixPath(rel).parent)
            ensure_remote_dir(self.api, remote_dir)
            uploaded = upload_file(self.api, path, remote_dir)
            current_stat = path.stat()
            if current_stat.st_mtime != s.st_mtime or current_stat.st_size != s.st_size:
                self._schedule(abs_path)
                return
            st.record_local(rel, s.st_mtime, s.st_size, uploaded.checksum, self.pair_name,
                            remote_mtime=uploaded.uploaded_at)
            log.info("[%s] pushed: %s", self.pair_name, rel)
        except FileNotFoundError:
            st.delete(rel, self.pair_name)
            log.info("[%s] untracked (deleted during upload): %s", self.pair_name, rel)
        except OSError as e:
            log.warning("[%s] upload skipped %s: %s", self.pair_name, rel, e)
        except Exception as e:
            log.error("[%s] failed to push %s: %s", self.pair_name, rel, e)
        finally:
            with self._in_flight_lock:
                self._in_flight.discard(rel)

    def on_created(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_modified(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            self._schedule(event.src_path)

    def on_moved(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            old_rel = self._rel(event.src_path)
            if old_rel and should_include(old_rel, self.selective, self.remote_path):
                self._remove_remote(old_rel)
            self._schedule(event.dest_path)

    def _remove_remote(self, rel: str) -> None:
        if should_exclude(rel, self.excludes):
            return
        record = st.get(rel, self.pair_name)
        if record and record.get("local_mtime") is not None:
            full_remote = str(PurePosixPath(self.remote_path) / rel)
            try:
                delete_remote(self.api, full_remote)
                log.info("[%s] deleted from remote: %s", self.pair_name, rel)
            except Exception as e:
                log.warning("[%s] remote delete failed for %s: %s", self.pair_name, rel, e)
                return
        st.delete(rel, self.pair_name)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            rel = self._rel(event.src_path)
            if rel is None:
                return
            if should_exclude(rel, self.excludes) or not should_include(
                rel, self.selective, self.remote_path
            ):
                return
            self._remove_remote(rel)


class LocalWatcher:
    _UPLOAD_WORKERS = 4

    def __init__(self, api, local_dir: Path, remote_path: str,
                 pair_name: str = "default", excludes: list[str] | None = None,
                 selective: list[str] | None = None,
                 max_workers: int = _UPLOAD_WORKERS):
        self._handler = _Handler(api, local_dir, remote_path, pair_name, excludes, selective)
        self._observer = Observer()
        self._observer.schedule(self._handler, str(local_dir), recursive=True)
        self._pool = ThreadPoolExecutor(max_workers=max(1, max_workers),
                                        thread_name_prefix="watcher-upload")

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join()
        self._handler.flush(self._pool, force=True)
        self._pool.shutdown(wait=True)

    def flush(self) -> None:
        self._handler.flush(self._pool)
