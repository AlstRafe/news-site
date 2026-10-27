"""HTML pages: the article list, an article, image delivery and the legacy URLs.

Nothing here parses saved HTML or touches the network while rendering a page: the list
and the article come from the index; images are fetched lazily by ``/img`` (D-04, D-05).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import abort, current_app, jsonify, redirect, render_template, request, send_file, url_for

from newsreader import i18n, paths
from newsreader.library import index
from newsreader.library.files import resolve_html_file, validate_directory
from newsreader.services import images as image_service
from newsreader.sources import IMAGE_EXTENSIONS, ReaderError, extract_article
from newsreader.web.assets import CACHE_LIST, CACHE_PAGE, YEAR
from newsreader.web.routing import Routes
from newsreader.web import public
from newsreader.web.security import csrf_token

PER_PAGE = 24
_KEY_CHARS = 12  # of the image hash, carried in /img URLs so a changed image gets a new URL

routes = Routes()


# ---- helpers shared with the API ---------------------------------------------

def db_path() -> Path:
    return current_app.config["LIBRARY_DB"]


def library_root() -> Path:
    return validate_directory(current_app.config["LIBRARY_ROOT"] or paths.articles_dir())


def article_file(record: dict) -> Path:
    """Resolve an indexed article to its HTML file, refusing anything outside the library."""
    root = library_root()
    source = record["source"]
    if source and (Path(source).name != source or source in {".", ".."}):
        raise ReaderError("記事の場所が不正です。")
    directory = root / source if source else root
    if directory.is_symlink():
        raise ReaderError("記事の場所が不正です。")
    return resolve_html_file(validate_directory(directory), record["filename"])


def load_article(record: dict) -> dict:
    """The article body from the index; parses the file only if the index has no body yet.

    The fallback exists for the first minutes after upgrading from v3 (rows without a body
    until the background sync reaches them); it also nudges that sync to run now.
    """
    article = index.get_content(db_path(), record["id"])
    if article is not None:
        return article
    if record.get("error"):
        raise ReaderError(record["error"])
    worker = current_app.extensions.get("library_sync")
    if worker is not None:
        worker.trigger()
    return extract_article(article_file(record).read_bytes())


def image_url(article_id: int, n: int, source: str) -> str:
    return url_for("image", article_id=article_id, n=n, k=image_service.image_key(source)[:_KEY_CHARS])


def image_entries(article_id: int, article: dict) -> list[dict]:
    """Images the page may show: only those on the allowed hosts (as in v3), each via /img."""
    return [{**image, "url": image_url(article_id, n, image["src"])}
            for n, image in enumerate(article["images"][:image_service.MAX_IMAGE_COUNT])
            if image_service.allowed_image_url(image["src"])]


def _error_page(message: str, status: int):
    heading = _HEADINGS.get(status, _HEADINGS[400 if status < 500 else 500])
    if status >= 500 and public.is_visitor():  # the owner's log is not theirs to read
        message = "時間をおいて、もう一度開いてみてください。"
    # In the page's language first: a message that starts with the heading says it once, as the heading
    message, heading = i18n.text(message), i18n.text(heading)
    if message.startswith(heading):
        message = message[len(heading):].lstrip("。. ")
    return render_template("error.html", message=message, status=status, heading=heading), status


# ---- pages -------------------------------------------------------------------

def query_in_english(query: str) -> str:
    """A non-English search, translated to English (cached; sent once per distinct search) so it
    also matches the English headlines kept for every article. "" without a key or on failure."""
    from newsreader.services import translation
    from newsreader.services.settings import SecretStoreError
    try:
        if not translation.provider_ready():
            return ""
        return translation.translate_titles_to_english([query])[0].strip()
    except (translation.TranslationError, SecretStoreError):
        return ""


#: 話題を追う: how many articles per followed topic the home page looks at, and how many it lists.
TOPIC_SCAN = 30
TOPIC_SHOWN = 3


def topic_digest(db: Path, topic: str) -> dict:
    """The newest articles matching a followed topic in any language (the same search as the
    list: headlines and openings, and the English headlines of every article)."""
    english = query_in_english(topic) if re.search(r"[^\x00-\x7f]", topic) else ""
    result = index.list_articles(db, query=topic, page=1, per_page=TOPIC_SCAN,
                                 pivot_words=index.search_words(english or topic))
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    recent = sum(1 for item in result["items"] if (item.get("published") or "") >= since)
    return {"q": topic, "total": result["total"], "recent": recent, "more": recent >= TOPIC_SCAN,
            "articles": result["items"][:TOPIC_SHOWN]}


#: Notices that are not news (appointments, obituaries, notices, book listings) never lead the page.
ROUTINE_TITLE = re.compile(r"^\s*[\[［【〔](?:인사|부고|동정|알림|게시판|신간|표|사진|포토|그래픽|訃告|人事)[\]］】〕]")
FRONT_STORIES = 5


def front_stories(db: Path) -> list[dict]:
    """The page's top: the newest stories several outlets carried, routine notices left out; the
    first is the one the most outlets carry (newest among equals) and has a picture when any
    of its articles has one."""
    from newsreader.library import stories
    found = [s for s in stories.list_stories(db, per_page=FRONT_STORIES * 2)["stories"]
             if not ROUTINE_TITLE.match(s["lead_title"] or "")][:FRONT_STORIES]
    if not found:
        return []
    lead = max(found, key=lambda s: (s["outlets"], s["count"], s["newest"] or ""))
    found.remove(lead)
    found.insert(0, lead)
    lead["thumb"] = ""
    for member in sorted(lead["members"], key=lambda m: m.get("id") != lead["lead_id"]):
        if image_service.allowed_image_url(member.get("thumb_src") or ""):
            lead["thumb"] = image_url(member["id"], 0, member["thumb_src"])
            break
    return found


@routes.get("/", "index")
def index_page():
    source = request.args.get("source", "")[:100]
    from newsreader.library import genres
    genre = request.args.get("genre", "")
    genre = genre if genre in genres.LABELS or genre == genres.OTHER else ""
    query = request.args.get("q", "")
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    db = db_path()
    query = query.strip()[:100]
    english = query_in_english(query) if re.search(r"[^\x00-\x7f]", query) else ""
    pivot_words = index.search_words(english or query)
    result = index.list_articles(db, source=source, query=query, page=page, per_page=PER_PAGE,
                                 pivot_words=pivot_words, genre=genre)
    from newsreader.library import stories
    marks = stories.badges(db, [item["id"] for item in result["items"]])
    for item in result["items"]:
        thumb = item.get("thumb_src", "")
        item["thumb"] = image_url(item["id"], 0, thumb) if image_service.allowed_image_url(thumb) else ""
        item["story"] = marks.get(item["id"])
    # The newest stories several outlets reported, on the first page of the plain list only;
    # the one most outlets carry leads the page with its picture.
    narrowed = bool(source or query or genre or result["page"] > 1)
    highlights = [] if narrowed else front_stories(db)
    # Headlines already translated into the page's language are printed with the page (the rest are
    # fetched after load).
    from newsreader.services import translation
    kept = translation.cached_titles([item["title"] for item in result["items"]]
                                     + [story["lead_title"] for story in highlights], page_target())
    for item, ja in zip(result["items"] + highlights, kept):
        item["ja"] = ja
    # 既読・保存 (the same on the PC and the phone)
    from newsreader.library import reading
    marks = {} if public.is_visitor() else reading.states(reading.path_for(db), result["items"])
    for item in result["items"]:
        item["mark"] = marks.get(item["id"], {})
    # ...and those whose outlets disagree on a count (D-59), computed in the background.
    from newsreader.library import facts_watch
    disputes = [] if narrowed else facts_watch.differing(db, limit=3)
    from newsreader.services.settings import topics as followed_topics
    followed = followed_topics()
    topic_cards = [] if narrowed else [topic_digest(db, t) for t in followed[:6]]
    state = index.sync_state(db)
    indexing = bool(state.get("running")) and not result["total"] and not source and not query and not genre
    response = current_app.make_response(render_template(
        "index.html", error=state.get("error", ""), warning=state.get("warning", ""), indexing=indexing,
        progress=state.get("progress") or [0, 0], items=result["items"], sources=index.sources(db, query, pivot_words if query else None, genre=genre),
        genres=genres.menu(index.genre_counts(db, source, query, pivot_words if query else None)), genre=genre, genre_names=genres.LABELS,
        genre_label=genres.LABELS.get(genre, "その他" if genre else ""), source=source, query=query, english_query=english, page=result["page"], pages=result["pages"],
        total=result["total"], csrf=csrf_token(), highlights=highlights, disputes=disputes,
        topic_cards=topic_cards, following=any(t.casefold() == query.casefold() for t in followed) if query else False,
    ))
    response.headers["Cache-Control"] = CACHE_LIST
    return response


def page_target() -> str:
    """The translation target that reads as the page's language (翻訳先 follows it until the reader picks one)."""
    return i18n.TARGETS[i18n.current()]


def _kept_title(title: str) -> str:
    from newsreader.services import translation
    return translation.cached_titles([title], page_target())[0] if title else ""


@routes.get("/a/<int:article_id>", "show_article")
def show_article(article_id: int):
    record = index.get(db_path(), article_id)
    if record is None:
        abort(404)
    try:
        article = load_article(record)
    except (ReaderError, OSError) as exc:
        return _error_page(str(exc) if isinstance(exc, ReaderError) else "記事ファイルを読み取れません。", 400)
    article = {**article, "images": image_entries(article_id, article)}
    from newsreader.library import stories
    related = stories.related(db_path(), article_id)
    if related:
        # Offer "read the others in this article's language" when the group mixes languages.
        own = stories.LANG_TARGETS.get(stories.headline_language(db_path(), article_id), "")
        others = {stories.LANG_TARGETS.get(m.get("lang") or "", "") for m in related["members"]}
        related["align"] = own if own and others - {own, ""} else ""
        # The group's outlets disagree on a count (background comparison, D-59): say so up top.
        from newsreader.library import facts_watch
        related["disputes"] = facts_watch.disputed(db_path(), [related["key"]]).get(related["key"], [])
        # このニュースの流れ (D-74): earlier / later groups of the same matter
        arc = stories.arc(db_path(), related["key"])
        current = next((g for g in arc if g["current"]), None)
        related["arc"] = len(arc) - 1 if arc else 0
        related["later"] = bool(current) and any(g["oldest"] > current["newest_time"] for g in arc if not g["current"])
    # 情報の出どころ (D-73): dateline, cited organisations, own reporting — read from the text.
    from newsreader.library import provenance
    prov = provenance.analyze(record.get("source") or "", article.get("paragraphs") or [])
    cited_outlets = {c["key"] for c in prov["cites"]}
    prov_links, seen = [], set()
    for m in (related or {}).get("members", []):
        if provenance.SELF.get(m.get("source")) in cited_outlets and m["source"] not in seen:
            seen.add(m["source"])
            prov_links.append(m)
    from newsreader.library import reading
    marks_db = reading.path_for(db_path())
    saved = False
    if not public.is_visitor():  # a visitor of the public site leaves no marks of the owner's
        saved = reading.states(marks_db, [record]).get(article_id, {}).get("saved", False)
        try:
            reading.mark_read(marks_db, record)
        except Exception:  # noqa: BLE001 - a read mark is a convenience; the page is shown regardless
            pass
    response = current_app.make_response(render_template(
        "article.html", article=article, record=record, article_id=article_id, csrf=csrf_token(), saved=saved,
        related=related, neighbours=index.neighbours(db_path(), article_id), prov=prov, prov_links=prov_links,
        similar=stories.similar(db_path(), article_id), title_ja=_kept_title(record.get("title") or ""),
    ))
    response.headers["Cache-Control"] = CACHE_PAGE
    return response


@routes.get("/manifest.webmanifest", "web_manifest")
def web_manifest():
    """Added to a phone's home screen, the site opens as its own app with its name and icon."""
    from newsreader import branding
    response = jsonify({
        "name": branding.APP_NAME, "short_name": branding.APP_NAME, "description": i18n.text(branding.APP_TAGLINE),
        "lang": i18n.current(),
        "start_url": url_for("index"), "scope": url_for("index"), "display": "standalone",
        "background_color": "#f6f4ef", "theme_color": "#f6f4ef",
        "icons": [{"src": url_for("static", filename=f"icon-{n}.png"), "sizes": f"{n}x{n}", "type": "image/png"} for n in (192, 512)],
    })
    response.mimetype = "application/manifest+json"
    response.headers["Cache-Control"] = "public, max-age=86400"
    return response


@routes.get("/favicon.ico", "favicon")
def favicon():
    """Browsers ask for /favicon.ico regardless of <link rel=icon>; answer without a 404."""
    return redirect(url_for("static", filename="favicon.svg"), code=301)


@routes.get("/article", "legacy_article_link")
def legacy_article_link():
    """Keep old bookmarks (``/article?file=...&path=SITE``) working."""
    record = index.find_by_file(db_path(), request.args.get("path", "").strip("/"), request.args.get("file", ""))
    if record is None:
        return redirect(url_for("index"))
    return redirect(url_for("show_article", article_id=record["id"]))


@routes.get("/img/<int:article_id>/<int:n>", "image")
def image(article_id: int, n: int):
    record = index.get(db_path(), article_id)
    if record is None or n >= image_service.MAX_IMAGE_COUNT:
        abort(404)
    try:
        article = load_article(record)
        images = article["images"]
        if n >= len(images):
            abort(404)
        source = images[n]["src"]
        requested = request.args.get("k", "")
        current = image_service.image_key(source)[:_KEY_CHARS]
        if requested and requested != current:
            # The article was re-saved with a different image: send the browser to the new URL.
            return redirect(image_url(article_id, n, source))
        path = image_service.cached_or_fetch(source, image_service.media_dir_for(article_file(record)),
                                             referer=article.get("url", ""))
    except (ReaderError, OSError, image_service.ImageUnavailable):
        response = current_app.make_response(("", 404))
        response.headers["Cache-Control"] = "no-store"
        return response
    response = send_file(path, mimetype=image_service.EXTENSION_MIMES.get(path.suffix.lower(), "application/octet-stream"),
                         conditional=True, max_age=YEAR if requested else 0)
    if requested:  # the URL names this exact image, so it never needs revalidating
        response.headers["Cache-Control"] = f"public, max-age={YEAR}, immutable"
    return response


@routes.get("/article-image", "article_image")
def legacy_article_image():
    """v3's image URL. Serves an already-cached file only; never fetches."""
    try:
        record = index.get(db_path(), int(request.args.get("id", "0")))
        if record is None:
            abort(404)
        image_name = request.args.get("image", "")
        if Path(image_name).name != image_name or Path(image_name).suffix.lower() not in IMAGE_EXTENSIONS:
            abort(404)
        media_dir = image_service.media_dir_for(article_file(record))
        image_path = media_dir / image_name
        if image_path.is_symlink() or not image_path.is_file() or image_path.resolve(strict=True).parent != media_dir.resolve():
            abort(404)
        return send_file(image_path, conditional=True)
    except (ReaderError, OSError, ValueError):
        abort(404)


# ---- errors ------------------------------------------------------------------

_MESSAGES = {
    400: "リクエストが正しくありません。ページを読み込み直してからもう一度お試しください。",
    403: "このアドレスからはアクセスできません。",
    404: "ページが見つかりません。記事が削除されたか、URLが変わった可能性があります。",
    405: "この操作はできません。",
    413: "送信データが大きすぎます。",
    500: "内部エラーが発生しました。アプリのコマンド画面に表示されたログを確認してください。",
}
_HEADINGS = {
    400: "うまく開けませんでした",
    403: "このページは開けません",
    404: "ページが見つかりません",
    405: "この操作はできません",
    413: "送信データが大きすぎます",
    500: "サーバーで問題が起きました",
}


def register_error_handlers(app) -> None:
    def handler(exc):
        status = getattr(exc, "code", 500) or 500
        message = _MESSAGES.get(status, _MESSAGES[500])
        if request.path.startswith("/api/"):
            return jsonify({"error": message}), status
        if status == 403:  # foreign Host: answer plainly, render nothing of the app
            return message, 403, {"Content-Type": "text/plain; charset=utf-8"}
        return _error_page(message, status)

    for status in _MESSAGES:
        app.register_error_handler(status, handler)
