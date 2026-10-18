"""Local preferences and OS-backed secret storage for translation providers."""
from __future__ import annotations

import json
import ipaddress
import os
import tempfile
from pathlib import Path

import keyring
from keyring.errors import PasswordDeleteError

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "instance" / "settings.json"
SERVICE_NAME = "NewsHtmlReader"
SECRET_NAMES = {
    "deepl_api_key", "azure_api_key", "stdict_api_key",
    "naver_api_key_id", "naver_api_key_secret",
}
DEFAULTS = {
    "provider": "azure",
    "deepl_endpoint": "auto",
    "azure_region": "",
    "network_bind_host": "",
    "network_port": "",
    "naver_search_query": "뉴스",
}


class SecretStoreError(Exception):
    """The operating system's credential vault could not be accessed."""


def get_settings() -> dict[str, str]:
    try:
        value = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        value = {}
    provider = value.get("provider") if isinstance(value, dict) else None
    endpoint = value.get("deepl_endpoint") if isinstance(value, dict) else None
    region = value.get("azure_region") if isinstance(value, dict) else None
    bind_host = value.get("network_bind_host") if isinstance(value, dict) else None
    port = value.get("network_port") if isinstance(value, dict) else None
    naver_query = value.get("naver_search_query") if isinstance(value, dict) else None
    try:
        bind_host = validate_bind_host(bind_host if isinstance(bind_host, str) else "")
    except ValueError:
        bind_host = ""
    port = str(port).strip() if isinstance(port, (str, int)) else ""
    if port and (not port.isdecimal() or not 1 <= int(port) <= 65535):
        port = ""
    return {
        "provider": provider if provider in {"deepl", "azure"} else DEFAULTS["provider"],
        "deepl_endpoint": endpoint if endpoint in {"auto", "free", "paid"} else DEFAULTS["deepl_endpoint"],
        "azure_region": region.strip()[:80] if isinstance(region, str) else DEFAULTS["azure_region"],
        "network_bind_host": bind_host,
        "network_port": port,
        "naver_search_query": naver_query.strip()[:100] if isinstance(naver_query, str) and naver_query.strip() else DEFAULTS["naver_search_query"],
    }


def validate_bind_host(host: str) -> str:
    host = host.strip()
    if not host:
        return ""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ValueError("待受けIPにはIPアドレスを入力してください。") from exc
    tailscale_range = ipaddress.ip_network("100.64.0.0/10")
    if address.version != 4 or not (address.is_loopback or address.is_private or address in tailscale_range):
        raise ValueError("待受けIPにはloopback、private IPv4、またはTailscale IPv4を指定してください。")
    return str(address)


def validate_network_settings(bind_host: str, port: str) -> tuple[str, str]:
    bind_host = validate_bind_host(bind_host)
    port = port.strip()
    if port and (not port.isdecimal() or not 1 <= int(port) <= 65535):
        raise ValueError("ポート番号は1から65535の整数で指定してください。")
    return bind_host, port


def save_settings(
    provider: str,
    deepl_endpoint: str,
    azure_region: str = "",
    network_bind_host: str = "",
    network_port: str = "",
    naver_search_query: str = "뉴스",
) -> None:
    if provider not in {"deepl", "azure"} or deepl_endpoint not in {"auto", "free", "paid"}:
        raise ValueError("設定値が不正です。")
    azure_region = azure_region.strip()
    if len(azure_region) > 80:
        raise ValueError("Azureのリージョン名が長すぎます。")
    network_bind_host, network_port = validate_network_settings(network_bind_host, network_port)
    naver_search_query = naver_search_query.strip()
    if not naver_search_query or len(naver_search_query) > 100:
        raise ValueError("NAVERニュース検索語は1～100文字で指定してください。")
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps({
        "provider": provider,
        "deepl_endpoint": deepl_endpoint,
        "azure_region": azure_region,
        "network_bind_host": network_bind_host,
        "network_port": network_port,
        "naver_search_query": naver_search_query,
    }, ensure_ascii=False)
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
