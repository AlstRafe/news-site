"""Serving the app: what ``python app.py`` (and therefore launcher.py) runs.

Waitress serves by default, also on loopback: it is multi-threaded and production-grade,
and it does not run a second, file-watching copy of the app the way Flask's reloader does.
``NEWS_DEV=1`` switches to Flask's development server with code and template reloading.
"""
from __future__ import annotations

import ipaddress
import os
import sys

from newsreader.web import security

#: Enough threads that a page view never waits behind several image downloads
#: (those are capped separately in services.images).
WAITRESS_THREADS = 16


def main() -> int:
    try:
        bind_host = security.configured_bind_host()
        port = security.configured_port()
    except RuntimeError as exc:
        print(f"起動できませんでした: {exc}", file=sys.stderr)
        return 2
    from newsreader.web import create_app
    from newsreader.web.assets import is_dev

    if is_dev():
        # Flask's reloader runs the app in a child process; only that child serves requests,
        # so only it starts the background index sync.
        serving_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
        app = create_app(start_background=serving_child)
        app.run(host=bind_host, port=port, debug=False, use_reloader=True)
        return 0
    from waitress import serve

    app = create_app()
    where = f"[{bind_host}]" if ipaddress.ip_address(bind_host).version == 6 else bind_host
    print(f"News HTML Reader: http://{where}:{port}/ （終了は Ctrl+C）", flush=True)
    serve(app, host=bind_host, port=port, threads=WAITRESS_THREADS, ident="NewsHtmlReader")
    return 0
