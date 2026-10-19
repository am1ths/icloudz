from icloudz import watcher


def test_local_watcher_accepts_configured_worker_count(monkeypatch, tmp_path):
    class _Observer:
        def schedule(self, handler, path, recursive):
            assert path == str(tmp_path)
            assert recursive is True

        def start(self):
            pass

    monkeypatch.setattr(watcher, "Observer", _Observer)

    local_watcher = watcher.LocalWatcher(
        object(), tmp_path, "/", max_workers=2
    )
    try:
        assert local_watcher._pool._max_workers == 2
    finally:
        local_watcher._pool.shutdown(wait=True)
