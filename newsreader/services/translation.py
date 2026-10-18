"""Translation provider adapters with bounded inputs and a local response cache."""
from __future__ import annotations

import hashlib
import json
import ssl
import urllib.error
import urllib.parse
import urllib.request

import truststore

from newsreader.services.cache import get as cache_get, put as cache_put, record_translation_usage
from newsreader.services.settings import SecretStoreError, get_secret, get_settings

TARGETS = {
    "ja": {"deepl": "JA", "azure": "ja"},
    "en-gb": {"deepl": "EN-GB"},
    "ko": {"deepl": "KO", "azure": "ko"},
    "zh-hant": {"deepl": "ZH-HANT", "azure": "zh-Hant"},
}
MAX_SELECTION_CHARS = 4_000
MAX_ARTICLE_CHARS = 250_000
MAX_BATCH_CHARS = 20_000
MAX_BATCH_ITEMS = 50
_SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class TranslationError(Exception):
    """A safe-to-display provider or configuration error."""


def active_provider() -> str:
    return get_settings()["provider"]


def provider_ready(provider: str | None = None) -> bool:
    provider = provider or active_provider()
    secret = "deepl_api_key" if provider == "deepl" else "azure_api_key"
    return bool(get_secret(secret))


def _split_long(text: str) -> list[str]:
    """Split one over-long paragraph (preferably at spaces) into pieces of at most 4,000 characters."""
    pieces: list[str] = []
    rest = text.strip()
    while len(rest) > 4_000:
        split_at = rest.rfind(" ", 0, 4_000)
        if split_at < 800:  # no usable space (e.g. Chinese text): cut at the limit
            split_at = 4_000
        piece = rest[:split_at].strip()
        if piece:
            pieces.append(piece)
        rest = rest[split_at:].strip()
    if rest:
        pieces.append(rest)
    return pieces


def _batches(pieces: list[str]) -> list[list[str]]:
    batches: list[list[str]] = []
    current: list[str] = []
    total = 0
    for piece in pieces:
        if current and (len(current) >= MAX_BATCH_ITEMS or total + len(piece) > MAX_BATCH_CHARS):
            batches.append(current)
            current, total = [], 0
        current.append(piece)
        total += len(piece)
    if current:
        batches.append(current)
    return batches


def _chunks(texts: list[str]) -> list[list[str]]:
    pieces: list[str] = []
    for original in texts:
        text = original.strip()
        if text:
            pieces.extend(_split_long(text))
    return _batches(pieces)


def _post_json(url: str, headers: dict[str, str], data: bytes, provider: str) -> tuple[dict | list, dict[str, str]]:
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=25, context=_SSL_CONTEXT) as response:
            raw = response.read(2 * 1024 * 1024 + 1)
            response_headers = {key.lower(): value for key, value in response.headers.items()}
        if len(raw) > 2 * 1024 * 1024:
            raise TranslationError("翻訳サービスの応答が大きすぎます。")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, (dict, list)):
            raise TranslationError("翻訳サービスから予期しない形式の応答がありました。")
        return payload, response_headers
    except urllib.error.HTTPError as exc:
        # Provider response bodies can contain account diagnostics; never echo them or credentials.
        if exc.code == 401:
            message = "APIキーが認証されませんでした。設定画面でキーを確認してください。"
        elif exc.code == 429:
            message = "翻訳サービスの利用上限またはアクセス頻度の上限に達しました。時間を置いて再試行してください。"
        elif provider == "deepl" and exc.code == 456:
            message = "DeepL APIの利用上限に達しました。プランの上限または請求設定を確認してください。"
        elif provider == "azure" and exc.code == 403:
            message = "Azure Translatorの利用が拒否されました。F0無料枠の月間上限、キー、リソースのリージョンを確認してください。"
        elif exc.code == 403:
            message = "翻訳サービスで利用が拒否されました。APIキー、課金、API有効化、利用上限を確認してください。"
        else:
            message = f"翻訳サービスがエラーを返しました（HTTP {exc.code}）。設定または時間を置いた再試行をご確認ください。"
        raise TranslationError(message) from None
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, json.JSONDecodeError, UnicodeDecodeError):
        raise TranslationError("翻訳サービスに接続できませんでした。ネットワークとAPI設定を確認してください。") from None


def _translate_batch(provider: str, texts: list[str], target: str) -> list[str]:
    if provider == "deepl":
        try:
            key = get_secret("deepl_api_key")
        except SecretStoreError as exc:
            raise TranslationError(str(exc)) from None
        if not key:
            raise TranslationError("DeepL APIキーが未設定です。設定画面から登録してください。")
        endpoint_mode = get_settings()["deepl_endpoint"]
        is_free_key = key.rstrip().endswith(":fx")
        use_free = is_free_key if endpoint_mode == "auto" else endpoint_mode == "free"
        base = "https://api-free.deepl.com" if use_free else "https://api.deepl.com"
        body = urllib.parse.urlencode([("text", text) for text in texts] + [("target_lang", TARGETS[target]["deepl"])]).encode()
        payload, _ = _post_json(base + "/v2/translate", {"Authorization": f"DeepL-Auth-Key {key}", "Content-Type": "application/x-www-form-urlencoded"}, body, "deepl")
        results = payload.get("translations") if isinstance(payload, dict) else None
        if not isinstance(results, list) or len(results) != len(texts) or any(not isinstance(item, dict) or not isinstance(item.get("text"), str) for item in results):
            raise TranslationError("DeepLから予期しない形式の応答がありました。")
        record_translation_usage("deepl", target, sum(len(text) for text in texts))
        return [str(item.get("text", "")) for item in results]
    if provider == "azure":
        try:
            key = get_secret("azure_api_key")
        except SecretStoreError as exc:
            raise TranslationError(str(exc)) from None
        if not key:
            raise TranslationError("Azure Translatorのキーが未設定です。設定画面から登録してください。")
        if target not in TARGETS or "azure" not in TARGETS[target]:
            raise TranslationError("Azure Translatorは英語の地域変種を指定できないため、English (UK)への翻訳にはDeepLを選んでください。")
        region = get_settings().get("azure_region", "").strip()
        query = urllib.parse.urlencode({"api-version": "3.0", "to": TARGETS[target]["azure"]})
        body = json.dumps([{"Text": text} for text in texts], ensure_ascii=False).encode("utf-8")
        headers = {"Ocp-Apim-Subscription-Key": key, "Content-Type": "application/json; charset=utf-8"}
        if region:
            headers["Ocp-Apim-Subscription-Region"] = region
        payload, response_headers = _post_json(
            "https://api.cognitive.microsofttranslator.com/translate?" + query,
            headers,
            body, "azure",
        )
        if not isinstance(payload, list) or len(payload) != len(texts):
            raise TranslationError("Azure Translatorから予期しない形式の応答がありました。")
        results = []
        for item in payload:
            translations = item.get("translations") if isinstance(item, dict) else None
            translated = translations[0].get("text") if isinstance(translations, list) and translations and isinstance(translations[0], dict) else None
            if not isinstance(translated, str):
                raise TranslationError("Azure Translatorから予期しない形式の応答がありました。")
            results.append(translated)
        estimated_chars = sum(len(text) for text in texts)
        raw_metered = response_headers.get("x-metered-usage", "").strip()
        try:
            metered_chars = int(raw_metered) if raw_metered else estimated_chars
        except ValueError:
            metered_chars = estimated_chars
        record_translation_usage("azure", target, estimated_chars, metered_chars)
        return results
    raise TranslationError("翻訳サービスの設定が不正です。")


def _run_batches(provider: str, target: str, batches: list[list[str]]) -> list[str]:
    """Translate batches in order, reusing the per-batch cache; returns one result per input piece."""
    translated: list[str] = []
    for batch in batches:
        part_digest = hashlib.sha256((provider + "\0" + target + "\0" + json.dumps(batch, ensure_ascii=False)).encode("utf-8")).hexdigest()
        part_cached = cache_get("translation_part", part_digest)
        if isinstance(part_cached, list) and len(part_cached) == len(batch) and all(isinstance(item, str) for item in part_cached):
            translated.extend(part_cached)
            continue
        part_translated = _translate_batch(provider, batch, target)
        translated.extend(part_translated)
        cache_put("translation_part", part_digest, part_translated, max_entries=1000)
    return translated


def translate_text(text: str, target: str, *, limit: int) -> str:
    if target not in TARGETS:
        raise TranslationError("翻訳先の指定が不正です。")
    text = text.strip()
    if not text:
        raise TranslationError("翻訳する文章がありません。")
    if len(text) > limit:
        raise TranslationError(f"翻訳できる文字数を超えています（上限 {limit:,} 文字）。")
    provider = active_provider()
    digest = hashlib.sha256((provider + "\0" + target + "\0" + text).encode("utf-8")).hexdigest()
    cached = cache_get("translation", digest)
    if isinstance(cached, str):
        return cached
    if active_provider() == "azure" and "azure" not in TARGETS[target]:
        raise TranslationError("Azure Translatorは英語の地域変種を指定できないため、English (UK)への翻訳にはDeepLを選んでください。")
    translated = _run_batches(provider, target, _chunks(text.splitlines() or [text]))
    output = "\n\n".join(translated)
    cache_put("translation", digest, output, max_entries=300)
    return output


def translate_paragraphs(paragraphs: list[str], target: str, *, limit: int) -> list[str]:
    """Translate each paragraph separately: result[i] is the translation of paragraphs[i].

    Blank inputs stay blank. Over-long paragraphs are split for the provider and re-joined, so
    the output always has exactly as many items as the input (needed for side-by-side display).
    """
    if target not in TARGETS:
        raise TranslationError("翻訳先の指定が不正です。")
    cleaned = [text.strip() for text in paragraphs]
    if not any(cleaned):
        raise TranslationError("翻訳する文章がありません。")
    if sum(len(text) for text in cleaned) > limit:
        raise TranslationError(f"翻訳できる文字数を超えています（上限 {limit:,} 文字）。")
    provider = active_provider()
    if provider == "azure" and "azure" not in TARGETS[target]:
        raise TranslationError("Azure Translatorは英語の地域変種を指定できないため、English (UK)への翻訳にはDeepLを選んでください。")
    pieces: list[str] = []
    owners: list[int] = []
    for index, text in enumerate(cleaned):
        if text:
            for piece in _split_long(text):
                pieces.append(piece)
                owners.append(index)
    translated = _run_batches(provider, target, _batches(pieces))
    if len(translated) != len(pieces):
        raise TranslationError("翻訳結果の段落数が一致しませんでした。")
    results = [""] * len(cleaned)
    for owner, text in zip(owners, translated):
        results[owner] = f"{results[owner]} {text}".strip() if results[owner] else text
    return results
