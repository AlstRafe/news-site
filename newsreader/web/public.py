"""テスト公開: the whole site under this PC's Tailscale Funnel name (``*.ts.net``), so people
outside the tailnet can try it on the articles this PC collects.

Visitors read and translate, nothing more: pages that change this PC (設定, 取得の状況) or show the
owner's own lists (保存, 単語帳) are not there for them, nothing they send changes settings, lists
or 読み比べ groups, their visits leave no 既読 marks, and every translation they cause counts as
automatic, so it stops at the monthly allowance (translation.AUTO_MONTHLY_CHARS). The owner keeps
using the site at its own addresses with everything.
"""
from __future__ import annotations

from flask import abort, g, has_request_context, jsonify, request

from newsreader.services import translation

FUNNEL_SUFFIX = ".ts.net"
#: What a visitor may send: translations and the read-only comparisons of a 読み比べ group.
VISITOR_POSTS = frozenset({
    "translate_selection", "dictionary_senses", "translate_whole_article", "translate_article_paragraphs",
    "translate_image_captions", "translate_list_titles", "compare_translate", "translate_story_leads",
    "story_facts", "story_quotes", "story_words",
})
#: The owner's pages.
OWNER_PAGES = frozenset({"translation_settings", "status_page", "saved_page", "words_page"})
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def is_funnel_host(host: str) -> bool:
    return host.endswith(FUNNEL_SUFFIX)


def is_visitor() -> bool:
    """Whether this request came in under the public (Funnel) name."""
    if not has_request_context():
        return False
    return is_funnel_host(request.host.rsplit(":", 1)[0].lower().rstrip("."))


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
