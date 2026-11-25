"""Flask layer: HTTP, templates and the JSON API.

``create_app`` builds a fully configured application. Importing this package has no side
effects: no secret key file, no thread, no database until ``create_app`` is called.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from flask import Flask
from markupsafe import Markup

from newsreader import i18n, paths
from newsreader.phrases import mark

#: Runs in <head> before the stylesheet applies, so a saved light/dark choice never flashes
#: the other theme first. No choice saved = follow the OS (prefers-color-scheme).
THEME_BOOT = (
    '(function(){try{var t=localStorage.getItem("theme");'
    'if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t);'
    # Translated headlines are printed with the page; someone who turned them off never sees them flash.
    'if(localStorage.getItem("news-html-reader-list-gloss")==="0")document.documentElement.classList.add("gloss-off");'
    'var z=localStorage.getItem("news-html-reader-text-size");if(z==="s"||z==="l")document.documentElement.setAttribute("data-text-size",z)'
    "}catch(e){}})();"
)


def wbr(text) -> Markup:
    """Japanese and Chinese text with ``<wbr>`` between its phrases (newsreader/phrases), so that every browser breaks
    a line between words, never inside one (product.css sets ``word-break: keep-all`` on these texts)."""
    return Markup(mark(str(text), i18n.current())) if text else Markup("")


def phrases(text: str) -> Markup:
    """A big headline with each phrase (up to its 、 。 , . and the like) in its own span, so the phrases can come
    in one after another (product.css), and inside a span ``<wbr>`` between its words (``wbr``)."""
    parts = re.findall(r"(.+?(?:[。．.,、，!！?？]|$))(\s*)", text.strip())
    return (
        Markup("")
        .join(Markup('<span class="w">%s</span>%s') % (wbr(word), " " if gap else "") for word, gap in parts if word)
        .rstrip()
    )


def _speculation_rules(mount: str = "", eagerness: str = "moderate") -> str:
    # Prefetch article and comparison pages when the pointer rests on a link (moderate),
    # so the click opens a page that is already in the browser's cache (§6). Visitors of the
    # public site (web/public.py) come through Tailscale's relays, where every page costs a
    # long round trip: their browser fetches the linked pages ahead ("eager"; opening an
    # article leaves no 既読 mark for them, so fetching one unread is harmless).
    rules = {
        "prefetch": [
            {
                "where": {"or": [{"href_matches": f"{mount}/a/*"}, {"href_matches": f"{mount}/stories/*"}]},
                "eagerness": eagerness,
            }
        ]
    }
    return json.dumps(rules, separators=(",", ":"))


def create_app(
    library_root: Path | str | None = None,
    db_path: Path | str | None = None,
    *,
    start_background: bool = True,
    secret_key: bytes | None = None,
    sync_interval: float = 60.0,
    mount: str = "",
) -> Flask:
    """Build the app.

    library_root: saved-HTML folder (default: ``HTML_DIRECTORY`` or ``articles/``).
    db_path: index database (default: ``instance/library.sqlite3``).
    start_background: run the index sync thread (tests pass False and sync explicitly).
    secret_key: session key; default is the persistent ``instance/secret_key``.
    mount: the path the app is served under (``/news`` behind the portal; links come from url_for).
    """
    from newsreader.library import index
    from newsreader.library.files import validate_directory
    from newsreader.web import (
        api,
        assets,
        compare_view,
        language,
        pages,
        public,
        reading_view,
        relay_view,
        security,
        settings_view,
        status_view,
        stories_view,
    )

    app = Flask("newsreader", template_folder=str(paths.TEMPLATES_DIR), static_folder=None)
    root = Path(library_root).expanduser() if library_root else paths.articles_dir()
    app.config.update(
        LIBRARY_ROOT=root,
        LIBRARY_DB=Path(db_path) if db_path else index.DEFAULT_DB_PATH,
    )
    # status.json lives beside the index: instance/ in production, the test's temp dir in tests.
    app.config["STATUS_PATH"] = app.config["LIBRARY_DB"].parent / "status.json"
    bind_host = security.configured_bind_host()
    security.init_app(
        app, bind_host=bind_host, secret_key=secret_key or security.persistent_secret_key(paths.SECRET_KEY_PATH)
    )
    public.init_app(app)
    assets.init_app(app, dev=assets.is_dev())
    language.init_app(app)  # after assets: its answers are put in the page's language before they are gzipped
    app.jinja_env.globals["speculation_rules"] = security.register_inline_script(
        "speculation_rules", _speculation_rules(mount)
    )
    app.jinja_env.globals["speculation_rules_visitor"] = security.register_inline_script(
        "speculation_rules_visitor", _speculation_rules(mount, "eager")
    )
    app.jinja_env.globals["theme_boot"] = security.register_inline_script("theme_boot", THEME_BOOT)

    status_view.init_app(app)
    from newsreader import branding
    from newsreader.services import translation
    from newsreader.services.settings import build_channel, get_settings

    # The version this process serves (a deployment restarts it): pages carry it, /api/version
    # answers it, and an open page offers a reload when the two differ.
    app.config.setdefault("APP_VERSION", paths.deployed_version())
    app.jinja_env.globals.update(
        app_name=branding.APP_NAME,
        app_name_ja=branding.APP_NAME_JA,
        app_tagline=branding.APP_TAGLINE,
        outlet_intro=branding.OUTLET_INTRO,
        auto_limit=translation.AUTO_MONTHLY_CHARS,
        app_version=app.config["APP_VERSION"],
        build_channel=build_channel(),
    )
    app.add_template_filter(branding.source_language, "src_lang")
    app.add_template_filter(branding.lang_attr, "lang_attr")
    # Texts kept in Japanese by the library (labels, outlet names, figures), in the page's language:
    # an outlet's name stays the key (filters, links, data-lang); only what is shown changes.
    from markupsafe import escape

    from newsreader.library import compare

    app.add_template_filter(lambda name: i18n.text(name) if name else name, "outlet")
    app.add_template_filter(lambda ja: i18n.text(ja) if ja else ja, "tr")
    app.add_template_filter(lambda text: compare.say_fact(text, i18n.current()), "fact")
    app.add_template_filter(lambda value: Markup("<strong>%s</strong>") % escape(value), "strong")
    app.add_template_filter(phrases, "phrases")
    app.add_template_filter(wbr, "wbr")
    from newsreader.library.provenance import dateline_label

    app.add_template_filter(dateline_label, "dateline_label")

    @app.context_processor
    def _translation_targets():
        # One list of translation targets for every page and script (base.html meta tag).
        menu = translation.target_menu(get_settings()["provider"])
        return {"translation_targets": menu, "translation_targets_json": json.dumps(menu, ensure_ascii=False)}

    for module in (pages, api, settings_view, status_view, stories_view, relay_view, compare_view, reading_view):
        module.routes.register(app)
    pages.register_error_handlers(app)

    if start_background:
        from newsreader.library import pipeline

        app.extensions["library_sync"] = index.start_background_sync(
            lambda: validate_directory(app.config["LIBRARY_ROOT"]),
            app.config["LIBRARY_DB"],
            sync_interval,
            after_change=lambda result: pipeline.after_sync(
                result, app.config["LIBRARY_DB"], app.config["STATUS_PATH"]
            ),
        )
        pipeline.keep_warm(app.config["LIBRARY_DB"], sync_interval)
    return app
