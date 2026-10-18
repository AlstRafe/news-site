"""Flask layer: HTTP, templates and the JSON API.

``create_app`` builds a fully configured application. Importing this package has no side
effects: no secret key file, no thread, no database until ``create_app`` is called.
"""
from __future__ import annotations

import json
from pathlib import Path

from flask import Flask

from newsreader import paths


#: Runs in <head> before the stylesheet applies, so a saved light/dark choice never flashes
#: the other theme first. No choice saved = follow the OS (prefers-color-scheme).
THEME_BOOT = ('(function(){try{var t=localStorage.getItem("theme");'
              'if(t==="light"||t==="dark")document.documentElement.setAttribute("data-theme",t)}catch(e){}})();')


def _speculation_rules() -> str:
    # Prefetch article and comparison pages when the pointer rests on a link (moderate),
    # so the click opens a page that is already in the browser's cache (§6).
    rules = {"prefetch": [{"where": {"or": [{"href_matches": "/a/*"}, {"href_matches": "/stories/*"}]},
                           "eagerness": "moderate"}]}
    return json.dumps(rules, separators=(",", ":"))


def create_app(library_root: Path | str | None = None, db_path: Path | str | None = None, *,
               start_background: bool = True, secret_key: bytes | None = None,
               sync_interval: float = 60.0) -> Flask:
    """Build the app.

    library_root: saved-HTML folder (default: ``HTML_DIRECTORY`` or ``articles/``).
    db_path: index database (default: ``instance/library.sqlite3``).
    start_background: run the index sync thread (tests pass False and sync explicitly).
    secret_key: session key; default is the persistent ``instance/secret_key``.
    """
    from newsreader.library import index
    from newsreader.library.files import validate_directory
    from newsreader.web import api, assets, pages, security, settings_view, status_view

    app = Flask("newsreader", template_folder=str(paths.TEMPLATES_DIR), static_folder=None)
    root = Path(library_root).expanduser() if library_root else paths.articles_dir()
    app.config.update(
        LIBRARY_ROOT=root,
        LIBRARY_DB=Path(db_path) if db_path else index.DEFAULT_DB_PATH,
    )
    # status.json lives beside the index: instance/ in production, the test's temp dir in tests.
    app.config["STATUS_PATH"] = app.config["LIBRARY_DB"].parent / "status.json"
    bind_host = security.configured_bind_host()
    security.init_app(app, bind_host=bind_host,
                      secret_key=secret_key or security.persistent_secret_key(paths.SECRET_KEY_PATH))
    assets.init_app(app, dev=assets.is_dev())
    app.jinja_env.globals["speculation_rules"] = security.register_inline_script(
        "speculation_rules", _speculation_rules())
    app.jinja_env.globals["theme_boot"] = security.register_inline_script("theme_boot", THEME_BOOT)

    status_view.init_app(app)
    for module in (pages, api, settings_view, status_view):
        module.routes.register(app)
    pages.register_error_handlers(app)

    if start_background:
        app.extensions["library_sync"] = index.start_background_sync(
            lambda: validate_directory(app.config["LIBRARY_ROOT"]), app.config["LIBRARY_DB"], sync_interval)
    return app
