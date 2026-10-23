"""Request-level safety: who may connect, which requests may change state, what the browser may run.

Invariants (CLAUDE.md §4), enforced here centrally so no individual view can forget them:
- listen only on loopback / private / Tailscale addresses, and answer only to trusted Host headers;
- every POST carries the session's CSRF token (header ``X-CSRF-Token`` or form field ``csrf``);
- responses carry a Content-Security-Policy that allows only this app's own scripts — inline
  scripts are allowed by hash, never wholesale — plus the usual anti-framing / sniffing headers.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import os
import secrets
from pathlib import Path

from flask import Flask, abort, request, session

from newsreader import paths
from newsreader.netpolicy import is_allowed_bind_address
from newsreader.web.public import is_funnel_host

DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}


def configured_bind_host() -> str:
    """Bind locally by default; remote use must name one private / Tailscale interface address."""
    host = os.environ.get("NEWS_BIND_HOST", DEFAULT_BIND_HOST).strip() or DEFAULT_BIND_HOST
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeError("NEWS_BIND_HOSTにはIPアドレスを指定してください。") from exc
    if not is_allowed_bind_address(address):
        raise RuntimeError("NEWS_BIND_HOSTにはloopbackまたはprivate IPだけを指定できます。")
    return str(address)


def configured_port() -> int:
    raw = os.environ.get("NEWS_PORT", str(DEFAULT_PORT)).strip() or str(DEFAULT_PORT)
    if not raw.isdecimal() or not 1 <= int(raw) <= 65535:
        raise RuntimeError("NEWS_PORTには1から65535の整数を指定してください。")
    return int(raw)


def trusted_hosts(bind_host: str) -> frozenset[str]:
    hosts = {"localhost", "127.0.0.1", "::1", bind_host.lower()}
    hosts.update(item.strip().lower().rstrip(".") for item in os.environ.get("NEWS_ALLOWED_HOSTS", "").split(",")
                 if item.strip())
    return frozenset(hosts)


def host_without_port(raw: str) -> str:
    """``example:5000`` -> ``example``; ``[::1]:5000`` and ``[::1]`` -> ``::1``."""
    raw = raw.strip().lower()
    if raw.startswith("["):
        return raw[1:raw.find("]")] if "]" in raw else raw
    if raw.count(":") == 1:
        raw = raw.split(":", 1)[0]
    return raw.rstrip(".")


def persistent_secret_key(path: Path) -> bytes:
    """Keep the session signing key across restarts so open pages (and their CSRF tokens) keep working."""
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # O_EXCL: two processes starting at once must not overwrite each other's key.
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
    except FileExistsError:
        try:
            existing = path.read_bytes()
            if len(existing) >= 32:
                return existing
        except OSError:
            pass
    except OSError:
        pass  # read-only folder: the key lives for this process only
    return data


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


def _csrf_ok() -> bool:
    expected = session.get("csrf")
    if not expected:
        return False
    supplied = request.headers.get("X-CSRF-Token") or request.form.get("csrf", "")
    return hmac.compare_digest(str(supplied), str(expected))


# ---- Content-Security-Policy --------------------------------------------------

#: Inline scripts the templates are allowed to contain, registered by name. Their
#: sha256 goes into the CSP; any other inline script is blocked by the browser.
INLINE_SCRIPTS: dict[str, str] = {}


def register_inline_script(name: str, source: str) -> str:
    INLINE_SCRIPTS[name] = source
    return source


def _script_hashes() -> str:
    return " ".join(
        "'sha256-" + base64.b64encode(hashlib.sha256(source.encode("utf-8")).digest()).decode("ascii") + "'"
        for source in INLINE_SCRIPTS.values()
    )


def content_security_policy() -> str:
    return "; ".join([
        "default-src 'self'",
        f"script-src 'self' {_script_hashes()}".strip(),
        "style-src 'self'",
        "img-src 'self' data:",  # article images are proxied through /img, never hot-linked
        "connect-src 'self'",
        "font-src 'self'",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
        "form-action 'self'",
    ])


def init_app(app: Flask, *, bind_host: str, secret_key: bytes) -> None:
    app.secret_key = secret_key
    app.config.update(
        SESSION_COOKIE_SAMESITE="Strict",
        SESSION_COOKIE_HTTPONLY=True,
        MAX_CONTENT_LENGTH=64 * 1024,
        TRUSTED_HOSTS_SET=trusted_hosts(bind_host),
        BIND_HOST=bind_host,
    )

    @app.before_request
    def _check_host_and_csrf():
        # Host validation complements binding to loopback or one chosen private address:
        # it blocks DNS-rebinding pages from talking to the app through the browser.
        # The Funnel name (テスト公開, public.py) is let in too; what visitors may do is limited there.
        host = host_without_port(request.host)
        if host not in app.config["TRUSTED_HOSTS_SET"] and not is_funnel_host(host):
            abort(403)
        if request.method not in _SAFE_METHODS:
            # Browsers mark cross-site requests; refuse them before even looking at the token.
            if request.headers.get("Sec-Fetch-Site", "") == "cross-site":
                abort(403)
            # The translation relay is called by other NewsBridge programs with a bearer token
            # (relay_view.py), never by pages; browsers cannot add that header cross-site.
            if request.path.startswith("/relay/") and request.headers.get("Authorization", "").startswith("Bearer "):
                return None
            if not _csrf_ok():
                abort(400)

    @app.after_request
    def _security_headers(response):
        headers = response.headers
        headers.setdefault("Content-Security-Policy", content_security_policy())
        headers.setdefault("X-Content-Type-Options", "nosniff")
        headers.setdefault("X-Frame-Options", "DENY")
        headers.setdefault("Referrer-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        headers.setdefault("Cross-Origin-Resource-Policy", "same-origin")
        headers.setdefault("Permissions-Policy", "camera=(), microphone=(), geolocation=(), payment=(), usb=()")
        return response

    app.jinja_env.globals["csrf_token"] = csrf_token


def default_secret_key_path() -> Path:
    return paths.SECRET_KEY_PATH
