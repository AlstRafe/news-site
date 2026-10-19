"""Translation provider adapters with bounded inputs and a local response cache."""
from __future__ import annotations

import hashlib
import json
import re
import ssl
import urllib.error
import urllib.parse
import urllib.request

import truststore

from newsreader.services.cache import get as cache_get, put as cache_put, record_translation_usage
from newsreader.services.settings import SecretStoreError, get_secret, get_settings

#: Translation targets offered in the app, in menu order, with the Azure Translator code.
#: "en" is also the pivot language for same-news grouping (§10). DeepL was removed (D-44).
TARGETS = {
    "ja": {"azure": "ja"},
    "en": {"azure": "en"},
    "ko": {"azure": "ko"},
    "zh-hant": {"azure": "zh-Hant"},
}
TARGET_LABELS = {"ja": "日本語", "en": "English", "ko": "한국어", "zh-hant": "繁體中文"}
#: BCP 47 tags for the lang attribute of translated text.
LANG_TAGS = {"ja": "ja", "en": "en", "ko": "ko", "zh-hant": "zh-Hant"}
#: Kept for callers of the earlier split between UI and internal targets ("en" is now a UI target).
INTERNAL_TARGETS: dict[str, dict[str, str]] = {}


def target_menu(provider: str) -> dict[str, dict]:
    """{code: {"label", "tag", "available"}} for the pages (the browser reads it from base.html)."""
    return {code: {"label": TARGET_LABELS[code], "tag": LANG_TAGS[code], "available": True}
            for code in TARGETS}
MAX_SELECTION_CHARS = 4_000
MAX_ARTICLE_CHARS = 250_000
MAX_BATCH_CHARS = 20_000
MAX_BATCH_ITEMS = 50
_SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)


class TranslationError(Exception):
    """A safe-to-display provider or configuration error."""


def active_provider() -> str:
    """Azure Translator is the only provider (the provider name stays part of every cache key)."""
    return "azure"


def provider_ready(provider: str | None = None) -> bool:
    return bool(get_secret("azure_api_key"))


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
    if provider == "azure":
        try:
            key = get_secret("azure_api_key")
        except SecretStoreError as exc:
            raise TranslationError(str(exc)) from None
        if not key:
            raise TranslationError("Azure Translatorのキーが未設定です。設定画面から登録してください。")
        region = get_settings().get("azure_region", "").strip()
        query = urllib.parse.urlencode({"api-version": "3.0", "to": _codes(target)["azure"]})
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


#: An English word or short idiom ("fly", "carry out", "Prime Minister's") — what the Azure
#: dictionary answers. Anything longer is translated only.
_DICTIONARY_WORD = re.compile(r"[A-Za-z][A-Za-z'’.-]*(?: [A-Za-z][A-Za-z'’.-]*){0,2}")
DICTIONARY_POS = {"ADJ": "形容詞", "ADV": "副詞", "CONJ": "接続詞", "DET": "限定詞", "MODAL": "助動詞",
                  "NOUN": "名詞", "PREP": "前置詞", "PRON": "代名詞", "VERB": "動詞", "OTHER": "その他"}
DICTIONARY_CACHE_ENTRIES = 20_000
MAX_DICTIONARY_TERMS = 10


def dictionary_available(text: str, target: str) -> bool:
    """English source (a word or short idiom), and a target the Azure dictionary covers."""
    return (active_provider() == "azure" and target in ("ja", "ko", "zh-hant") and len(text.strip()) <= 40
            and _DICTIONARY_WORD.fullmatch(text.strip()) is not None)


def dictionary_lookup(text: str, target: str) -> list[dict]:
    """Senses of an English word from the Azure Translator dictionary, grouped by part of speech:
    ``[{"pos": "動詞", "terms": ["飛ぶ", "飛行する"]}, ...]`` (most likely first). Cached per word
    and target, including "not found" (an empty list), so a word is sent once."""
    word = text.strip().replace("’", "'")
    key = hashlib.sha256(("azure\0" + target + "\0" + word.lower()).encode("utf-8")).hexdigest()
    cached = cache_get("dictionary", key)
    if isinstance(cached, list):
        return cached
    try:
        secret = get_secret("azure_api_key")
    except SecretStoreError as exc:
        raise TranslationError(str(exc)) from None
    if not secret:
        raise TranslationError("Azure Translatorのキーが未設定です。設定画面から登録してください。")
    query = urllib.parse.urlencode({"api-version": "3.0", "from": "en", "to": _codes(target)["azure"]})
    headers = {"Ocp-Apim-Subscription-Key": secret, "Content-Type": "application/json; charset=utf-8"}
    region = get_settings().get("azure_region", "").strip()
    if region:
        headers["Ocp-Apim-Subscription-Region"] = region
    payload, response_headers = _post_json(
        "https://api.cognitive.microsofttranslator.com/dictionary/lookup?" + query, headers,
        json.dumps([{"Text": word}], ensure_ascii=False).encode("utf-8"), "azure")
    entry = payload[0] if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict) else None
    translations = entry.get("translations") if entry else None
    if not isinstance(translations, list):
        raise TranslationError("Azure Translatorから予期しない形式の応答がありました。")
    try:
        metered = int(response_headers.get("x-metered-usage", "").strip() or len(word))
    except ValueError:
        metered = len(word)
    record_translation_usage("azure", target, len(word), metered)
    senses = _group_senses(translations)
    cache_put("dictionary", key, senses, max_entries=DICTIONARY_CACHE_ENTRIES)
    return senses


def _group_senses(translations: list) -> list[dict]:
    """Most confident first; one row per part of speech in the order it first appears."""
    usable = [t for t in translations if isinstance(t, dict) and isinstance(t.get("displayTarget"), str)
              and t["displayTarget"].strip()]
    usable.sort(key=lambda t: -(t.get("confidence") if isinstance(t.get("confidence"), (int, float)) else 0))
    groups: dict[str, list[str]] = {}
    for item in usable[:MAX_DICTIONARY_TERMS]:
        pos = DICTIONARY_POS.get(str(item.get("posTag", "OTHER")).upper(), "その他")
        prefix = item.get("prefixWord") if isinstance(item.get("prefixWord"), str) else ""
        term = f"{prefix} {item['displayTarget'].strip()}".strip()
        if term not in groups.setdefault(pos, []):
            groups[pos].append(term)
    return [{"pos": pos, "terms": terms} for pos, terms in groups.items()]


def _codes(target: str) -> dict[str, str]:
    return TARGETS.get(target) or INTERNAL_TARGETS.get(target) or {}


def translate_titles_to_english(titles: list[str]) -> list[str]:
    """Headlines only, to English, one result per input (blank stays blank) — for same-news grouping.

    Uses the same per-batch cache as the reader, so a headline is never paid for twice. Raises
    TranslationError (no key, quota, network) for the caller to record; nothing is retried here.
    """
    cleaned = [title.strip() for title in titles]
    pieces = [title[:1_000] for title in cleaned if title]
    if not pieces:
        return ["" for _ in cleaned]
    provider = active_provider()
    translated = iter(_run_batches(provider, "en", _batches(pieces)))
    return [next(translated) if title else "" for title in cleaned]


#: Headlines translated for the list pages are kept one by one (a page shows a different mix each time).
TITLE_CACHE_ENTRIES = 20_000


def translate_titles(titles: list[str], target: str, *, kind: str = "title", max_chars: int = 1_000) -> list[str]:
    """One translation per headline (blank stays blank), each cached on its own so a headline is
    sent to the provider once per target, whichever list page it appears on. ``kind="lead"``
    does the same for the opening paragraphs compared on a same-news page."""
    if target not in TARGETS:
        raise TranslationError("翻訳先の指定が不正です。")
    provider = active_provider()
    cleaned = [title.strip()[:max_chars] for title in titles]
    keys = [hashlib.sha256((provider + "\0" + target + "\0" + title).encode("utf-8")).hexdigest() for title in cleaned]
    results = [cache_get(kind, key) if title else "" for title, key in zip(cleaned, keys)]
    missing = [i for i, value in enumerate(results) if not isinstance(value, str)]
    if missing:
        fresh: list[str] = []
        for batch in _batches([cleaned[i] for i in missing]):
            fresh.extend(_translate_batch(provider, batch, target))
        for i, text in zip(missing, fresh):
            results[i] = text
            cache_put(kind, keys[i], text, max_entries=TITLE_CACHE_ENTRIES)
    return [value if isinstance(value, str) else "" for value in results]


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
