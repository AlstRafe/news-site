"""News Site as one site of another server: on this PC, the Home Page project's server.

That server (``~/Documents/Claude/Home Page``, homepage/server.py) owns the address: it listens on
port 5000, lists the sites on "/", and calls ``site`` once at its start through its sites.toml
(``python = "../News Site/work/app"``, ``entry = "newsreader.hosting:site"``). It restarts into
new News Site code when ``version`` changes (a deployment writes VERSION) and reinstalls the
packages when requirements.txt changed.

Hosted, News Site does not start a server of its own: its start at logon is turned off and
``instance/hosted.json`` tells launcher.py where the site is served instead. Run on its own
(another PC, a test build), app.py serves it as before (newsreader.portal).
"""

from __future__ import annotations

import json
import os
from urllib.parse import quote

from newsreader import branding, i18n, paths

#: Under the mount, for crawlers to leave alone.
DISALLOW = ("/api/",)
#: Old addresses answered as they are (test builds POST to the relay at a fixed URL).
LEGACY_DIRECT = ("/relay/",)
#: Written here while another server hosts News Site (read by launcher.py).
HOSTED_PATH = paths.INSTANCE_DIR / "hosted.json"
#: Headlines the host's front page shows.
PREVIEW = 3


def site(mount: str = "/news", host: dict | None = None) -> dict:
    """News Site for a host server, served under ``mount``. ``host``: ``{"port": int, "addresses": [...],
    "names": [...], "urls": [...]}``, where and under which names the host answers.

    Returns the WSGI ``app`` and what the host's front page shows: ``name``, ``subtitle``,
    ``preview(lang)`` (the stories several outlets carried, and how fresh the collection is, in the
    visitor's language: newsreader/i18n.py),
    ``disallow`` (for robots.txt), ``legacy`` (old addresses: the host redirects unknown ones under the
    mount and passes these prefixes as they are) and ``version()`` (the deployed version)."""
    from newsreader.services.preset import import_relay_token
    from newsreader.web import create_app
    from newsreader.web.run import _record_web_status

    host = host or {}
    names = [str(item) for item in host.get("names", [])]
    # News Site answers only to the names it trusts: the host's addresses and names too.
    os.environ["NEWS_ALLOWED_HOSTS"] = ",".join(filter(None, [os.environ.get("NEWS_ALLOWED_HOSTS", ""), *names]))
    import_relay_token()
    app = create_app(mount=mount)
    addresses = [str(item) for item in host.get("addresses", [])] or ["127.0.0.1"]
    _record_web_status(app, addresses[0], int(host.get("port", 5000)), tuple(addresses[1:]))
    _hand_over_the_start(host, mount)
    return {
        "app": app,
        "name": branding.APP_NAME,
        "subtitle": branding.APP_TAGLINE,
        "preview": lambda lang=i18n.DEFAULT: news_now(app, lang),
        "disallow": DISALLOW,
        "legacy": LEGACY_DIRECT,
        "version": paths.deployed_version,
    }


def hosted() -> dict:
    """Where another server serves News Site (``{"url": ..., "port": ...}``), or {}."""
    try:
        value = json.loads(HOSTED_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _hand_over_the_start(host: dict, mount: str) -> None:
    """The host is the server started at logon now: News Site's own start would race it for the port."""
    from newsreader.services.settings import save_start_at_logon

    urls = [str(item) for item in host.get("urls", [])] or ["http://127.0.0.1:5000/"]
    try:
        HOSTED_PATH.parent.mkdir(parents=True, exist_ok=True)
        HOSTED_PATH.write_text(
            json.dumps(
                {"by": "Home Page", "url": urls[0].rstrip("/") + mount + "/", "port": int(host.get("port", 5000))},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        save_start_at_logon(False)
    except OSError:
        pass
    _remove_logon_task()


def _remove_logon_task() -> None:
    """launcher.py's start-at-logon task, if it registered one (Windows)."""
    if os.name != "nt":
        return
    from newsreader.web.run import set_logon_task

    try:
        set_logon_task(False)
    except Exception as exc:  # the site is served either way
        print(f"News Site のログオン時の自動起動を外せませんでした: {exc!r}", flush=True)


def news_now(news_app, lang: str = i18n.DEFAULT) -> dict:
    """The latest stories several outlets carried (or the newest articles), in the visitor's language
    where the headline was translated already (or is in it), and when the collector last ran; paths
    are under the site."""
    from flask import g

    from newsreader.library import index, status
    from newsreader.library.stories import LANG_TARGETS
    from newsreader.services import translation
    from newsreader.services.settings import collects_hourly
    from newsreader.web.pages import front_stories
    from newsreader.web.status_view import status_data

    db = news_app.config["LIBRARY_DB"]
    target = i18n.TARGETS.get(lang, "ja")
    with news_app.test_request_context("/"):  # front_stories builds picture URLs
        g.lang = lang if lang in i18n.LANGS else i18n.DEFAULT
        # More than are shown: headlines the visitor can read go first (an hour's new Korean or English
        # headline waits until someone has opened it in their language here).
        stories = front_stories(db)
        if stories:
            rows = [(s["lead_title"], f"/stories/{quote(s['key'])}", s.get("lead_source") or "") for s in stories]
        else:
            rows = [
                (a["title"], f"/a/{a['id']}", a.get("source") or "")
                for a in index.list_articles(db, page=1, per_page=PREVIEW * 3)["items"]
            ]
        kept = translation.cached_titles([title for title, _, _ in rows], target)
        badge = status.badge(status_data(), collects=collects_hourly())
        title = i18n.text("各社が伝えた話題" if stories else "新着記事")
    readable = lambda row, said: bool(said) or LANG_TARGETS.get(branding.source_language(row[2]), "") == target
    shown = sorted(zip(rows, kept), key=lambda pair: not readable(*pair))[:PREVIEW]  # stable: newest first within each
    return {
        "title": title,
        "note": badge["label"] if badge.get("tone") != "idle" else "",
        "items": [(said or headline, path) for (headline, path, _), said in shown],
    }
