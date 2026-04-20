import logging
import os
import keyring
import getpass
from pathlib import Path
from pyicloud import PyiCloudService
from pyicloud.exceptions import PyiCloudFailedLoginException

log = logging.getLogger(__name__)

KEYRING_SERVICE = "icloudz"
SESSION_DIR = Path.home() / ".config" / "icloudz" / "session"
ENV_FILE = Path.home() / ".config" / "icloudz" / ".env"

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/16.6 Safari/605.1.15"
)


def _patch_pyicloud_ua() -> None:
    """Inject a browser User-Agent before pyicloud's authenticate() runs."""
    try:
        from pyicloud import base as pb
        _orig = pb.PyiCloudSession.__init__
        def _patched(self, *a, **kw):
            _orig(self, *a, **kw)
            self.headers["User-Agent"] = _BROWSER_UA
        pb.PyiCloudSession.__init__ = _patched
    except Exception:
        pass


_patch_pyicloud_ua()


def get_api(apple_id: str | None = None) -> PyiCloudService:
    _load_env()
    if apple_id is None:
        apple_id = os.environ.get("ICLOUDZ_APPLE_ID") or _load_apple_id()
    if apple_id is None:
        raise RuntimeError("Apple ID not set. Run: icloudz login")

    password = _get_password(apple_id)
    if password is None:
        raise RuntimeError(f"No password found for {apple_id}. Run: icloudz login")

    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    try:
        api = PyiCloudService(apple_id, password, cookie_directory=str(SESSION_DIR))
    except PyiCloudFailedLoginException as e:
        raise RuntimeError(f"Login failed: {e}") from e

    if api.requires_2fa:
        _handle_2fa(api)
    elif api.requires_2sa:
        _handle_2sa(api)

    return api


def login(apple_id: str) -> PyiCloudService:
    password = getpass.getpass(f"Password for {apple_id}: ").strip()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)

    try:
        api = PyiCloudService(apple_id, password, cookie_directory=str(SESSION_DIR))
    except PyiCloudFailedLoginException as e:
        detail = str(e)
        cause = e.__cause__
        if cause and hasattr(cause, "response") and cause.response is not None:
            detail += f"\nApple response ({cause.response.status_code}): {cause.response.text[:500]}"
        raise RuntimeError(f"Login failed: {detail}") from e

    if api.requires_2fa:
        _handle_2fa(api)
    elif api.requires_2sa:
        _handle_2sa(api)

    keyring.set_password(KEYRING_SERVICE, apple_id, password)
    _save_apple_id(apple_id)
    return api


def refresh_api(api: PyiCloudService, apple_id: str) -> PyiCloudService:
    """Re-authenticate if session has expired. Returns same or new API instance."""
    try:
        if not api.is_trusted_session:
            raise PyiCloudAPIResponseException("session not trusted", None)
        return api
    except Exception:
        pass

    log.warning("iCloud session expired, re-authenticating...")
    try:
        return get_api(apple_id)
    except RuntimeError as e:
        raise RuntimeError(f"Re-authentication failed: {e}") from e


def _get_password(apple_id: str) -> str | None:
    # 1. .env file
    if "ICLOUDZ_PASSWORD" in os.environ:
        return os.environ["ICLOUDZ_PASSWORD"]
    # 2. keyring
    return keyring.get_password(KEYRING_SERVICE, apple_id)


def _load_env() -> None:
    """Load ~/.config/icloudz/.env into os.environ if it exists."""
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _handle_2fa(api: PyiCloudService) -> None:
    print("Two-factor authentication required.")
    if hasattr(api, "request_2fa_code"):
        api.request_2fa_code()
        print("A verification code was sent to your trusted device.")
    code = input("Enter the 6-digit code: ").strip()
    result = api.validate_2fa_code(code)
    if not result:
        raise RuntimeError("Invalid 2FA code")
    if not api.is_trusted_session:
        api.trust_session()


def _handle_2sa(api: PyiCloudService) -> None:
    devices = api.trusted_devices
    for i, dev in enumerate(devices):
        name = dev.get("deviceName") or dev.get("phoneNumber", f"Device {i}")
        print(f"  [{i}] {name}")
    idx = int(input("Select device for verification code: ").strip())
    device = devices[idx]
    if not api.send_verification_code(device):
        raise RuntimeError("Failed to send verification code")
    code = input("Enter verification code: ").strip()
    if not api.validate_verification_code(device, code):
        raise RuntimeError("Invalid verification code")


def _apple_id_file() -> Path:
    return Path.home() / ".config" / "icloudz" / "apple_id"


def _save_apple_id(apple_id: str) -> None:
    _apple_id_file().parent.mkdir(parents=True, exist_ok=True)
    _apple_id_file().write_text(apple_id)


def _load_apple_id() -> str | None:
    p = _apple_id_file()
    return p.read_text().strip() if p.exists() else None
