import json
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "icloudz"
CONFIG_FILE = CONFIG_DIR / "config.json"
PID_FILE = CONFIG_DIR / "daemon.pid"
LOG_FILE = CONFIG_DIR / "daemon.log"
STATUS_FILE = CONFIG_DIR / "status.json"

_PAIR_DEFAULTS = {
    "name": "default",
    "local_dir": str(Path.home() / "iCloud"),
    "remote_path": "/",
    "conflict": "newest-wins",   # remote-wins | local-wins | newest-wins
    "excludes": [".DS_Store", "*.tmp", "*.part", "Thumbs.db"],
    "selective": [],             # [] = all; ["/Documents", "/Desktop"] = only these
}

_GLOBAL_DEFAULTS = {
    "apple_id": None,
    "poll_interval": 30,
    "max_workers": 6,
    "notify": True,
    "pairs": [dict(_PAIR_DEFAULTS)],
}


def load() -> dict:
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text())
            return _migrate(data)
        except (json.JSONDecodeError, OSError):
            pass
    return dict(_GLOBAL_DEFAULTS)


def _migrate(data: dict) -> dict:
    """Upgrade flat config (old format) to pairs-based config."""
    cfg = {**_GLOBAL_DEFAULTS, **{k: v for k, v in data.items() if k in _GLOBAL_DEFAULTS}}
    if "pairs" not in data:
        pair = dict(_PAIR_DEFAULTS)
        if "local_dir" in data:
            pair["local_dir"] = data["local_dir"]
        if "remote_path" in data:
            pair["remote_path"] = data["remote_path"]
        cfg["pairs"] = [pair]
    else:
        cfg["pairs"] = [
            {**_PAIR_DEFAULTS, **p} for p in data["pairs"]
        ]
    return cfg


def get_pairs(cfg: dict) -> list[dict]:
    return cfg.get("pairs", [dict(_PAIR_DEFAULTS)])


def save(cfg: dict) -> None:
    for pair in cfg.get("pairs", []):
        local_dir = Path(pair.get("local_dir", "")).expanduser()
        if not local_dir.exists():
            try:
                local_dir.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ValueError(f"Cannot create local_dir {local_dir}: {e}") from e
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))


def write_status(data: dict) -> None:
    try:
        STATUS_FILE.write_text(json.dumps(data, indent=2, default=str))
    except OSError:
        pass


def read_status() -> dict:
    try:
        return json.loads(STATUS_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
