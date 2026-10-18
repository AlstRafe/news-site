"""The settings page: provider choice, API keys (stored in the OS credential store), connection.

API keys are written to the credential store and never echoed back or logged; the page
only shows whether each one is set.
"""
from __future__ import annotations

import os

from flask import current_app, render_template, request

from newsreader.services.cache import monthly_translation_usage
from newsreader.services.settings import (
    SECRET_NAMES, STORY_DAILY_RANGE, SecretStoreError, configured_secrets, delete_secret, get_settings,
    get_story_settings, save_settings, save_story_settings, set_secret, validate_network_settings,
)
from newsreader.web.routing import Routes
from newsreader.web.security import DEFAULT_BIND_HOST, DEFAULT_PORT, csrf_token

routes = Routes()


def _story_daily(raw: str) -> int:
    """Accept "8000", "8,000" or full-width digits; anything else (or out of range) is a ValueError."""
    low, high = STORY_DAILY_RANGE
    text = raw.strip().translate(str.maketrans("０１２３４５６７８９", "0123456789")).replace(",", "").replace("，", "")
    if not text.isdigit() or not low <= int(text) <= high:
        raise ValueError(f"見出し英訳の1日の上限は{low:,}〜{high:,}文字で指定してください。")
    return int(text)


@routes.route("/translation-settings", "translation_settings", methods=("GET", "POST"))
def translation_settings():
    message = ""
    error = ""
    if request.method == "POST":  # CSRF already verified centrally
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
            # Forms from before the same-news section (or scripts) omit it: leave those settings alone.
            stories_form = "stories_daily_chars" in request.form
            stories_daily = _story_daily(request.form["stories_daily_chars"]) if stories_form else 0
            secrets_to_save = {name: request.form.get(name, "").strip() for name in sorted(SECRET_NAMES)}
            for name, value in secrets_to_save.items():
                if request.form.get("remove_" + name) == "1":
                    delete_secret(name)
                elif value:
                    set_secret(name, value)
            save_settings(provider, deepl_endpoint, azure_region, network_bind_host, network_port, naver_search_query)
            if stories_form:
                save_story_settings(request.form.get("stories_enabled") == "1", stories_daily)
            message = "設定を保存しました。APIキーはこのPCの資格情報ストアに保存されています。接続IPまたはポートの変更はアプリ再起動後に反映されます。"
        except Exception as exc:
            # Never log the exception text: it could echo a submitted key.
            current_app.logger.warning("Could not save translation settings (%s)", type(exc).__name__)
            error = str(exc) if isinstance(exc, ValueError) else "設定を保存できませんでした。APIキーを確認し、Windows資格情報ストアが利用できる状態で再試行してください。"
    secret_store_error = ""
    try:
        secret_status = configured_secrets()
    except SecretStoreError:
        secret_status = {name: False for name in SECRET_NAMES}
        secret_store_error = "Windows資格情報ストアにアクセスできません。アプリを通常のWindowsユーザーとして起動し直してください。"
    usage = monthly_translation_usage()
    azure = usage["providers"]["azure"]["metered_chars"]
    return render_template(
        "settings.html", settings=get_settings(), secrets=secret_status, story_settings=get_story_settings(),
        story_range=STORY_DAILY_RANGE,
        secret_store_error=secret_store_error,
        csrf=csrf_token(), message=message, error=error,
        usage=usage, azure_free_limit=2_000_000, azure_used=min(azure, 2_000_000),
        first_run=request.args.get("first_run") == "1",
        active_bind_host=os.environ.get("NEWS_BIND_HOST", DEFAULT_BIND_HOST),
        active_port=os.environ.get("NEWS_PORT", str(DEFAULT_PORT)),
    )
