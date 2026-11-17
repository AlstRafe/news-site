"""Local preferences and OS-backed secret storage for translation providers."""

from __future__ import annotations

import ipaddress
import json
import os
import re
import tempfile
from pathlib import Path

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


def stored() -> dict:
    """settings.json as saved; {} when it is missing, unreadable or not an object."""
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def get_settings() -> dict[str, str]:
    value = stored()
    region = value.get("azure_region")
    bind_host = value.get("network_bind_host")
    port = value.get("network_port")
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
        "translation_source": "relay" if value.get("translation_source") == "relay" else "own",
        "relay_url": relay_url(value.get("relay_url")),
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
    _write_merged(
        {
            "provider": "azure",
            "azure_region": azure_region,
            "network_bind_host": network_bind_host,
            "network_port": network_port,
        }
    )


def get_story_settings() -> dict:
    value = stored()
    enabled = value.get("stories_enabled", STORY_DEFAULTS["stories_enabled"])
    daily = value.get("stories_daily_chars", STORY_DEFAULTS["stories_daily_chars"])
    low, high = STORY_DAILY_RANGE
    if not isinstance(daily, int) or isinstance(daily, bool) or not low <= daily <= high:
        daily = STORY_DEFAULTS["stories_daily_chars"]
    return {"stories_enabled": enabled if isinstance(enabled, bool) else True, "stories_daily_chars": daily}


def validate_library_dir(text: str) -> str:
    """ "" (use the default) or an existing folder given as an absolute path; never a symlink."""
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
    return stored().get("start_at_logon") is not False


def library_limit() -> int:
    """Most articles the collector keeps adding up to (settings "library_limit"); 0 = no limit.
    Test builds ship with a few sample articles and a small limit (D-53)."""
    value = stored().get("library_limit", 0)
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def collects_hourly() -> bool:
    """launcher.py registers the hourly collection task unless "collect_hourly" is false (test builds)."""
    return stored().get("collect_hourly") is not False


#: 話題を追う (D-61): searches kept to show on the home page.
MAX_TOPICS = 10
MAX_TOPIC_CHARS = 60


def topics() -> list[str]:
    value = stored().get("topics", [])
    if not isinstance(value, list):
        return []
    return [t for t in value if isinstance(t, str) and 0 < len(t.strip()) <= MAX_TOPIC_CHARS][:MAX_TOPICS]


def save_topic(topic: str, follow: bool) -> list[str]:
    """Add (``follow``) or remove one topic; the newest added comes first."""
    topic = " ".join(str(topic).split())
    if not topic or len(topic) > MAX_TOPIC_CHARS:
        raise ValueError(f"話題は1〜{MAX_TOPIC_CHARS}文字で指定してください。")
    current = [t for t in topics() if t.casefold() != topic.casefold()]
    if follow:
        if len(current) >= MAX_TOPICS:
            raise ValueError(f"追える話題は{MAX_TOPICS}件までです。どれかを外してください。")
        current.insert(0, topic)
    _write_merged({"topics": current})
    return current


#: 訳語の指定 (D-63): fixed translations of names, applied by the translator (Azure dynamic
#: dictionary). Characters that would break its markup are refused.
MAX_GLOSSARY = 200
MAX_GLOSSARY_CHARS = 40
GLOSSARY_LANGS = ("ja", "en", "ko", "zh-hant")
_GLOSSARY_BAD = re.compile(r'[<>"&\r\n\t]')


def _glossary_entry(source, target, lang) -> dict | None:
    if not (isinstance(source, str) and isinstance(target, str) and lang in GLOSSARY_LANGS):
        return None
    source, target = " ".join(source.split()), " ".join(target.split())
    if not (0 < len(source) <= MAX_GLOSSARY_CHARS and 0 < len(target) <= MAX_GLOSSARY_CHARS):
        return None
    if _GLOSSARY_BAD.search(source) or _GLOSSARY_BAD.search(target):
        return None
    return {"source": source, "target": target, "lang": lang}


def glossary() -> list[dict]:
    value = stored().get("glossary", [])
    entries = (
        [_glossary_entry(e.get("source"), e.get("target"), e.get("lang")) for e in value if isinstance(e, dict)]
        if isinstance(value, list)
        else []
    )
    return [e for e in entries if e][:MAX_GLOSSARY]


def save_glossary_entry(source: str, target: str, lang: str) -> list[dict]:
    entry = _glossary_entry(source, target, lang)
    if entry is None:
        raise ValueError(
            f'原語と訳語は1〜{MAX_GLOSSARY_CHARS}文字（< > " & は使えません）、翻訳先は一覧から選んでください。'
        )
    current = [e for e in glossary() if not (e["source"] == entry["source"] and e["lang"] == entry["lang"])]
    if len(current) >= MAX_GLOSSARY:
        raise ValueError(f"訳語の指定は{MAX_GLOSSARY}件までです。")
    _write_merged({"glossary": [entry] + current})
    return [entry] + current


def remove_glossary_entry(index: int) -> list[dict]:
    current = glossary()
    if 0 <= index < len(current):
        del current[index]
        _write_merged({"glossary": current})
    return current


def build_channel() -> str:
    """ "alpha" / "beta" for a test build made by make_alpha_zip.py, else ""."""
    value = stored().get("build_channel", "")
    return value if value in ("alpha", "beta") else ""


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
    current = stored()
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
