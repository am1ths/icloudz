import fnmatch
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path, PurePosixPath

from watchdog.events import FileSystemEventHandler, FileSystemEvent
from watchdog.observers import Observer

from . import state as st
from .drive import upload_file, ensure_remote_dir, delete_remote

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
                 pair_name: str = "default", excludes: list[str] | None = None):
        self.api = api
        self.local_dir = local_dir
        self.remote_path = remote_path
        self.pair_name = pair_name
        self.excludes = excludes or []
        self._debounce: dict[str, float] = {}
        self._lock = threading.Lock()
        self._in_flight: set[str] = set()
        self._in_flight_lock = threading.Lock()

    def _rel(self, abs_path: str) -> str | None:
        try:
            return str(Path(abs_path).relative_to(self.local_dir))
        except ValueError:
            return None

    def _excluded(self, rel: str) -> bool:
        name = PurePosixPath(rel).name
        return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in self.excludes)

    def _schedule(self, abs_path: str) -> None:
        with self._lock:
            self._debounce[abs_path] = time.monotonic() + 1.5

    def flush(self, pool: ThreadPoolExecutor | None = None) -> None:
        now = time.monotonic()
        with self._lock:
            ready = [p for p, t in self._debounce.items() if t <= now]
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
        if self._excluded(rel):
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
            upload_file(self.api, path, remote_dir)
            checksum = st.file_checksum(path)
            st.record_local(rel, s.st_mtime, s.st_size, checksum, self.pair_name)
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
            self._schedule(event.dest_path)

    def on_deleted(self, event: FileSystemEvent) -> None:
        if not event.is_directory:
            rel = self._rel(event.src_path)
            if rel is None:
                return
            record = st.get(rel, self.pair_name)
            st.delete(rel, self.pair_name)
            log.info("[%s] untracked (deleted locally): %s", self.pair_name, rel)
            if record and record.get("remote_mtime"):
                full_remote = str(PurePosixPath(self.remote_path) / rel)
                try:
                    delete_remote(self.api, full_remote)
                    log.info("[%s] deleted from remote: %s", self.pair_name, rel)
                except Exception as e:
                    log.warning("[%s] remote delete failed for %s: %s", self.pair_name, rel, e)


class LocalWatcher:
    _UPLOAD_WORKERS = 4

    def __init__(self, api, local_dir: Path, remote_path: str,
                 pair_name: str = "default", excludes: list[str] | None = None):
        self._handler = _Handler(api, local_dir, remote_path, pair_name, excludes)
        self._observer = Observer()
        self._observer.schedule(self._handler, str(local_dir), recursive=True)
        self._pool = ThreadPoolExecutor(max_workers=self._UPLOAD_WORKERS,
                                        thread_name_prefix="watcher-upload")

    def start(self) -> None:
        self._observer.start()

    def stop(self) -> None:
        self._observer.stop()
        self._observer.join(timeout=5)
        self._pool.shutdown(wait=False)

    def flush(self) -> None:
        self._handler.flush(self._pool)
