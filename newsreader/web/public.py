"""テスト公開: the whole site under its public name, so people outside the tailnet can try it on the
articles this PC collects. The public name is the Tailscale Funnel name (``*.ts.net``), or any name
the host server (Home Page) marks as public in the request (``homepage.visitor`` in the WSGI environ,
which no request can set: the VM's own domain served directly).

Visitors read and translate, nothing more: pages that change this PC (設定, 取得の状況) or show the
owner's own lists (単語帳) are not there for them, nothing they send changes settings, lists
or 読み比べ groups, their visits leave no 既読 marks here (後で読む and 既読 stay in their own browser,
and their 保存 page shows that list), and every translation they cause counts as
automatic, so it stops at the monthly allowance (translation.AUTO_MONTHLY_CHARS). The owner keeps
using the site at its own addresses with everything, and under the public name too on a device
signed in at Home Page's /owner (``homepage.owner`` in the environ, set by Home Page alone).
"""

from __future__ import annotations

from flask import abort, g, has_request_context, jsonify, request

from newsreader.services import translation

FUNNEL_SUFFIX = ".ts.net"
#: The WSGI environ key Home Page sets on a request that came in under one of its public names.
HOST_VISITOR_KEY = "homepage.visitor"
#: The one it sets instead on a public name's request from a device the owner signed in on.
HOST_OWNER_KEY = "homepage.owner"
#: What a visitor may send: translations and the read-only comparisons of a 読み比べ group.
VISITOR_POSTS = frozenset(
    {
        "translate_selection",
        "dictionary_senses",
        "translate_whole_article",
        "translate_article_paragraphs",
        "translate_image_captions",
        "translate_list_titles",
        "compare_translate",
        "translate_story_leads",
        "story_facts",
        "story_quotes",
        "story_words",
    }
)
#: The owner's pages (保存 is there for visitors too, with the list their browser keeps).
OWNER_PAGES = frozenset({"translation_settings", "status_page", "words_page"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_funnel_host(host: str) -> bool:
    return host.endswith(FUNNEL_SUFFIX)


def marked_public(environ) -> bool:
    """Whether the host server said this request came in under a public name."""
    return environ.get(HOST_VISITOR_KEY) is True


def marked_owner(environ) -> bool:
    """Whether the host server said this public name's request is the owner's (signed in there)."""
    return environ.get(HOST_OWNER_KEY) is True


def is_visitor() -> bool:
    """Whether this request came in under a public name (Funnel's, or one the host server marks),
    from anyone but the owner signed in there."""
    if not has_request_context() or marked_owner(request.environ):
        return False
    return marked_public(request.environ) or is_funnel_host(request.host.rsplit(":", 1)[0].lower().rstrip("."))


def init_app(app) -> None:
    @app.before_request
    def _visitor_limits():
        if not is_visitor() or request.path.startswith("/relay/"):  # the relay has its own tokens
            return None
        g.visitor_token = translation.visitor_translations.set(True)
        if request.endpoint in OWNER_PAGES:
            abort(404)
        if request.method not in _SAFE_METHODS and request.endpoint not in VISITOR_POSTS:
            return jsonify({"error": "公開中のページではこの操作はできません。"}), 403
        return None

    @app.teardown_request
    def _visitor_done(_exc=None):
        token = g.pop("visitor_token", None)
        if token is not None:
            translation.visitor_translations.reset(token)

    app.jinja_env.globals["visitor"] = is_visitor
