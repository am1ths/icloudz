import fnmatch
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, FileSizeColumn, TaskID

from .drive import list_remote, download_file, upload_file, ensure_remote_dir
from .config import load as load_config
from . import state as st

console = Console()

_WORKERS = load_config().get("max_workers", 6)


def _should_exclude(rel: str, excludes: list[str]) -> bool:
    name = PurePosixPath(rel).name
    return any(fnmatch.fnmatch(name, p) or fnmatch.fnmatch(rel, p) for p in excludes)


def _should_include(rel: str, selective: list[str]) -> bool:
    if not selective:
        return True
    prefixes = [s.lstrip("/") for s in selective]
    return any(rel.startswith(p + "/") or rel == p or rel.startswith(p) for p in prefixes)


def _fetch_remote(api, remote_path: str, recursive: bool = True) -> list[dict]:
    with Progress(SpinnerColumn(), TextColumn("[dim]Fetching remote file list...[/dim]"),
                  console=console, transient=True) as p:
        p.add_task("", total=None)
        return list_remote(api, remote_path, recursive=recursive)


def pull(api, remote_path: str, local_dir: Path, dry_run: bool = False,
         pair: dict | None = None) -> None:
    pair = pair or {}
    excludes = pair.get("excludes", [])
    selective = pair.get("selective", [])
    conflict = pair.get("conflict", "newest-wins")
    pair_name = pair.get("name", "default")

    items = [i for i in _fetch_remote(api, remote_path) if not i.get("is_dir")]
    items = [i for i in items if not _should_exclude(i["path"], excludes)]
    items = [i for i in items if _should_include(i["path"], selective)]

    if not items:
        console.print("[yellow]No files found remotely.[/yellow]")
        return

    to_download = []
    for item in items:
        rel = item["path"]
        dest = local_dir / rel
        remote_mtime = _parse_mtime(item["date_modified"])
        try:
            local_mtime = dest.stat().st_mtime if dest.exists() else 0.0
        except OSError:
            local_mtime = 0.0
        tracked = st.get(rel, pair_name)

        if dest.exists() and tracked:
            if conflict == "remote-wins":
                pass
            elif conflict == "local-wins":
                continue
            else:  # newest-wins
                if local_mtime >= remote_mtime:
                    continue
        elif dest.exists() and not tracked:
            if conflict == "local-wins":
                continue
            elif conflict == "newest-wins" and local_mtime >= remote_mtime:
                continue
        to_download.append(item)

    if not to_download:
        console.print("[green]Already up to date.[/green]")
        return

    if dry_run:
        total_bytes = sum(i["size"] or 0 for i in to_download)
        console.print(f"[yellow]Dry run:[/yellow] would pull {len(to_download)} file(s), "
                      f"{_fmt_bytes(total_bytes)} total")
        for item in to_download:
            console.print(f"  [dim]{item['path']}[/dim]")
        return

    with Progress(SpinnerColumn(), TextColumn("{task.description}"),
                  BarColumn(), FileSizeColumn(), console=console) as progress:
        tasks: dict[str, TaskID] = {
            item["path"]: progress.add_task(f"[cyan]{item['path']}", total=item["size"] or 1)
            for item in to_download
        }

        def _download(item: dict) -> None:
            rel = item["path"]
            dest = local_dir / rel
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
            download_file(item["node"], dest)
            try:
                s = dest.stat()
                st.record_file(rel, s.st_mtime, s.st_size,
                               item["date_modified"], item["size"],
                               st.file_checksum(dest), pair_name)
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
                finally:
                    progress.update(tasks[rel], completed=item["size"] or 1)

    console.print(f"[green]Pull complete.[/green] {len(to_download)} file(s) downloaded.")

    # delete local files that were removed from remote
    remote_paths = {i["path"] for i in items}
    deleted = 0
    for record in st.all_tracked(pair_name):
        rel = record["path"]
        if not record["remote_mtime"]:
            continue  # never synced from remote, skip
        if _should_exclude(rel, excludes) or not _should_include(rel, selective):
            continue  # outside current filter scope
        if rel not in remote_paths:
            local_file = local_dir / rel
            if local_file.exists():
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
    pair_name = pair.get("name", "default")

    local_files = [p for p in local_dir.rglob("*") if p.is_file()]

    if not local_files:
        console.print("[yellow]No local files to push.[/yellow]")
        return

    to_upload = []
    for local_path in local_files:
        rel = str(PurePosixPath(local_path.relative_to(local_dir)))
        if _should_exclude(rel, excludes):
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
        tasks: dict[str, TaskID] = {
            rel: progress.add_task(f"[cyan]{rel}", total=s.st_size)
            for _, rel, s in to_upload
        }

        def _upload(local_path: Path, rel: str) -> None:
            remote_dir = str(PurePosixPath(remote_path) / PurePosixPath(rel).parent)
            ensure_remote_dir(api, remote_dir)
            upload_file(api, local_path, remote_dir)
            try:
                s = local_path.stat()
                checksum = st.file_checksum(local_path)
                st.record_local(rel, s.st_mtime, s.st_size, checksum, pair_name)
            except FileNotFoundError:
                st.delete(rel, pair_name)
            except OSError:
                pass

        with ThreadPoolExecutor(max_workers=_WORKERS) as pool:
            futures = {pool.submit(_upload, lp, rel): (lp, rel, s)
                       for lp, rel, s in to_upload}
            for future in as_completed(futures):
                local_path, rel, s = futures[future]
                try:
                    future.result()
                except Exception as e:
                    console.print(f"[red]  failed:[/red] {rel} — {e}")
                finally:
                    progress.update(tasks[rel], completed=s.st_size)

    console.print("[green]Push complete.[/green]")


def sync(api, local_dir: Path, remote_path: str, dry_run: bool = False,
         pair: dict | None = None) -> None:
    console.print("[bold]Starting bidirectional sync...[/bold]")
    pull(api, remote_path, local_dir, dry_run=dry_run, pair=pair)
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
