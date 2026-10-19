import logging
import logging.handlers
import os
import shutil
import signal
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone

from .config import PID_FILE, LOG_FILE, load as load_config, get_pairs, write_status
from .auth import get_api, refresh_api
from .watcher import LocalWatcher, mark_poll_start, mark_poll_done
from .drive import (list_remote, download_file, safe_local_path, delete_remote,
                    ensure_remote_dir, upload_file)
from . import state as st
from .filters import should_exclude as _excluded
from .filters import should_include as _included
from .sync import push as push_local
from .errors import TransferError

log = logging.getLogger(__name__)

_MAX_BACKOFF = 300


def _setup_logging() -> None:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                LOG_FILE, maxBytes=10_000_000, backupCount=5
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )


def _notify(title: str, body: str) -> None:
    if shutil.which("notify-send"):
        try:
            subprocess.run(["notify-send", "-a", "icloudz", title, body],
                           timeout=3, capture_output=True)
        except Exception:
            pass


def _write_pid() -> None:
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()))


def _clear_pid() -> None:
    PID_FILE.unlink(missing_ok=True)


def _poll_remote(api, local_dir: Path, remote_path: str, pair_name: str = "default",
                 excludes: list[str] | None = None, selective: list[str] | None = None,
                 conflict: str = "newest-wins", max_workers: int = 4) -> int:
    """Pull remote changes for one pair. Returns number of files pulled."""
    excludes = excludes or []
    selective = selective or []

    api.drive.refresh_root()
    items = [i for i in list_remote(api, remote_path, recursive=True,
                                    max_workers=max_workers) if not i.get("is_dir")]

    items = [i for i in items if not _excluded(i["path"], excludes)
             and _included(i["path"], selective, remote_path)]

    to_pull = []
    for item in items:
        rel = item["path"]
        dest = safe_local_path(local_dir, rel)
        tracked = st.get(rel, pair_name)
        remote_mtime = _parse_mtime(item["date_modified"])
        try:
            local_stat = dest.stat() if dest.exists() else None
            local_mtime = local_stat.st_mtime if local_stat else 0.0
        except OSError:
            local_stat = None
            local_mtime = 0.0
        local_changed = bool(
            tracked and local_stat
            and (local_stat.st_mtime != tracked["local_mtime"]
                 or local_stat.st_size != tracked["local_size"])
        )
        remote_changed = st.remote_changed(
            rel, item["date_modified"], item["size"], pair_name
        )

        if (tracked and local_stat is None and conflict != "remote-wins"
                and tracked.get("local_mtime") is not None):
            try:
                delete_remote(api, str(PurePosixPath(remote_path) / rel))
                st.delete(rel, pair_name)
                log.info("[%s] deleted remotely (removed locally): %s", pair_name, rel)
            except Exception as e:
                log.warning("[%s] remote delete retry for %s failed: %s", pair_name, rel, e)
            continue

        if dest.exists() and tracked:
            if conflict == "remote-wins":
                if not local_changed and not remote_changed:
                    continue
            elif conflict == "local-wins":
                if tracked and remote_changed and dest.exists():
                    remote_dir = str(PurePosixPath(remote_path) / PurePosixPath(rel).parent)
                    try:
                        ensure_remote_dir(api, remote_dir)
                        uploaded = upload_file(api, dest, remote_dir)
                        local_stat = dest.stat()
                        st.record_local(rel, local_stat.st_mtime, local_stat.st_size,
                                        uploaded.checksum, pair_name,
                                        remote_mtime=uploaded.uploaded_at)
                        log.info("[%s] local-wins preserved: %s", pair_name, rel)
                    except Exception as e:
                        log.error("[%s] failed to preserve local version %s: %s",
                                  pair_name, rel, e)
                continue
            elif not remote_changed or (local_changed and local_mtime >= remote_mtime):
                continue
        elif dest.exists() and not tracked:
            if conflict == "local-wins":
                continue
            elif conflict == "newest-wins" and local_mtime >= remote_mtime:
                continue
        to_pull.append(item)

    # delete local files that were removed from remote
    remote_paths = {i["path"] for i in items}
    for record in st.all_tracked(pair_name):
        rel = record["path"]
        if record["local_mtime"] is None:
            continue
        if _excluded(rel, excludes) or not _included(rel, selective, remote_path):
            continue
        if rel not in remote_paths:
            local_file = safe_local_path(local_dir, rel)
            local_changed = False
            if local_file.exists():
                try:
                    local_stat = local_file.stat()
                    local_changed = (local_stat.st_mtime != record["local_mtime"]
                                     or local_stat.st_size != record["local_size"])
                except OSError:
                    local_changed = True
            preserve_local = local_file.exists() and (
                local_changed or conflict == "local-wins"
            )
            if preserve_local:
                remote_dir = str(PurePosixPath(remote_path) / PurePosixPath(rel).parent)
                try:
                    ensure_remote_dir(api, remote_dir)
                    uploaded = upload_file(api, local_file, remote_dir)
                    local_stat = local_file.stat()
                    st.record_local(rel, local_stat.st_mtime, local_stat.st_size,
                                    uploaded.checksum, pair_name,
                                    remote_mtime=uploaded.uploaded_at)
                    log.info("[%s] restored local version to iCloud: %s", pair_name, rel)
                except Exception as e:
                    log.error("[%s] failed to restore local file %s: %s", pair_name, rel, e)
                continue
            if local_file.exists():
                local_file.unlink(missing_ok=True)
                log.info("[%s] deleted locally (removed from remote): %s", pair_name, rel)
            st.delete(rel, pair_name)

    if not to_pull:
        return 0

    pulling_paths = {i["path"] for i in to_pull}
    mark_poll_start(pulling_paths)
    pulled = 0
    failed = 0

    def _download(item: dict) -> bool:
        rel = item["path"]
        dest = safe_local_path(local_dir, rel)
        try:
            if dest.exists():
                tracked = st.get(rel, pair_name)
                if tracked and tracked.get("local_mtime") is not None:
                    try:
                        if dest.stat().st_mtime != tracked["local_mtime"]:
                            backup = dest.with_name(dest.name + ".conflict")
                            dest.rename(backup)
                            log.info("[%s] conflict backup: %s", pair_name, backup.name)
                    except OSError:
                        pass
            checksum = download_file(item["node"], dest)
            s = dest.stat()
            st.record_file(rel, s.st_mtime, s.st_size,
                           item["date_modified"], item["size"], checksum, pair_name)
            log.info("[%s] pulled: %s", pair_name, rel)
            return True
        except Exception as e:
            log.error("[%s] failed to pull %s: %s", pair_name, rel, e)
            return False

    try:
        with ThreadPoolExecutor(max_workers=max(1, max_workers)) as pool:
            futures = [pool.submit(_download, item) for item in to_pull]
            for future in as_completed(futures):
                success = future.result()
                pulled += success
                if not success:
                    failed += 1
    finally:
        mark_poll_done(pulling_paths)

    if failed:
        raise TransferError(f"failed to download {failed} file(s) for pair {pair_name!r}")
    return pulled


_FATAL_AUTH_MSGS = (
    "No password found", "Apple ID not set", "Invalid email/password",
    "2FA requires an interactive terminal",
)
_AUTH_RETRY_DELAYS = [30, 60, 120, 300, 600]


def _get_api_with_retry(apple_id: str | None, stop_event: threading.Event):
    """Try to authenticate, retrying on transient errors (503, 421).
    Returns None if stop_event is set or a permanent credential error occurs."""
    for delay in _AUTH_RETRY_DELAYS + [None]:
        try:
            return get_api(apple_id)
        except RuntimeError as e:
            msg = str(e)
            if any(m in msg for m in _FATAL_AUTH_MSGS):
                log.error("authentication failed (credentials): %s", e)
                return None
            if delay is None:
                log.error("authentication failed after all retries: %s", e)
                return None
            log.warning("authentication failed (%s), retrying in %ds...", msg, delay)
            write_status({"last_error": msg, "backoff": delay})
            if stop_event.wait(timeout=delay):
                return None
    return None


def _parse_mtime(date_str: str) -> float:
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def run(foreground: bool = False) -> None:
    _setup_logging()
    cfg = load_config()
    pairs = get_pairs(cfg)
    poll_interval = max(1, int(cfg.get("poll_interval", 30)))
    max_workers = min(64, max(1, int(cfg.get("max_workers", 6))))
    apple_id = cfg.get("apple_id")
    notify = cfg.get("notify", True)

    for pair in pairs:
        Path(pair["local_dir"]).mkdir(parents=True, exist_ok=True)

    if not foreground:
        _daemonize()

    _write_pid()

    stop_event = threading.Event()

    def _handle_signal(sig, frame):
        log.info("received signal %s, shutting down", sig)
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    log.info("icloudz daemon starting — %d pair(s), poll: %ds", len(pairs), poll_interval)
    for p in pairs:
        log.info("  pair %r: %s → %s", p["name"], p["local_dir"], p["remote_path"])

    api = _get_api_with_retry(apple_id, stop_event)
    if api is None:
        _clear_pid()
        sys.exit(1)

    watchers = []
    for pair in pairs:
        local_dir = Path(pair["local_dir"])
        w = LocalWatcher(api, local_dir, pair["remote_path"],
                         pair_name=pair["name"], excludes=pair.get("excludes", []),
                         selective=pair.get("selective", []),
                         max_workers=max_workers)
        w.start()
        watchers.append((pair, w))
        log.info("[%s] watching %s", pair["name"], local_dir)

    last_poll = time.monotonic()
    backoff = 0
    poll_lock = threading.Lock()

    def _run_poll(initial_sync: bool = False):
        nonlocal backoff, last_poll, api
        if not poll_lock.acquire(blocking=False):
            return
        t0 = time.monotonic()
        total_pulled = 0
        try:
            api = refresh_api(api, apple_id)
            for pair in pairs:
                local_dir = Path(pair["local_dir"])
                n = _poll_remote(
                    api, local_dir, pair["remote_path"],
                    pair_name=pair["name"],
                    excludes=pair.get("excludes", []),
                    selective=pair.get("selective", []),
                    conflict=pair.get("conflict", "newest-wins"),
                    max_workers=max_workers,
                )
                total_pulled += n
                if initial_sync:
                    push_local(api, local_dir, pair["remote_path"], pair=pair)
            elapsed = time.monotonic() - t0
            log.info("poll done — pulled %d file(s) in %.1fs", total_pulled, elapsed)
            if notify and total_pulled > 0:
                _notify("icloudz", f"Pulled {total_pulled} file(s)")
            backoff = 0
            write_status({
                "pid": os.getpid(),
                "last_poll": datetime.now(timezone.utc).isoformat(),
                "last_error": None,
                "backoff": 0,
            })
        except TransferError as e:
            backoff = min(backoff * 2 + 30, _MAX_BACKOFF)
            log.error("transfer error (retry in %ds): %s", backoff, e)
            write_status({"pid": os.getpid(), "last_error": str(e), "backoff": backoff})
        except RuntimeError as e:
            backoff = _MAX_BACKOFF
            log.error("authentication error (retry in %ds): %s", backoff, e)
            write_status({"pid": os.getpid(), "last_error": str(e), "backoff": backoff})
        except Exception as e:
            backoff = min(backoff * 2 + 30, _MAX_BACKOFF)
            log.error("poll error (retry in %ds): %s", backoff, e)
            write_status({"pid": os.getpid(), "last_error": str(e), "backoff": backoff})
        finally:
            last_poll = time.monotonic()
            poll_lock.release()

    log.info("initial sync...")
    threading.Thread(target=_run_poll, kwargs={"initial_sync": True},
                     daemon=True, name="initial-sync").start()

    try:
        while not stop_event.is_set():
            for _, w in watchers:
                w.flush()

            if time.monotonic() - last_poll >= poll_interval + backoff:
                log.info("polling remote...")
                threading.Thread(target=_run_poll, daemon=True, name="poll").start()

            stop_event.wait(timeout=1)
    finally:
        for _, w in watchers:
            w.stop()
        _clear_pid()
        log.info("daemon stopped")


def _daemonize() -> None:
    if not hasattr(os, "fork"):
        return
    if os.fork() > 0:
        sys.exit(0)
    os.setsid()
    if os.fork() > 0:
        sys.exit(0)
    sys.stdout.flush()
    sys.stderr.flush()
    with open(os.devnull, "r+") as devnull:
        os.dup2(devnull.fileno(), sys.stdin.fileno())
        os.dup2(devnull.fileno(), sys.stdout.fileno())
        os.dup2(devnull.fileno(), sys.stderr.fileno())
    root_logger = logging.getLogger()
    root_logger.handlers = [h for h in root_logger.handlers
                            if not isinstance(h, logging.StreamHandler)
                            or isinstance(h, logging.FileHandler)]


def get_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
        if not any(arg.rsplit(b"/", 1)[-1] == b"icloudz" for arg in cmdline):
            PID_FILE.unlink(missing_ok=True)
            return None
        return pid
    except (ValueError, ProcessLookupError, PermissionError, FileNotFoundError):
        PID_FILE.unlink(missing_ok=True)
        return None


def stop_daemon() -> bool:
    pid = get_pid()
    if pid is None:
        return False
    os.kill(pid, signal.SIGTERM)
    for _ in range(30):
        time.sleep(0.5)
        if get_pid() is None:
            return True
    os.kill(pid, signal.SIGKILL)
    _clear_pid()
    return True
