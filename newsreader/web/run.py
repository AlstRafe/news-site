"""Serving the app: what ``python app.py`` (and therefore launcher.py) runs.

Waitress serves by default, also on loopback: it is multi-threaded and production-grade,
and it does not run a second, file-watching copy of the app the way Flask's reloader does.
``NEWS_DEV=1`` switches to Flask's development server with code and template reloading.

Live updates: when started by launcher.py (``NEWS_SUPERVISED=1``), the server watches the
``VERSION`` file a deployment writes last. When it changes, the server stops accepting
connections, lets the requests in flight finish, and exits with RESTART_EXIT_CODE; the
launcher then starts the new code. Started any other way, it never exits on its own.
"""
from __future__ import annotations

import ipaddress
import os
import sys
import threading

from newsreader import paths
from newsreader.web import security

#: Enough threads that a page view never waits behind several image downloads
#: (those are capped separately in services.images).
WAITRESS_THREADS = 16
RESTART_EXIT_CODE = 75
VERSION_POLL_SECONDS = 15.0


def _dev_mode() -> bool:
    from newsreader.web.assets import is_dev
    # A child of Flask's reloader (an older version of this app ran with the reloader on
    # loopback) must keep using the reloader's inherited socket, i.e. stay in dev mode.
    return is_dev() or os.environ.get("WERKZEUG_RUN_MAIN") == "true"


def serve_until_redeployed(app, host: str, port: int, poll_seconds: float = VERSION_POLL_SECONDS,
                           supervised: bool = True) -> int:
    from waitress.server import create_server

    server = create_server(app, host=host, port=port, threads=WAITRESS_THREADS, ident="NewsHtmlReader")
    redeployed = threading.Event()

    def watch() -> None:
        started = paths.deployed_version()
        while not redeployed.wait(poll_seconds):
            current = paths.deployed_version()
            if current and current != started:
                print(f"新しいバージョン {current} を検出しました。処理中の要求を終えてから再起動します。", flush=True)
                redeployed.set()
                # close() must run on the server's own loop thread; the loop then ends once the
                # connections still open have been answered.
                server.trigger.pull_trigger(server.close)
                return

    if supervised:
        threading.Thread(target=watch, name="deploy-watch", daemon=True).start()
    where = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    from newsreader.branding import APP_NAME
    print(f"{APP_NAME}: http://{where}:{port}/ （終了は Ctrl+C）", flush=True)
    try:
        server.run()
    finally:
        server.task_dispatcher.shutdown()
    return RESTART_EXIT_CODE if redeployed.is_set() else 0


def main() -> int:
    try:
        bind_host = security.configured_bind_host()
        port = security.configured_port()
    except RuntimeError as exc:
        print(f"起動できませんでした: {exc}", file=sys.stderr)
        return 2
    from newsreader.web import create_app

    if _dev_mode():
        # Flask's reloader runs the app in a child process; only that child serves requests,
        # so only it starts the background index sync.
        serving_child = os.environ.get("WERKZEUG_RUN_MAIN") == "true"
        app = create_app(start_background=serving_child)
        app.run(host=bind_host, port=port, debug=False, use_reloader=True)
        return 0
    app = create_app()
    _record_web_status(app, bind_host, port)
    if os.environ.get("NEWS_SUPERVISED") == "1":
        _keep_logon_task_current()
    return serve_until_redeployed(app, bind_host, port, supervised=os.environ.get("NEWS_SUPERVISED") == "1")


def _keep_logon_task_current() -> None:
    """Under launcher.py (Windows), register or remove the start-at-logon task as the settings
    say, at each (re)start, so a new version applies it without starting the app by hand.
    Runs in the background; any problem only means the task is left as it was."""
    def work() -> None:
        try:
            import importlib.util
            from pathlib import Path

            from newsreader.services.settings import start_at_logon
            spec = importlib.util.spec_from_file_location("newsreader_launcher", paths.PROJECT_ROOT / "launcher.py")
            launcher = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(launcher)
            launcher.ensure_logon_task(Path(sys.executable), start_at_logon())
        except Exception:  # noqa: BLE001 - informational only, never stops the server
            pass

    threading.Thread(target=work, name="logon-task", daemon=True).start()


def _record_web_status(app, host: str, port: int) -> None:
    """Where this server listens and what it serves, for /status and for whoever maintains the PC."""
    from pathlib import Path

    from newsreader.library import status
    target = app.config.get("STATUS_PATH")
    if not isinstance(target, Path):  # a stand-in app (tests) has nowhere real to write
        return
    where = f"[{host}]" if ipaddress.ip_address(host).version == 6 else host
    try:
        status.update("web", {"url": f"http://{where}:{port}/", "started": status.now_iso(), "pid": os.getpid(),
                              "version": paths.deployed_version(), "library": str(app.config["LIBRARY_ROOT"]),
                              "project": str(paths.PROJECT_ROOT)}, path=target)
    except OSError:
        pass
