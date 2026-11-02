"""``instance/status.json``: what the background jobs last did, for the header badge and /status.

Written by the collector process (and later the story pipeline), read by the web app on
every page. Two rules keep that cheap and safe (D-12):
- writers replace the file atomically (temp file + ``os.replace``), with a short retry on
  Windows, where a reader holding the file open makes the replace fail for a moment;
- readers ``stat`` it once per request and re-read only when the mtime changed; a missing
  or corrupt file reads as "nothing known yet" — it never breaks a page.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from newsreader import paths

#: Module-level so tests can point it at an isolated file.
STATUS_PATH = paths.STATUS_PATH
_REPLACE_RETRIES = 20

_cache_lock = threading.Lock()
_cache: dict[str, tuple[int, dict]] = {}


def now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def read(path: Path | None = None) -> dict:
    path = Path(path or STATUS_PATH)
    try:
        mtime = path.stat().st_mtime_ns
    except OSError:
        return {}
    key = str(path)
    cached = _cache.get(key)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        return cached[1] if cached else {}  # half-written or corrupt: keep the last good view
    with _cache_lock:
        _cache[key] = (mtime, data)
    return data


def update(section: str, values: dict, path: Path | None = None) -> dict:
    """Merge ``values`` into ``status[section]`` and write the file atomically. Never raises."""
    path = Path(path or STATUS_PATH)
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(current, dict):
            current = {}
    except (OSError, ValueError):
        current = {}
    merged = {**(current.get(section) or {}), **values}
    current[section] = merged
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".status-", suffix=".tmp", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(current, handle, ensure_ascii=False, indent=1)
            for attempt in range(_REPLACE_RETRIES):
                try:
                    os.replace(temporary, path)
                    break
                except PermissionError:  # Windows: a reader has it open right now
                    if attempt == _REPLACE_RETRIES - 1:
                        raise
                    time.sleep(0.05)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    except OSError:
        pass  # status is informational; a failed write must not fail the job that reports it
    return merged


def minutes_since(iso_utc: str) -> int | None:
    try:
        moment = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return None
    return max(0, int((datetime.now(UTC) - moment).total_seconds() // 60))


def ago(iso_utc: str) -> str:
    """ "5分前", in the page's language (newsreader/i18n.py)."""
    minutes = minutes_since(iso_utc)
    if minutes is None:
        return ""
    if minutes < 1:
        return _say("たった今")
    if minutes < 60:
        return _say("{n}分前", n=minutes)
    if minutes < 48 * 60:
        return _say("{n}時間前", n=minutes // 60)
    return _say("{n}日前", n=minutes // (24 * 60))


def _say(ja: str, **values) -> str:
    from newsreader import i18n

    return i18n.text(ja, **values)


def badge(data: dict | None = None, collects: bool = True) -> dict:
    """The header badge: label, tone (ok / warn / bad / idle) and a tooltip. ``collects`` False:
    this copy registers no hourly collection (a test build, D-55)."""
    data = read() if data is None else data
    collector = data.get("collector") or {}
    finished = collector.get("finished", "")
    if collector.get("running"):
        return {"label": _say("取得中…"), "tone": "ok", "title": _say("自動取得を実行中です")}
    if not finished:
        if not collects:
            return {
                "label": _say("サンプル記事"),
                "tone": "idle",
                "title": _say("テスト版: 記事はサンプルのみで、自動取得はしません"),
            }
        return {"label": _say("未取得"), "tone": "idle", "title": _say("自動取得の記録がまだありません")}
    since = minutes_since(finished)
    if not collector.get("ok", False):
        return {
            "label": _say("取得失敗 · {when}", when=ago(finished)),
            "tone": "bad",
            "title": collector.get("error", ""),
        }
    # The task runs hourly; three missed hours means the PC slept or the task stopped.
    tone = "warn" if since is not None and since > 3 * 60 else "ok"
    sites = (collector.get("sites") or {}).values()
    saved = sum(int(site.get("saved", 0)) for site in sites)
    if sites and all(site.get("limited") for site in sites):  # a test build at its article limit
        return {
            "label": _say("記事数の上限"),
            "tone": "idle",
            "title": _say("テスト版の記事数の上限に達しているため、新しい記事は追加していません"),
        }
    return {
        "label": _say("取得 {when}", when=ago(finished)),
        "tone": tone,
        "title": _say("前回の自動取得で {count} 件保存", count=saved)
        + (_say("（3時間以上更新がありません）") if tone == "warn" else ""),
    }
