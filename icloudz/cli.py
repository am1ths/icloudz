import sys
from pathlib import Path

import click
from rich.console import Console
from rich.table import Table

console = Console()


@click.group()
def main():
    """icloudz — iCloud Drive sync tool."""


# ── Auth ──────────────────────────────────────────────────────────────────────

@main.command()
@click.argument("apple_id")
def login(apple_id: str):
    """Authenticate with iCloud and save credentials."""
    from .auth import login as do_login
    try:
        do_login(apple_id)
        console.print(f"[green]Logged in as {apple_id}[/green]")
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@main.command()
@click.option("--apple-id", "-u", default=None)
def whoami(apple_id):
    """Show current iCloud account info."""
    from .auth import get_api
    try:
        api = get_api(apple_id)
        console.print(f"[green]Logged in[/green] as {api.account_name}")
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ── Config ────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--local-dir", "-l", default=None, help="Local directory")
@click.option("--remote", "-r", default=None, help="Remote iCloud Drive path")
@click.option("--interval", "-i", default=None, type=int, help="Poll interval in seconds")
@click.option("--max-workers", "-w", default=None, type=int, help="Parallel transfer workers")
@click.option("--apple-id", "-u", default=None, help="Apple ID")
@click.option("--notify/--no-notify", default=None, help="Desktop notifications")
@click.option("--conflict", default=None,
              type=click.Choice(["newest-wins", "remote-wins", "local-wins"]),
              help="Conflict resolution strategy")
@click.option("--exclude", "excludes", multiple=True, metavar="PATTERN",
              help="Exclude pattern (repeatable); replaces current list")
@click.option("--selective", "selective", multiple=True, metavar="PATH",
              help="Selective sync paths (repeatable); replaces current list")
@click.option("--pair-name", default=None, help="Which pair to configure (default: first pair)")
@click.option("--show", is_flag=True, help="Print current configuration as JSON")
def configure(local_dir, remote, interval, max_workers, apple_id, notify,
              conflict, excludes, selective, pair_name, show):
    """Set daemon configuration (saved to ~/.config/icloudz/config.json)."""
    from . import config as cfg_mod
    cfg = cfg_mod.load()
    if show:
        import json
        console.print(json.dumps(cfg, indent=2))
        return
    pairs = cfg_mod.get_pairs(cfg)
    if pair_name:
        idx = next((i for i, p in enumerate(pairs) if p["name"] == pair_name), None)
        if idx is None:
            console.print(f"[red]Error:[/red] pair {pair_name!r} not found. Use list-pairs.")
            sys.exit(1)
    else:
        idx = 0
    pair = pairs[idx] if pairs else dict(cfg_mod._PAIR_DEFAULTS)
    changed_pair = False
    if local_dir:
        pair["local_dir"] = str(Path(local_dir).expanduser().resolve())
        changed_pair = True
    if remote:
        pair["remote_path"] = remote
        changed_pair = True
    if conflict:
        pair["conflict"] = conflict
        changed_pair = True
    if excludes:
        pair["excludes"] = list(excludes)
        changed_pair = True
    if selective:
        pair["selective"] = list(selective)
        changed_pair = True
    if changed_pair:
        pairs[idx] = pair
        cfg["pairs"] = pairs
    if interval:
        cfg["poll_interval"] = interval
    if max_workers:
        cfg["max_workers"] = max_workers
    if apple_id:
        cfg["apple_id"] = apple_id
    if notify is not None:
        cfg["notify"] = notify
    try:
        cfg_mod.save(cfg)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    console.print("[green]Configuration saved.[/green]")
    for k, v in cfg.items():
        if k != "pairs":
            console.print(f"  {k} = {v}")
    for i, p in enumerate(cfg.get("pairs", [])):
        console.print(f"  pair[{i}] = {p}")


# ── Pair management ───────────────────────────────────────────────────────────

@main.command("list-pairs")
def list_pairs():
    """List all configured sync pairs."""
    from . import config as cfg_mod
    cfg = cfg_mod.load()
    pairs = cfg_mod.get_pairs(cfg)
    table = Table(title="Sync pairs", show_lines=False)
    table.add_column("Name", style="cyan")
    table.add_column("Local dir")
    table.add_column("Remote path")
    table.add_column("Conflict")
    table.add_column("Selective")
    for p in pairs:
        sel = ", ".join(p.get("selective") or []) or "[dim]all[/dim]"
        table.add_row(p["name"], p["local_dir"], p["remote_path"], p["conflict"], sel)
    console.print(table)


@main.command("add-pair")
@click.argument("name")
@click.option("--local-dir", "-l", required=True, help="Local directory")
@click.option("--remote", "-r", default="/", show_default=True, help="Remote iCloud Drive path")
@click.option("--conflict", default="newest-wins",
              type=click.Choice(["newest-wins", "remote-wins", "local-wins"]),
              help="Conflict resolution strategy")
def add_pair(name, local_dir, remote, conflict):
    """Add a new sync pair."""
    from . import config as cfg_mod
    cfg = cfg_mod.load()
    pairs = cfg_mod.get_pairs(cfg)
    if any(p["name"] == name for p in pairs):
        console.print(f"[red]Error:[/red] pair {name!r} already exists")
        sys.exit(1)
    pair = {
        **cfg_mod._PAIR_DEFAULTS,
        "name": name,
        "local_dir": str(Path(local_dir).expanduser().resolve()),
        "remote_path": remote,
        "conflict": conflict,
    }
    cfg["pairs"] = pairs + [pair]
    try:
        cfg_mod.save(cfg)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)
    console.print(f"[green]Pair {name!r} added.[/green]")


@main.command("remove-pair")
@click.argument("name")
def remove_pair(name):
    """Remove a sync pair by name."""
    from . import config as cfg_mod
    cfg = cfg_mod.load()
    pairs = cfg_mod.get_pairs(cfg)
    new_pairs = [p for p in pairs if p["name"] != name]
    if len(new_pairs) == len(pairs):
        console.print(f"[red]Error:[/red] pair {name!r} not found")
        sys.exit(1)
    cfg["pairs"] = new_pairs
    cfg_mod.save(cfg)
    console.print(f"[green]Pair {name!r} removed.[/green]")


# ── Manual sync ───────────────────────────────────────────────────────────────

@main.command()
@click.option("--apple-id", "-u", default=None)
@click.option("--remote", "-r", default="/", show_default=True)
@click.option("--recursive", "-R", is_flag=True, help="List recursively")
def ls(apple_id, remote, recursive):
    """List files in iCloud Drive."""
    from .auth import get_api
    from .drive import list_remote
    try:
        api = get_api(apple_id)
        items = list_remote(api, remote, recursive=recursive)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    if not items:
        console.print("[yellow]No files found.[/yellow]")
        return

    table = Table(title=f"iCloud Drive: {remote}", show_lines=False)
    table.add_column("Path", style="cyan")
    table.add_column("Size", justify="right")
    table.add_column("Modified")
    for item in items:
        if item.get("is_dir"):
            table.add_row(f"[bold]{item['path']}[/bold]", "[dim]dir[/dim]", item["date_modified"])
        else:
            size = f"{item['size']:,}" if item["size"] else "-"
            table.add_row(item["path"], size, item["date_modified"])
    console.print(table)


def _load_pair(pair_name: str | None) -> dict:
    from . import config as cfg_mod
    cfg = cfg_mod.load()
    pairs = cfg_mod.get_pairs(cfg)
    if pair_name:
        for p in pairs:
            if p["name"] == pair_name:
                return p
        raise click.ClickException(f"Pair {pair_name!r} not found. Use list-pairs to see available pairs.")
    return pairs[0] if pairs else {}


@main.command()
@click.argument("local_dir", type=click.Path(file_okay=False))
@click.option("--apple-id", "-u", default=None)
@click.option("--remote", "-r", default=None)
@click.option("--pair", "-p", default=None, help="Use config from named pair (excludes/conflict/selective)")
@click.option("--dry-run", is_flag=True)
def pull(local_dir, apple_id, remote, pair, dry_run):
    """Download files from iCloud Drive to LOCAL_DIR."""
    from .auth import get_api
    from .sync import pull as do_pull
    try:
        pair_cfg = _load_pair(pair)
    except click.ClickException as e:
        console.print(f"[red]Error:[/red] {e.format_message()}")
        sys.exit(1)
    remote = remote or pair_cfg.get("remote_path", "/")
    local = Path(local_dir)
    local.mkdir(parents=True, exist_ok=True)
    try:
        api = get_api(apple_id)
        do_pull(api, remote, local, dry_run=dry_run, pair=pair_cfg)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@main.command()
@click.argument("local_dir", type=click.Path(exists=True, file_okay=False))
@click.option("--apple-id", "-u", default=None)
@click.option("--remote", "-r", default=None)
@click.option("--pair", "-p", default=None, help="Use config from named pair")
@click.option("--dry-run", is_flag=True)
def push(local_dir, apple_id, remote, pair, dry_run):
    """Upload files from LOCAL_DIR to iCloud Drive."""
    from .auth import get_api
    from .sync import push as do_push
    try:
        pair_cfg = _load_pair(pair)
    except click.ClickException as e:
        console.print(f"[red]Error:[/red] {e.format_message()}")
        sys.exit(1)
    remote = remote or pair_cfg.get("remote_path", "/")
    try:
        api = get_api(apple_id)
        do_push(api, Path(local_dir), remote, dry_run=dry_run, pair=pair_cfg)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@main.command()
@click.argument("local_dir", type=click.Path(file_okay=False))
@click.option("--apple-id", "-u", default=None)
@click.option("--remote", "-r", default=None)
@click.option("--pair", "-p", default=None, help="Use config from named pair")
@click.option("--dry-run", is_flag=True)
def sync(local_dir, apple_id, remote, pair, dry_run):
    """Bidirectional sync between LOCAL_DIR and iCloud Drive."""
    from .auth import get_api
    from .sync import sync as do_sync
    try:
        pair_cfg = _load_pair(pair)
    except click.ClickException as e:
        console.print(f"[red]Error:[/red] {e.format_message()}")
        sys.exit(1)
    remote = remote or pair_cfg.get("remote_path", "/")
    local = Path(local_dir)
    local.mkdir(parents=True, exist_ok=True)
    try:
        api = get_api(apple_id)
        do_sync(api, local, remote, dry_run=dry_run, pair=pair_cfg)
    except RuntimeError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


# ── Daemon ────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--foreground", "-f", is_flag=True, help="Run in foreground (don't fork)")
def start(foreground):
    """Start the background sync daemon."""
    from .daemon import get_pid, run
    if get_pid() is not None:
        console.print("[yellow]Daemon is already running.[/yellow]")
        sys.exit(0)
    console.print("[green]Starting daemon...[/green]")
    if foreground:
        run(foreground=True)
    else:
        run(foreground=False)
        console.print("[green]Daemon started.[/green] Logs: ~/.config/icloudz/daemon.log")


@main.command()
def stop():
    """Stop the background sync daemon."""
    from .daemon import stop_daemon, get_pid
    if get_pid() is None:
        console.print("[yellow]Daemon is not running.[/yellow]")
        sys.exit(0)
    if stop_daemon():
        console.print("[green]Daemon stopped.[/green]")
    else:
        console.print("[red]Failed to stop daemon.[/red]")
        sys.exit(1)


@main.command()
@click.option("--foreground", "-f", is_flag=True)
def restart(foreground):
    """Restart the background sync daemon."""
    from .daemon import stop_daemon, get_pid, run
    if get_pid() is not None:
        stop_daemon()
        console.print("Stopped old daemon.")
    console.print("[green]Starting daemon...[/green]")
    run(foreground=foreground)


@main.command("daemon-status")
def daemon_status():
    """Show daemon status and recent log lines."""
    from .daemon import get_pid
    from .config import LOG_FILE, read_status
    pid = get_pid()
    if pid:
        console.print(f"[green]Daemon running[/green] (PID {pid})")
    else:
        console.print("[yellow]Daemon not running[/yellow]")

    s = read_status()
    if s:
        console.print(f"  Last poll:  {s.get('last_poll', '—')}")
        if s.get("last_error"):
            console.print(f"  [red]Last error:[/red] {s['last_error']}")
        if s.get("backoff"):
            console.print(f"  [yellow]Backoff:[/yellow] {s['backoff']}s")

    if LOG_FILE.exists():
        lines = LOG_FILE.read_text().splitlines()
        console.print(f"\n[bold]Last 20 log lines[/bold] ({LOG_FILE}):")
        for line in lines[-20:]:
            console.print(f"  [dim]{line}[/dim]")


@main.command("install-service")
def install_service():
    """Install a systemd user service for auto-start on login."""
    _write_systemd_unit()


# ── State ─────────────────────────────────────────────────────────────────────

@main.command()
def status():
    """Show locally tracked files."""
    from .state import all_tracked
    records = all_tracked()
    if not records:
        console.print("[yellow]No files tracked yet.[/yellow]")
        return
    table = Table(title="Tracked files", show_lines=False)
    table.add_column("Pair", style="dim")
    table.add_column("Path", style="cyan")
    table.add_column("Local size", justify="right")
    table.add_column("Remote size", justify="right")
    table.add_column("Checksum", style="dim")
    for r in records:
        table.add_row(
            r["pair"],
            r["path"],
            str(r["local_size"] or "-"),
            str(r["remote_size"] or "-"),
            (r["checksum"] or "")[:12],
        )
    console.print(table)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_systemd_unit() -> None:
    import shutil
    unit_dir = Path.home() / ".config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    unit_file = unit_dir / "icloudz.service"

    exe = shutil.which("icloudz") or sys.executable + " -m icloudz"

    unit = f"""[Unit]
Description=iCloud Drive sync daemon
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={exe} start --foreground
Restart=on-failure
RestartSec=60
StartLimitIntervalSec=600
StartLimitBurst=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""
    unit_file.write_text(unit)
    console.print(f"[green]Service file written:[/green] {unit_file}")
    console.print("\nEnable and start with:")
    console.print("  [bold]systemctl --user daemon-reload[/bold]")
    console.print("  [bold]systemctl --user enable --now icloudz[/bold]")
    console.print("\nCheck status:")
    console.print("  [bold]systemctl --user status icloudz[/bold]")
    console.print("  [bold]journalctl --user -u icloudz -f[/bold]")
