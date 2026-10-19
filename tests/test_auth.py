import stat

import pytest

from icloudz import auth


def test_session_storage_permissions_are_private(monkeypatch, tmp_path):
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    cookie_file = session_dir / "cookies"
    cookie_file.write_text("secret")
    monkeypatch.setattr(auth, "SESSION_DIR", session_dir)

    auth._secure_session_dir()

    assert stat.S_IMODE(session_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(cookie_file.stat().st_mode) == 0o600


def test_two_factor_requires_tty_when_input_is_closed(monkeypatch):
    monkeypatch.setattr("builtins.input", lambda *args: (_ for _ in ()).throw(EOFError()))

    with pytest.raises(RuntimeError, match="interactive terminal"):
        auth._handle_2fa(object())
