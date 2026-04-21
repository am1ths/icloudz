# icloudz

iCloud Drive sync CLI for Linux. Pull files from iCloud, push local changes back, or run a background daemon that keeps a local directory in sync automatically — just like the native client on macOS and Windows.

## How it works

icloudz uses the iCloud web API (via [pyicloud](https://github.com/picklepete/pyicloud)) to communicate with Apple's servers. It tracks file state in a local SQLite database and uses `watchdog` to detect local changes in real time.

## Requirements

- Python 3.10+
- A working iCloud account with two-factor authentication enabled

## Installation

```bash
git clone https://github.com/am1ths/icloudz.git
cd icloudz
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

## Authentication

icloudz uses your regular Apple ID password. App-specific passwords are **not** supported — use the same password you use to sign in to iCloud.com.

```bash
icloudz login your@apple.id
# Enter your Apple ID password
# A 2FA code will be sent to your trusted device — enter it when prompted
```

Credentials are saved to the system keyring. The session is stored in `~/.config/icloudz/session/` and reused automatically.

### Credentials via .env (recommended for daemon use)

Create `~/.config/icloudz/.env` with permissions set to `600`:

```bash
cat > ~/.config/icloudz/.env << 'EOF'
ICLOUDZ_APPLE_ID=your@apple.id
ICLOUDZ_PASSWORD=your-apple-id-password
EOF
chmod 600 ~/.config/icloudz/.env
```

## Configuration

```bash
icloudz configure \
  --local-dir ~/iCloud \
  --remote / \
  --interval 30 \
  --apple-id your@apple.id
```

Settings are saved to `~/.config/icloudz/config.json`.

| Option | Default | Description |
|--------|---------|-------------|
| `--local-dir` | `~/iCloud` | Local directory (default pair) |
| `--remote` | `/` | Remote iCloud Drive path (default pair) |
| `--interval` | `30` | Remote poll interval in seconds |
| `--max-workers` | `6` | Parallel transfer workers |
| `--apple-id` | — | Apple ID (overrides .env) |
| `--conflict` | `newest-wins` | Conflict strategy: `newest-wins`, `remote-wins`, `local-wins` |
| `--exclude PATTERN` | built-in set | Exclude pattern (repeatable, replaces list) |
| `--selective PATH` | — | Only sync this path (repeatable) |
| `--notify/--no-notify` | on | Desktop notifications via `notify-send` |
| `--pair-name NAME` | first pair | Which pair to edit |
| `--show` | — | Print current config as JSON |

### Multiple sync pairs

Sync different local directories to different iCloud folders, each with its own settings:

```bash
# Add pairs
icloudz add-pair photos --local-dir ~/Photos --remote /Photos --conflict remote-wins
icloudz add-pair docs   --local-dir ~/Docs   --remote /Documents

# Configure a specific pair
icloudz configure --pair-name photos --exclude "*.raw" --exclude "*.cr2"

# Selective sync (only these subdirectories)
icloudz configure --pair-name docs --selective /Documents/Work --selective /Documents/Personal

# List all pairs
icloudz list-pairs

# Remove a pair
icloudz remove-pair photos
```

The daemon automatically handles all pairs.

### Conflict resolution

When both local and remote copies change between syncs:

- `newest-wins` (default): the more recently modified version wins. If the local copy loses, it is saved as `filename.conflict` before being overwritten.
- `remote-wins`: remote always wins, local changes are overwritten.
- `local-wins`: local always wins, remote changes are never pulled.

## Commands

### List files

```bash
icloudz ls                        # top-level contents
icloudz ls --remote /Documents    # specific folder
icloudz ls -R                     # recursive listing
```

### Manual sync

```bash
# Download from iCloud to local directory
icloudz pull ~/iCloud

# Upload from local directory to iCloud
icloudz push ~/iCloud

# Bidirectional sync
icloudz sync ~/iCloud

# Preview without making changes
icloudz sync ~/iCloud --dry-run
```

All sync commands accept `--remote` to target a specific iCloud Drive folder:

```bash
icloudz pull ~/Documents --remote /Documents
```

Pass `--pair NAME` to apply the excludes, conflict strategy, and selective paths from a named pair:

```bash
icloudz sync ~/Docs --pair docs
```

### Status

```bash
icloudz whoami          # show logged-in account
icloudz status          # show tracked files and checksums
icloudz daemon-status   # show daemon status and recent log lines
```

## Background daemon

The daemon watches the local directory for changes and uploads them immediately. It also polls iCloud every N seconds to download new or updated remote files.

### Start / stop

```bash
icloudz start           # start in background
icloudz start -f        # start in foreground (useful for debugging)
icloudz stop
icloudz restart
```

Logs are written to `~/.config/icloudz/daemon.log`.

### Run as a systemd user service (auto-start on login)

```bash
icloudz install-service

systemctl --user daemon-reload
systemctl --user enable --now icloudz
```

### Run without being logged in (server / headless)

Enable systemd linger so the user session starts at boot:

```bash
sudo loginctl enable-linger $USER
```

Then install and enable the service as shown above. The daemon will start automatically when the system boots, even if no user session is active.

Check status and logs:

```bash
systemctl --user status icloudz
journalctl --user -u icloudz -f
```

## Sync behavior

- **Pull**: downloads files newer on iCloud or absent locally. Respects conflict strategy, exclude patterns, and selective paths.
- **Push**: uploads files whose `mtime` or `size` changed since last sync. Unchanged files are skipped without reading contents.
- **Delete sync**: files deleted locally are removed from iCloud (best-effort). Files deleted from iCloud are removed locally on the next poll.
- **Daemon**: local changes are pushed within ~2 seconds (debounced, parallel uploads). Remote changes are pulled on each poll interval. Exponential backoff on errors, automatic session re-authentication.

File state is tracked in `~/.config/icloudz/state.db`.
Logs rotate at 10 MB, keeping 5 files.

## Project structure

```
icloudz/
├── auth.py      — authentication, 2FA, keyring and .env credential loading
├── cli.py       — Click-based CLI entry point
├── config.py    — configuration file management
├── daemon.py    — background daemon with signal handling and backoff
├── drive.py     — iCloud Drive API wrappers (list, download, upload)
├── state.py     — SQLite state tracking
├── sync.py      — pull / push / sync logic with parallel transfers
└── watcher.py   — local filesystem watcher (watchdog)
```

## License

MIT
