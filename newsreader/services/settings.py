"""Local preferences and OS-backed secret storage for translation providers."""
from __future__ import annotations

import json
from pathlib import Path
import ipaddress
import os
import tempfile

import keyring
from keyring.errors import PasswordDeleteError

from newsreader import paths
from newsreader.netpolicy import is_allowed_bind_address

SETTINGS_PATH = paths.SETTINGS_PATH
SERVICE_NAME = "NewsHtmlReader"
#: Credentials this version uses. Keys of removed services (DeepL, NAVER API) stay in the
#: credential store untouched if they were ever saved; nothing reads them any more.
SECRET_NAMES = {"azure_api_key", "stdict_api_key", "relay_token"}
#: Same-news grouping (§10/§11). Kept in the same settings.json; every writer merges, so saving
#: the main form never drops these keys (and saving these never drops the others).
STORY_DEFAULTS = {"stories_enabled": True, "stories_daily_chars": 8_000}
STORY_DAILY_RANGE = (500, 200_000)
DEFAULTS = {
    "provider": "azure",
    "azure_region": "",
    "network_bind_host": "",
    "network_port": "",
}


class SecretStoreError(Exception):
    """The operating system's credential vault could not be accessed."""


def get_settings() -> dict[str, str]:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    provider = value.get("provider") if isinstance(value, dict) else None
    region = value.get("azure_region") if isinstance(value, dict) else None
    bind_host = value.get("network_bind_host") if isinstance(value, dict) else None
    port = value.get("network_port") if isinstance(value, dict) else None
    try:
        bind_host = validate_bind_host(bind_host if isinstance(bind_host, str) else "")
    except ValueError:
        bind_host = ""
    port = str(port).strip() if isinstance(port, (str, int)) else ""
    if port and (not port.isdecimal() or not 1 <= int(port) <= 65535):
        port = ""
    return {
        "provider": "azure",  # the only translation provider (D-44); an old "deepl" choice reads as Azure
        "azure_region": region.strip()[:80] if isinstance(region, str) else DEFAULTS["azure_region"],
        "network_bind_host": bind_host,
        "network_port": port,
        # "own": this PC's Azure key; "relay": ask another NewsBridge to translate (relay.py, D-50).
        "translation_source": "relay" if isinstance(value, dict) and value.get("translation_source") == "relay" else "own",
        "relay_url": relay_url(value.get("relay_url") if isinstance(value, dict) else ""),
    }


def relay_url(value) -> str:
    """An http(s) base URL without a trailing slash, or "" (anything else is ignored)."""
    if not isinstance(value, str):
        return ""
    from urllib.parse import urlsplit
    text = value.strip().rstrip("/")
    parts = urlsplit(text)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.query or parts.fragment or len(text) > 200:
        return ""
    return text


def save_translation_source(source: str, url: str) -> None:
    if source not in ("own", "relay"):
        raise ValueError("翻訳の取得元が不正です。")
    if source == "relay" and not relay_url(url):
        raise ValueError("中継先の URL は http:// または https:// で始まる形で指定してください。")
    _write_merged({"translation_source": source, "relay_url": relay_url(url)})


def validate_bind_host(host: str) -> str:
    host = host.strip()
    if not host:
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("待受けIPにはIPアドレスを入力してください。") from exc
    if address.version != 4 or not is_allowed_bind_address(address):
        raise ValueError("待受けIPにはloopback、private IPv4、またはTailscale IPv4を指定してください。")
    return str(address)


def validate_network_settings(bind_host: str, port: str) -> tuple[str, str]:
    bind_host = validate_bind_host(bind_host)
    port = port.strip()
    if port and (not port.isdecimal() or not 1 <= int(port) <= 65535):
        raise ValueError("ポート番号は1から65535の整数で指定してください。")
    return bind_host, port


def save_settings(azure_region: str = "", network_bind_host: str = "", network_port: str = "") -> None:
    azure_region = azure_region.strip()
    if len(azure_region) > 80:
        raise ValueError("Azureのリージョン名が長すぎます。")
    network_bind_host, network_port = validate_network_settings(network_bind_host, network_port)
    _write_merged({"provider": "azure", "azure_region": azure_region,
                   "network_bind_host": network_bind_host, "network_port": network_port})


def get_story_settings() -> dict:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    value = value if isinstance(value, dict) else {}
    enabled = value.get("stories_enabled", STORY_DEFAULTS["stories_enabled"])
    daily = value.get("stories_daily_chars", STORY_DEFAULTS["stories_daily_chars"])
    low, high = STORY_DAILY_RANGE
    if not isinstance(daily, int) or isinstance(daily, bool) or not low <= daily <= high:
        daily = STORY_DEFAULTS["stories_daily_chars"]
    return {"stories_enabled": enabled if isinstance(enabled, bool) else True, "stories_daily_chars": daily}


def validate_library_dir(text: str) -> str:
    """"" (use the default) or an existing folder given as an absolute path; never a symlink."""
    text = text.strip().strip('"')
    if not text:
        return ""
    path = Path(text).expanduser()
    if not path.is_absolute():
        raise ValueError("記事フォルダは絶対パス（例: C:\\Users\\…\\articles）で指定してください。")
    if path.is_symlink() or not path.is_dir():
        raise ValueError("記事フォルダが見つかりません（シンボリックリンクは使えません）。")
    return str(path)


def save_library_dir(value: str) -> None:
    _write_merged({"library_dir": validate_library_dir(value)})


def start_at_logon() -> bool:
    """launcher.py registers the start-at-logon task unless this is switched off (default on)."""
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return True
    return not (isinstance(value, dict) and value.get("start_at_logon") is False)


def library_limit() -> int:
    """Most articles the collector keeps adding up to (settings "library_limit"); 0 = no limit.
    Test builds ship with a few sample articles and a small limit (D-53)."""
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8")).get("library_limit", 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def save_start_at_logon(enabled: bool) -> None:
    _write_merged({"start_at_logon": bool(enabled)})


def save_story_settings(enabled: bool, daily_chars: int) -> None:
    low, high = STORY_DAILY_RANGE
    if not isinstance(daily_chars, int) or not low <= daily_chars <= high:
        raise ValueError(f"1日の翻訳上限は{low:,}〜{high:,}文字で指定してください。")
    _write_merged({"stories_enabled": bool(enabled), "stories_daily_chars": daily_chars})


def _write_merged(values: dict) -> None:
    """Read-modify-write settings.json atomically, keeping every key this call does not set
    (other forms' settings, keys written by launcher.py, keys from newer versions)."""
    try:
        current = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
        current = current if isinstance(current, dict) else {}
    except (OSError, ValueError, TypeError):
        current = {}
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({**current, **values}, ensure_ascii=False)
    fd, temporary = tempfile.mkstemp(prefix="settings-", suffix=".tmp", dir=SETTINGS_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, SETTINGS_PATH)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def get_secret(name: str) -> str | None:
    if name not in SECRET_NAMES:
        raise ValueError("Unknown secret name")
    try:
        return keyring.get_password(SERVICE_NAME, name)
    except Exception:
        raise SecretStoreError("Windows資格情報ストアを利用できません。") from None


def set_secret(name: str, value: str) -> None:
    if name not in SECRET_NAMES:
        raise ValueError("Unknown secret name")
    try:
        keyring.set_password(SERVICE_NAME, name, value.strip())
    except Exception:
        raise SecretStoreError("Windows資格情報ストアへ保存できません。") from None


def delete_secret(name: str) -> None:
    if name not in SECRET_NAMES:
        raise ValueError("Unknown secret name")
    try:
        keyring.delete_password(SERVICE_NAME, name)
    except PasswordDeleteError:
        pass
    except Exception:
        raise SecretStoreError("Windows資格情報ストアから削除できません。") from None


def configured_secrets() -> dict[str, bool]:
    return {name: bool(get_secret(name)) for name in SECRET_NAMES}
