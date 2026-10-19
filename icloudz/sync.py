from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, FileSizeColumn, TaskID

from .drive import (list_remote, download_file, upload_file, ensure_remote_dir,
                    safe_local_path, delete_remote)
from .config import load as load_config
from . import state as st
from .errors import TransferError
from .filters import should_exclude as _should_exclude
from .filters import should_include as _should_include

console = Console()

_WORKERS = min(64, max(1, int(load_config().get("max_workers", 6))))


def _fetch_remote(api, remote_path: str, recursive: bool = True) -> list[dict]:
    with Progress(SpinnerColumn(), TextColumn("[dim]Fetching remote file list...[/dim]"),
                  console=console, transient=True) as p:
        p.add_task("", total=None)
        return list_remote(api, remote_path, recursive=recursive, max_workers=_WORKERS)


def pull(api, remote_path: str, local_dir: Path, dry_run: bool = False,
         pair: dict | None = None, resolve_local_wins: bool = False,
         propagate_local_deletes: bool = False) -> None:
    pair = pair or {}
    excludes = pair.get("excludes", [])
    selective = pair.get("selective", [])
    conflict = pair.get("conflict", "newest-wins")
    pair_name = pair.get("_state_key", pair.get("name", "default"))

    items = [i for i in _fetch_remote(api, remote_path) if not i.get("is_dir")]
    items = [i for i in items if not _should_exclude(i["path"], excludes)]
    items = [i for i in items if _should_include(i["path"], selective, remote_path)]

    if not items:
        console.print("[yellow]No files found remotely.[/yellow]")

    to_download = []
    to_upload = []
    to_delete = []
    resolved_local = []
    for item in items:
        rel = item["path"]
        dest = safe_local_path(local_dir, rel)
        remote_mtime = _parse_mtime(item["date_modified"])
        try:
            local_mtime = dest.stat().st_mtime if dest.exists() else 0.0
        except OSError:
            local_mtime = 0.0
        tracked = st.get(rel, pair_name)
        if (propagate_local_deletes and tracked and not dest.exists()
                and tracked.get("local_mtime") is not None and conflict != "remote-wins"):
            if not dry_run:
                try:
                    delete_remote(api, str(PurePosixPath(remote_path) / rel))
                    st.delete(rel, pair_name)
                except Exception as e:
                    console.print(f"[red]  failed to propagate deletion:[/red] {rel} — {e}")
            else:
                to_delete.append(rel)
            continue
        try:
            local_stat = dest.stat() if dest.exists() else None
        except OSError:
            local_stat = None
        local_changed = bool(
            tracked and local_stat
            and (local_mtime != tracked["local_mtime"]
                 or local_stat.st_size != tracked["local_size"])
        )
        remote_changed = st.remote_changed(rel, item["date_modified"], item["size"], pair_name)

        if dest.exists() and tracked:
            if conflict == "remote-wins":
                if not local_changed and not remote_changed:
                    continue
            elif conflict == "local-wins":
                if resolve_local_wins and remote_changed:
                    if dry_run:
                        to_upload.append((dest, rel))
                    else:
                        remote_dir = str(PurePosixPath(remote_path) / PurePosixPath(rel).parent)
                        try:
                            ensure_remote_dir(api, remote_dir)
                            uploaded = upload_file(api, dest, remote_dir)
                            local_stat = dest.stat()
                            st.record_local(rel, local_stat.st_mtime, local_stat.st_size,
                                            uploaded.checksum, pair_name,
                                            remote_mtime=uploaded.uploaded_at)
                            resolved_local.append(rel)
                        except Exception as e:
                            console.print(f"[red]  failed to preserve local version:[/red] {rel} — {e}")
                continue
            elif not remote_changed or (local_changed and local_mtime >= remote_mtime):
                continue
        elif dest.exists() and not tracked:
            if conflict == "local-wins":
                continue
            elif conflict == "newest-wins" and local_mtime >= remote_mtime:
                continue
        to_download.append(item)

    if dry_run:
        if not to_download and not to_upload and not to_delete:
            console.print("[green]Already up to date.[/green]")
            return
        if to_download:
            total_bytes = sum(i["size"] or 0 for i in to_download)
            console.print(f"[yellow]Dry run:[/yellow] would pull {len(to_download)} file(s), "
                          f"{_fmt_bytes(total_bytes)} total")
        for item in to_download:
            console.print(f"  [dim]{item['path']}[/dim]")
        if to_upload:
            console.print(f"[yellow]Dry run:[/yellow] would upload/preserve {len(to_upload)} local file(s) to iCloud")
            for _, rel in to_upload:
                console.print(f"  [dim]{rel}[/dim]")
        if to_delete:
            console.print(f"[yellow]Dry run:[/yellow] would delete {len(to_delete)} remote file(s)")
            for rel in to_delete:
                console.print(f"  [dim]{rel}[/dim]")
        return

    if to_download:
        failed = []
        with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                      BarColumn(), FileSizeColumn(), console=console) as progress:
            tasks: dict[str, TaskID] = {
                item["path"]: progress.add_task(f"[cyan]{item['path']}", total=item["size"] or 1)
                for item in to_download
            }

            def _download(item: dict) -> None:
                rel = item["path"]
                dest = safe_local_path(local_dir, rel)
                if dest.exists():
                    tracked = st.get(rel, pair_name)
                    if tracked and tracked.get("local_mtime") is not None:
                        try:
                            if dest.stat().st_mtime != tracked["local_mtime"]:
                                backup = dest.with_name(dest.name + ".conflict")
                                dest.rename(backup)
                                console.print(f"  [yellow]conflict backup:[/yellow] {backup.name}")
                        except OSError:
                            pass
                checksum = download_file(item["node"], dest)
                try:
                    s = dest.stat()
                    st.record_file(rel, s.st_mtime, s.st_size,
                                   item["date_modified"], item["size"], checksum, pair_name)
                except OSError:
                    pass

            with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
                futures = {pool.submit(_download, item): item for item in to_download}
                for future in as_completed(futures):
                    item = futures[future]
                    rel = item["path"]
                    try:
                        future.result()
                    except Exception as e:
                        console.print(f"[red]  failed:[/red] {rel} — {e}")
                        failed.append(rel)
                    finally:
                        progress.update(tasks[rel], completed=item["size"] or 1)

        console.print(f"[green]Pull complete.[/green] {len(to_download)} file(s) downloaded.")
        if failed:
            raise TransferError(f"failed to download {len(failed)} file(s)")
    else:
        if resolved_local:
            console.print(f"[green]Preserved {len(resolved_local)} local conflict(s) in iCloud.[/green]")
        else:
            console.print("[green]Already up to date.[/green]")

    # delete local files that were removed from remote
    remote_paths = {i["path"] for i in items}
    deleted = 0
    for record in st.all_tracked(pair_name):
        rel = record["path"]
        if record["local_mtime"] is None:
            continue  # never synced from remote, skip
        if _should_exclude(rel, excludes) or not _should_include(rel, selective, remote_path):
            continue  # outside current filter scope
        if rel not in remote_paths:
            local_file = safe_local_path(local_dir, rel)
            local_changed = False
            if local_file.exists() and record:
                try:
                    local_stat = local_file.stat()
                    local_changed = (local_stat.st_mtime != record["local_mtime"]
                                     or local_stat.st_size != record["local_size"])
                except OSError:
                    local_changed = True
            preserve_local = local_file.exists() and (
                local_changed or conflict == "local-wins"
            )
            if local_file.exists() and not preserve_local:
                local_file.unlink(missing_ok=True)
                deleted += 1
                console.print(f"  [dim]deleted locally (removed from remote):[/dim] {rel}")
            st.delete(rel, pair_name)
    if deleted:
        console.print(f"[green]Deleted {deleted} local file(s) removed from remote.[/green]")


def push(api, local_dir: Path, remote_path: str, dry_run: bool = False,
         pair: dict | None = None) -> None:
    pair = pair or {}
    excludes = pair.get("excludes", [])
    selective = pair.get("selective", [])
    pair_name = pair.get("_state_key", pair.get("name", "default"))

    local_files = [p for p in local_dir.rglob("*") if p.is_file() and not p.is_symlink()]

    if not local_files:
        console.print("[yellow]No local files to push.[/yellow]")
        return

    to_upload = []
    for local_path in local_files:
        rel = str(PurePosixPath(local_path.relative_to(local_dir)))
        if _should_exclude(rel, excludes) or not _should_include(rel, selective, remote_path):
            continue
        try:
            s = local_path.stat()
        except OSError:
            continue
        if not st.needs_upload(rel, s.st_mtime, s.st_size, pair_name):
            continue
        to_upload.append((local_path, rel, s))

    if not to_upload:
        console.print("[green]Already up to date.[/green]")
        return

    if dry_run:
        total_bytes = sum(s.st_size for _, _, s in to_upload)
        console.print(f"[yellow]Dry run:[/yellow] would push {len(to_upload)} file(s), "
                      f"{_fmt_bytes(total_bytes)} total")
        for _, rel, _ in to_upload:
            console.print(f"  [dim]{rel}[/dim]")
        return

    console.print(f"[bold]Pushing {len(to_upload)} file(s) to[/bold] {remote_path} ...")

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  BarColumn(), console=console) as progress:
        failed = []
        tasks: dict[str, TaskID] = {
            rel: progress.add_task(f"[cyan]{rel}", total=s.st_size)
            for _, rel, s in to_upload
        }

        def _upload(local_path: Path, rel: str, observed_stat) -> None:
            remote_dir = str(PurePosixPath(remote_path) / PurePosixPath(rel).parent)
            ensure_remote_dir(api, remote_dir)
            uploaded = upload_file(api, local_path, remote_dir)
            try:
                s = local_path.stat()
                if s.st_mtime != observed_stat.st_mtime or s.st_size != observed_stat.st_size:
                    return
                st.record_local(rel, s.st_mtime, s.st_size, uploaded.checksum, pair_name,
                                remote_mtime=uploaded.uploaded_at)
            except FileNotFoundError:
                st.delete(rel, pair_name)
            except OSError:
                pass

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = {pool.submit(_upload, lp, rel, s): (lp, rel, s)
                       for lp, rel, s in to_upload}
            for future in as_completed(futures):
                local_path, rel, s = futures[future]
                try:
                    future.result()
                except Exception as e:
                    console.print(f"[red]  failed:[/red] {rel} — {e}")
                    failed.append(rel)
                finally:
                    progress.update(tasks[rel], completed=s.st_size)

    console.print("[green]Push complete.[/green]")
    if failed:
        raise TransferError(f"failed to upload {len(failed)} file(s)")


def sync(api, local_dir: Path, remote_path: str, dry_run: bool = False,
         pair: dict | None = None) -> None:
    console.print("[bold]Starting bidirectional sync...[/bold]")
    pull(api, remote_path, local_dir, dry_run=dry_run, pair=pair,
         resolve_local_wins=True, propagate_local_deletes=True)
    push(api, local_dir, remote_path, dry_run=dry_run, pair=pair)
    console.print("[bold green]Sync complete.[/bold green]")


def _parse_mtime(date_str: str) -> float:
    from datetime import datetime
    try:
        dt = datetime.fromisoformat(str(date_str).replace("Z", "+00:00"))
        return dt.timestamp()
    except Exception:
        return 0.0


def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"
