import json
import pytest
from icloudz import config as cfg_mod


def test_load_defaults(tmp_config):
    cfg = cfg_mod.load()
    assert cfg["poll_interval"] == 30
    assert cfg["max_workers"] == 6
    assert cfg["notify"] is True
    pairs = cfg_mod.get_pairs(cfg)
    assert len(pairs) == 1
    assert pairs[0]["conflict"] == "newest-wins"


def test_save_and_reload(tmp_config):
    cfg = cfg_mod.load()
    cfg["poll_interval"] = 60
    cfg_mod.save(cfg)
    reloaded = cfg_mod.load()
    assert reloaded["poll_interval"] == 60


def test_migrate_flat_config(tmp_config):
    old = {
        "apple_id": "test@example.com",
        "local_dir": "/home/user/iCloud",
        "remote_path": "/Documents",
        "poll_interval": 45,
    }
    (tmp_config / "config.json").write_text(json.dumps(old))
    cfg = cfg_mod.load()
    assert cfg["apple_id"] == "test@example.com"
    assert cfg["poll_interval"] == 45
    pairs = cfg_mod.get_pairs(cfg)
    assert pairs[0]["local_dir"] == "/home/user/iCloud"
    assert pairs[0]["remote_path"] == "/Documents"


def test_migrate_pairs_config(tmp_config):
    existing = {
        "apple_id": "a@b.com",
        "pairs": [
            {"name": "work", "local_dir": "/work", "remote_path": "/Work"},
        ],
    }
    (tmp_config / "config.json").write_text(json.dumps(existing))
    cfg = cfg_mod.load()
    pairs = cfg_mod.get_pairs(cfg)
    assert pairs[0]["name"] == "work"
    assert pairs[0]["conflict"] == "newest-wins"  # default filled in


def test_get_pairs_empty(tmp_config):
    cfg = {"apple_id": None}
    pairs = cfg_mod.get_pairs(cfg)
    assert len(pairs) == 1
