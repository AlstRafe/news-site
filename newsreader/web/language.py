"""The page's language on every request (newsreader/i18n.py): which one, the switch, and the texts
the templates and the scripts use.

- ``g.lang``: the ``lang`` cookie, else the browser's languages. Pages differ by it, so HTML
  answers vary by Cookie and Accept-Language (a cached page never shows the language before a switch).
- ``?lang=<code>`` on a page address remembers the choice for the whole host (the cookie's path is
  "/", as the Home Page server sets it) and leads to the same address without it. Hosted, the
  Home Page server answers it before News Site sees it; run on its own, News Site does.
- Templates: ``_("日本語の文", name=value)`` (text) and ``_m(...)`` (where a value is markup),
  ``lang``, ``lang_menu`` and ``texts_js`` (the scripts' translations, ``/i18n/<lang>.js``).
"""
from __future__ import annotations

import hashlib
import json
from urllib.parse import urlencode

from flask import Response, abort, g, redirect, request, url_for

from newsreader import i18n
from newsreader.services.translation import LANG_TAGS
from newsreader.web.assets import YEAR

#: The keys of an API answer that are messages to show (the rest is data: translations, headlines).
MESSAGES = ("error", "translation_error")


def init_app(app) -> None:
    @app.before_request
    def _choose_language():
        asked = request.args.get(i18n.COOKIE) if request.method in ("GET", "HEAD") else None
        if asked in i18n.LANGS and not request.path.startswith(("/api/", "/i18n/", "/static/")):
            rest = urlencode([(key, value) for key, value in request.args.items(multi=True) if key != i18n.COOKIE])
            path = "/" + request.path.lstrip("/")  # "//other.example/" must not lead off the site
            response = redirect(request.script_root + path + (f"?{rest}" if rest else ""))
            response.set_cookie(i18n.COOKIE, asked, max_age=i18n.COOKIE_SECONDS, path="/", samesite="Lax",
                                secure=request.is_secure)
            return response
        g.lang = i18n.chosen(request.cookies.get(i18n.COOKIE, ""), request.headers.get("Accept-Language", ""))
        return None

    @app.after_request
    def _in_the_language(response):
        if response.mimetype in ("text/html", "application/manifest+json"):
            response.vary.update(("Cookie", "Accept-Language"))
        elif response.is_json and g.get("lang", i18n.DEFAULT) != i18n.DEFAULT and not response.direct_passthrough:
            # An API's message for the page ({"error": "…"}, a translation's error): said in the page's language
            payload = response.get_json(silent=True)
            if isinstance(payload, dict) and any(isinstance(payload.get(key), str) for key in MESSAGES):
                for key in MESSAGES:
                    if isinstance(payload.get(key), str):
                        payload[key] = i18n.text(payload[key])
                response.set_data(json.dumps(payload, ensure_ascii=False))
        return response

    scripts: dict[str, tuple[bytes, str]] = {}

    def script_texts(lang: str) -> tuple[bytes, str]:
        if lang not in scripts:
            body = ("/* NewsBridge: the scripts' texts in " + lang + " (newsreader/locale/" + lang + ".json) */\n"
                    "window.NR_TEXTS=" + json.dumps(i18n.table(lang)["scripts"], ensure_ascii=False, sort_keys=True)
                    + ";\n").encode("utf-8")
            scripts[lang] = (body, hashlib.sha256(body).hexdigest()[:12])
        return scripts[lang]

    def texts_js(lang: str):
        if lang not in i18n.LANGS or lang == i18n.DEFAULT:
            abort(404)
        body, digest = script_texts(lang)
        response = Response(body, mimetype="application/javascript")
        response.headers["Content-Type"] = "application/javascript; charset=utf-8"
        response.set_etag(digest, weak=True)
        response.headers["Cache-Control"] = (f"public, max-age={YEAR}, immutable" if request.args.get("v") == digest
                                             else "no-cache")
        return response.make_conditional(request)

    app.add_url_rule("/i18n/<lang>.js", endpoint="texts_js", view_func=texts_js)

    def _texts_js_url() -> str:
        lang = g.get("lang", i18n.DEFAULT)
        return "" if lang == i18n.DEFAULT else url_for("texts_js", lang=lang, v=script_texts(lang)[1])

    app.jinja_env.globals.update(_=i18n.text, _m=i18n.markup)

    @app.context_processor
    def _language():
        lang = g.get("lang", i18n.DEFAULT)
        return {"lang": lang, "locale": i18n.LOCALES[lang], "lang_menu": i18n.menu(lang),
                "texts_js": _texts_js_url(), "page_target": i18n.TARGETS[lang],
                # the language of the headline translations printed with the page (they follow the page's)
                "gloss_tag": LANG_TAGS[i18n.TARGETS[lang]]}
