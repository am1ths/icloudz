from icloudz.cli import _scope_manual_state


def test_manual_state_scope_is_separate_for_path_override(tmp_path):
    pair = {
        "name": "docs",
        "local_dir": str(tmp_path / "configured"),
        "remote_path": "/Documents",
    }

    _scope_manual_state(pair, tmp_path / "override", "/Archive")

    assert pair["_state_key"].startswith("docs:")


def test_manual_state_scope_is_shared_for_identical_pair_paths(tmp_path):
    local = tmp_path / "docs"
    local.mkdir()
    pair = {
        "name": "docs",
        "local_dir": str(local),
        "remote_path": "/Documents",
    }

    _scope_manual_state(pair, local, "/Documents")

    assert "_state_key" not in pair
