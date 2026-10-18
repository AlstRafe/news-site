"""News HTML Reader: URL-driven article library with translation tools.

Navigation state lives in the URL (``/?source=BBC&page=2``, ``/a/42``), never in
the server session, so back/reload/bookmarks and multiple phones behave the same.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
from datetime import datetime, timezone
from urllib.parse import urlsplit
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, send_from_directory, session, url_for

from newsreader import paths
from newsreader.library import index as library
from newsreader.services.cache import monthly_translation_usage
from newsreader.services.dictionary import british_ipa, lookup_chinese, lookup_korean_hanja
from newsreader.services.images import _allowed_host as image_host_allowed, cache_article_images
from newsreader.services.settings import SecretStoreError, configured_secrets, delete_secret, get_settings, save_settings, set_secret, validate_network_settings
from newsreader.services.translation import MAX_ARTICLE_CHARS, MAX_SELECTION_CHARS, TARGETS, TranslationError, translate_paragraphs, translate_text
from newsreader.library.files import resolve_html_file, validate_directory
from newsreader.sources import IMAGE_EXTENSIONS, ReaderError, extract_article

# These two stay module-level: the tests redirect them at an isolated fixture.
LIBRARY_DB = library.DEFAULT_DB_PATH
DEFAULT_DIRECTORY = paths.articles_dir()
DEFAULT_BIND_HOST = "127.0.0.1"
DEFAULT_PORT = 5000
PER_PAGE = 24


def _persistent_secret_key() -> bytes:
    """Keep the session signing key across restarts so open pages keep working."""
    path = paths.SECRET_KEY_PATH
    try:
        data = path.read_bytes()
        if len(data) >= 32:
            return data
    except OSError:
        pass
    data = secrets.token_bytes(32)
    try:
        paths.INSTANCE_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    except OSError:
        pass
    return data


app = Flask(
    __name__,
    template_folder=str(paths.TEMPLATES_DIR),
    static_folder=str(paths.STATIC_DIR),
    static_url_path="/static",  # keep the pre-Phase-1 asset URLs working
)
app.secret_key = _persistent_secret_key()
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config["SEND_FILE_MAX_AGE_DEFAULT"] = 0
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.config["SESSION_COOKIE_SAMESITE"] = "Strict"
app.config["SESSION_COOKIE_HTTPONLY"] = True


def configured_bind_host() -> str:
    """Bind locally by default; remote hosting must use a specific private interface IP."""
    host = os.environ.get("NEWS_BIND_HOST", DEFAULT_BIND_HOST).strip()
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise RuntimeError("NEWS_BIND_HOSTにはIPアドレスを指定してください。") from exc
    tailscale_cgnat = ipaddress.ip_network("100.64.0.0/10")
    if not (address.is_loopback or address.is_private or address in tailscale_cgnat):
        raise RuntimeError("NEWS_BIND_HOSTにはloopbackまたはprivate IPだけを指定できます。")
    return str(address)


def trusted_hosts() -> set[str]:
    hosts = {"localhost", "127.0.0.1", "::1", configured_bind_host().lower()}
    hosts.update(item.strip().lower().rstrip(".") for item in os.environ.get("NEWS_ALLOWED_HOSTS", "").split(",") if item.strip())
    return hosts


def csrf_token() -> str:
    if "csrf" not in session:
        session["csrf"] = secrets.token_urlsafe(24)
    return session["csrf"]


@app.before_request
def localhost_only():
    # Host validation complements binding to loopback or one explicitly chosen private IP.
    host = request.host.rsplit(":", 1)[0].strip("[]").lower().rstrip(".")
    if host not in trusted_hosts():
        abort(403)


@app.template_filter("localtime")
def localtime_filter(iso_utc: str) -> str:
    """Render a stored UTC timestamp in the server's own timezone (no hard-coded zone)."""
    try:
        moment = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return moment.astimezone().strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ""


def library_root() -> Path:
    return validate_directory(DEFAULT_DIRECTORY)


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


def media_dir_for(record: dict, file_path: Path) -> Path:
    return file_path.with_name(file_path.stem + "_media")


def card_thumb(record: dict) -> str:
    """Prefer the locally cached image; otherwise hot-link only the known image hosts."""
    src = record.get("thumb_src", "")
    if not src:
        return ""
    try:
        file_path = article_file(record)
        key = hashlib.sha256(src.encode("utf-8")).hexdigest()[:24]
        cached = next(media_dir_for(record, file_path).glob(key + ".*"), None)
        if cached and cached.is_file() and not cached.is_symlink():
            return url_for("article_image", id=record["id"], image=cached.name)
    except (ReaderError, OSError):
        pass
    if src.startswith("https://") and image_host_allowed((urlsplit(src).hostname or "").lower()):
        return src
    return ""


def _library_error(message: str, status: int):
    return render_template("index.html", error=message, items=[], sources=[], source="", query="",
                           page=1, pages=1, total=0, csrf=csrf_token()), status


@app.get("/")
def index():
    source = request.args.get("source", "")[:100]
    query = request.args.get("q", "")
    try:
        page = int(request.args.get("page", "1"))
    except ValueError:
        page = 1
    try:
        library.sync(library_root(), LIBRARY_DB)
    except ReaderError as exc:
        return _library_error(str(exc), 404)
    result = library.list_articles(LIBRARY_DB, source=source, query=query, page=page, per_page=PER_PAGE)
    for item in result["items"]:
        item["thumb"] = card_thumb(item)
    return render_template(
        "index.html", error="", items=result["items"], sources=library.sources(LIBRARY_DB),
        source=source, query=query.strip()[:100], page=result["page"], pages=result["pages"],
        total=result["total"], csrf=csrf_token(),
    )


@app.get("/a/<int:article_id>")
def show_article(article_id: int):
    record = library.get(LIBRARY_DB, article_id)
    if record is None:
        abort(404)
    try:
        file_path = article_file(record)
        article = extract_article(file_path.read_bytes())
        article["images"] = cache_article_images(file_path, article["url"], article["images"])
        for image in article["images"]:
            if image.get("filename"):
                image["url"] = url_for("article_image", id=article_id, image=image["filename"])
    except (ReaderError, OSError) as exc:
        return _library_error(str(exc), 400)
    return render_template(
        "article.html", article=article, record=record, article_id=article_id,
        csrf=csrf_token(), translation_provider=get_settings()["provider"],
    )


@app.get("/article")
def legacy_article_link():
    """Keep old bookmarks (``/article?file=...&path=SITE``) working."""
    record = library.find_by_file(LIBRARY_DB, request.args.get("path", "").strip("/"), request.args.get("file", ""))
    if record is None:
        return redirect(url_for("index"))
    return redirect(url_for("show_article", article_id=record["id"]))


def _valid_csrf() -> bool:
    return hmac.compare_digest(request.headers.get("X-CSRF-Token", ""), session.get("csrf", "!"))


def _json_request() -> dict:
    if not request.is_json or not _valid_csrf():
        abort(400)
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        abort(400)
    return payload


@app.post("/api/translate")
def translate_selection():
    payload = _json_request()
    text = payload.get("text")
    target = payload.get("target")
    if not isinstance(text, str) or not isinstance(target, str) or target not in TARGETS:
        return jsonify({"error": "翻訳する文章または翻訳先の指定が不正です。"}), 400
    response = {"translation": None, "translation_error": None, "ipa": None, "hanja": [], "zhuyin": None, "dictionary_url": None}
    try:
        response["translation"] = translate_text(text, target, limit=MAX_SELECTION_CHARS)
    except TranslationError as exc:
        response["translation_error"] = str(exc)

    hangul = len(re.findall(r"[\uac00-\ud7a3]", text))
    han = len(re.findall(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if hangul:
        dictionary = lookup_korean_hanja(text)
        response["hanja"] = dictionary["entries"]
        response["dictionary_url"] = dictionary["source_url"]
        response["dictionary_label"] = dictionary["source_label"]
        response["dictionary_configured"] = dictionary["configured"]
    elif han and han >= latin:
        dictionary = lookup_chinese(text)
        response["zhuyin"] = dictionary["zhuyin"]
        response["dictionary_entries"] = dictionary.get("entries", [])
        response["dictionary_url"] = dictionary["source_url"]
        response["dictionary_label"] = dictionary["source_label"]
        if dictionary.get("entries"):
            response["meaning"] = "\n".join(entry["definition"] for entry in dictionary["entries"] if entry.get("definition")) or None
            response["meaning_language"] = "教育部辞典・繁體中文"
        if not response.get("meaning") and target == "zh-hant":
            try:
                response["meaning"] = translate_text(text, "ja", limit=MAX_SELECTION_CHARS)
                response["meaning_language"] = "日本語"
            except TranslationError:
                response["meaning"] = None
    elif latin:
        response["ipa"] = british_ipa(text)
        response["ipa_label"] = "英国英語 · eSpeak NG IPA"
    return jsonify(response)


@app.post("/api/translate-article")
def translate_whole_article():
    payload = _json_request()
    target = payload.get("target")
    article_id = payload.get("id")
    if not isinstance(target, str) or target not in TARGETS or not isinstance(article_id, int) or isinstance(article_id, bool):
        return jsonify({"error": "記事または翻訳先の指定が不正です。"}), 400
    record = library.get(LIBRARY_DB, article_id)
    if record is None:
        return jsonify({"error": "記事が見つかりません。"}), 404
    try:
        article = extract_article(article_file(record).read_bytes())
    except (ReaderError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        translated = translate_text("\n\n".join(article["paragraphs"]), target, limit=MAX_ARTICLE_CHARS)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"translation": translated, "title": article["title"]})


@app.post("/api/translate-paragraphs")
def translate_article_paragraphs():
    """Per-paragraph translation for the side-by-side (対訳) view; the title is item 0."""
    payload = _json_request()
    target = payload.get("target")
    article_id = payload.get("id")
    if not isinstance(target, str) or target not in TARGETS or not isinstance(article_id, int) or isinstance(article_id, bool):
        return jsonify({"error": "記事または翻訳先の指定が不正です。"}), 400
    record = library.get(LIBRARY_DB, article_id)
    if record is None:
        return jsonify({"error": "記事が見つかりません。"}), 404
    try:
        article = extract_article(article_file(record).read_bytes())
    except (ReaderError, OSError) as exc:
        return jsonify({"error": str(exc)}), 400
    try:
        translated = translate_paragraphs([article["title"], *article["paragraphs"]], target, limit=MAX_ARTICLE_CHARS)
    except TranslationError as exc:
        return jsonify({"error": str(exc)}), 503
    return jsonify({"title": translated[0], "paragraphs": translated[1:]})


@app.route("/translation-settings", methods=["GET", "POST"])
def translation_settings():
    message = ""
    error = ""
    if request.method == "POST":
        if not hmac.compare_digest(request.form.get("csrf", ""), session.get("csrf", "!")):
            abort(400)
        provider = request.form.get("provider", "")
        deepl_endpoint = request.form.get("deepl_endpoint", "")
        azure_region = request.form.get("azure_region", "").strip()
        network_bind_host = request.form.get("network_bind_host", "")
        network_port = request.form.get("network_port", "")
        naver_search_query = request.form.get("naver_search_query", "뉴스")
        try:
            if provider not in {"deepl", "azure"} or deepl_endpoint not in {"auto", "free", "paid"}:
                raise ValueError("設定値が不正です。")
            network_bind_host, network_port = validate_network_settings(network_bind_host, network_port)
            secrets_to_save = {
                "deepl_api_key": request.form.get("deepl_api_key", "").strip(),
                "azure_api_key": request.form.get("azure_api_key", "").strip(),
                "stdict_api_key": request.form.get("stdict_api_key", "").strip(),
                "naver_api_key_id": request.form.get("naver_api_key_id", "").strip(),
                "naver_api_key_secret": request.form.get("naver_api_key_secret", "").strip(),
            }
            for name, value in secrets_to_save.items():
                if request.form.get("remove_" + name) == "1":
                    delete_secret(name)
                elif value:
                    set_secret(name, value)
            save_settings(provider, deepl_endpoint, azure_region, network_bind_host, network_port, naver_search_query)
            message = "設定を保存しました。APIキーはこのPCの資格情報ストアに保存されています。接続IPまたはポートの変更はアプリ再起動後に反映されます。"
        except Exception as exc:
            app.logger.warning("Could not save translation settings (%s)", type(exc).__name__)
            error = str(exc) if isinstance(exc, ValueError) else "設定を保存できませんでした。APIキーを確認し、Windows資格情報ストアが利用できる状態で再試行してください。"
    secret_store_error = ""
    try:
        secret_status = configured_secrets()
    except SecretStoreError:
        secret_status = {name: False for name in ("deepl_api_key", "azure_api_key", "stdict_api_key", "naver_api_key_id", "naver_api_key_secret")}
        secret_store_error = "Windows資格情報ストアにアクセスできません。アプリを通常のWindowsユーザーとして起動し直してください。"
    return render_template(
        "settings.html", settings=get_settings(), secrets=secret_status,
        secret_store_error=secret_store_error,
        csrf=csrf_token(), message=message, error=error,
        usage=monthly_translation_usage(),
        first_run=request.args.get("first_run") == "1",
        active_bind_host=os.environ.get("NEWS_BIND_HOST", DEFAULT_BIND_HOST),
        active_port=os.environ.get("NEWS_PORT", str(DEFAULT_PORT)),
    )


@app.get("/article-image")
def article_image():
    try:
        record = library.get(LIBRARY_DB, int(request.args.get("id", "0")))
        if record is None:
            abort(404)
        image_name = request.args.get("image", "")
        if Path(image_name).name != image_name or Path(image_name).suffix.lower() not in IMAGE_EXTENSIONS:
            abort(404)
        media_dir = media_dir_for(record, article_file(record))
        image_path = media_dir / image_name
        if image_path.is_symlink() or not image_path.is_file() or image_path.resolve(strict=True).parent != media_dir.resolve():
            abort(404)
        return send_from_directory(media_dir, image_name, conditional=True)
    except (ReaderError, OSError, ValueError):
        abort(404)


def main() -> int:
    """Serve the app. Called by the thin root ``app.py`` that ``launcher.py`` runs."""
    bind_host = configured_bind_host()
    port = int(os.environ.get("NEWS_PORT", str(DEFAULT_PORT)))
    if ipaddress.ip_address(bind_host).is_loopback:
        # Keep the local editing workflow's automatic Python source reload.
        app.run(host=bind_host, port=port, debug=False, use_reloader=True)
    else:
        # Waitress is suitable for a continuously running private home server on Windows.
        from waitress import serve

        serve(app, host=bind_host, port=port, threads=8)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
