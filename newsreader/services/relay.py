"""Translation relay (D-50): one NewsBridge ("本体") holds the Azure key; other installations —
test builds handed to other people — ask it to translate instead of needing a key of their own.

Server side: the 本体 issues relay tokens. Only a SHA-256 of each token is kept (settings.json
"relay_tokens"); the token itself is shown once. Each token has a daily character allowance.
Client side: settings "translation_source": "relay" with "relay_url" and the token in the
credential store ("relay_token"); translation.py then sends texts here instead of to Azure.

Nothing here logs or returns texts, tokens or provider responses in errors.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import sqlite3
import ssl
import time
import urllib.error
import urllib.request
from contextlib import closing
from datetime import datetime

from newsreader.services import cache
from newsreader.services.settings import _write_merged, get_secret, get_settings

#: Largest request a relay client sends (characters of text); the server refuses more.
MAX_REQUEST_CHARS = 12_000
MAX_REQUEST_TEXTS = 50
DEFAULT_DAILY_CHARS = 200_000
TIMEOUT_SECONDS = 40


class RelayError(Exception):
    """A safe-to-display relay problem."""


# ---- server: tokens ---------------------------------------------------------------


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def tokens() -> list[dict]:
    value = get_settings_raw().get("relay_tokens")
    return (
        [t for t in value if isinstance(t, dict) and isinstance(t.get("hash"), str)] if isinstance(value, list) else []
    )


def get_settings_raw() -> dict:
    from newsreader.services import settings as settings_module

    try:
        value = json.loads(settings_module.SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def issue_token(name: str, daily_chars: int = DEFAULT_DAILY_CHARS) -> str:
    """A new token (returned once, never stored in clear)."""
    token = "nbr_" + secrets.token_urlsafe(32)
    entry = {
        "name": (name or "テスト版")[:60],
        "hash": _digest(token),
        "daily_chars": int(daily_chars),
        "created": datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
    }
    _write_merged({"relay_tokens": tokens() + [entry]})
    return token


def revoke(prefix: str) -> bool:
    """Revoke the one token whose digest starts with ``prefix`` (16 hex characters)."""
    if len(prefix) != 16 or any(c not in "0123456789abcdef" for c in prefix):
        return False
    kept = [t for t in tokens() if not t["hash"].startswith(prefix)]
    if len(kept) == len(tokens()):
        return False
    _write_merged({"relay_tokens": kept})
    return True


def revoke_all() -> int:
    count = len(tokens())
    _write_merged({"relay_tokens": []})
    return count


def check_token(header: str) -> dict | None:
    """The token entry for an ``Authorization: Bearer …`` header, or None."""
    if not header.startswith("Bearer "):
        return None
    supplied = _digest(header[len("Bearer ") :].strip())
    for entry in tokens():
        if hmac.compare_digest(entry["hash"], supplied):
            return entry
    return None


def _usage_db() -> sqlite3.Connection:
    db = sqlite3.connect(cache.CACHE_PATH, timeout=5)
    db.execute(
        "CREATE TABLE IF NOT EXISTS relay_usage (day TEXT NOT NULL, token TEXT NOT NULL, chars INTEGER NOT NULL,"
        " PRIMARY KEY(day, token))"
    )
    return db


def charge(entry: dict, chars: int) -> bool:
    """Count ``chars`` against the token's allowance for today; False when it would go over."""
    day = time.strftime("%Y-%m-%d")
    key = entry["hash"][:16]
    with closing(_usage_db()) as db:
        used = (db.execute("SELECT chars FROM relay_usage WHERE day=? AND token=?", (day, key)).fetchone() or (0,))[0]
        if used + chars > int(entry.get("daily_chars", DEFAULT_DAILY_CHARS)):
            return False
        db.execute(
            "INSERT INTO relay_usage(day, token, chars) VALUES(?,?,?) "
            "ON CONFLICT(day, token) DO UPDATE SET chars = chars + excluded.chars",
            (day, key, chars),
        )
        db.commit()
    return True


def usage_today() -> dict[str, int]:
    day = time.strftime("%Y-%m-%d")
    try:
        with closing(_usage_db()) as db:
            rows = db.execute("SELECT token, chars FROM relay_usage WHERE day=?", (day,)).fetchall()
    except sqlite3.Error:
        return {}
    return dict(rows)


# ---- client ------------------------------------------------------------------------


def _token() -> str:
    """The relay token: from the credential store, else (store unusable) the one shipped in preset/."""
    from newsreader.services.preset import shipped_token
    from newsreader.services.settings import SecretStoreError

    try:
        token = get_secret("relay_token") or ""
    except SecretStoreError:
        token = ""
    return token or shipped_token()


def client_configured() -> bool:
    settings = get_settings()
    return settings.get("translation_source") == "relay" and bool(settings.get("relay_url")) and bool(_token())


def _post(path: str, body: dict) -> dict:
    settings = get_settings()
    base = str(settings.get("relay_url", "")).rstrip("/")
    token = _token()
    if not base or not token:
        raise RelayError("翻訳の中継先が設定されていません。設定画面を確認してください。")
    request = urllib.request.Request(
        base + path,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Authorization": f"Bearer {token}"},
    )
    context = ssl.create_default_context() if base.startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
            payload = json.loads(response.read(4 * 1024 * 1024).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        messages = {
            401: "中継先が合言葉を受け付けませんでした（配布元に確認してください）。",
            429: "中継先の本日の翻訳量の上限に達しました。明日もう一度お試しください。",
            503: "中継先で翻訳できませんでした。時間を置いて再試行してください。",
        }
        raise RelayError(messages.get(exc.code, f"中継先がエラーを返しました（HTTP {exc.code}）。")) from None
    except (urllib.error.URLError, TimeoutError, OSError, ValueError, UnicodeDecodeError):
        raise RelayError(
            "翻訳の中継先に接続できませんでした。配布元の NewsBridge が起動しているか確認してください。"
        ) from None
    if not isinstance(payload, dict):
        raise RelayError("中継先から予期しない応答がありました。")
    return payload


def ping() -> dict:
    """{"daily_chars", "used_today"} from the 本体, or RelayError with a message to show."""
    payload = _post("/relay/v1/ping", {})
    daily, used = payload.get("daily_chars"), payload.get("used_today")
    if payload.get("ok") is not True or not isinstance(daily, int) or not isinstance(used, int):
        raise RelayError("中継先から予期しない応答がありました（NewsBridge の版が古い可能性があります）。")
    return {"daily_chars": daily, "used_today": used}


def translate(texts: list[str], target: str) -> list[str]:
    """One translation per text, through the 本体 (split into requests within its limits)."""
    results: list[str] = []
    batch: list[str] = []
    size = 0
    for text in texts:
        if batch and (len(batch) >= MAX_REQUEST_TEXTS or size + len(text) > MAX_REQUEST_CHARS):
            results.extend(_translate_batch(batch, target))
            batch, size = [], 0
        batch.append(text[:MAX_REQUEST_CHARS])
        size += min(len(text), MAX_REQUEST_CHARS)
    if batch:
        results.extend(_translate_batch(batch, target))
    return results


def _translate_batch(texts: list[str], target: str) -> list[str]:
    from newsreader.services.translation import glossary_for

    body = {"texts": texts, "target": target}
    entries = glossary_for(target)
    if entries:
        body["glossary"] = [list(pair) for pair in entries[:200]]  # this PC's 訳語の指定 (D-63)
    payload = _post("/relay/v1/translate", body)
    out = payload.get("translations")
    if not isinstance(out, list) or len(out) != len(texts) or not all(isinstance(t, str) for t in out):
        raise RelayError("中継先から予期しない応答がありました。")
    lengths = payload.get("sentences")  # sentence lengths, from a 本体 that has them (D-57)
    if isinstance(lengths, list) and len(lengths) == len(texts):
        from newsreader.services import sentences

        for source, translated, found in zip(texts, out, lengths):
            if isinstance(found, dict):
                sentences.remember(target, source, translated, found.get("src"), found.get("tr"))
    return out


def dictionary(text: str, target: str) -> list[dict]:
    payload = _post("/relay/v1/dictionary", {"text": text, "target": target})
    senses = payload.get("senses")
    if not isinstance(senses, list):
        raise RelayError("中継先から予期しない応答がありました。")
    return [s for s in senses if isinstance(s, dict) and isinstance(s.get("terms"), list)]
