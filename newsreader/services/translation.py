"""Translation provider adapters with bounded inputs and a local response cache."""
from __future__ import annotations

import contextvars
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


# ---- 訳語の指定 (D-63) ------------------------------------------------------------
#: A relay request translates with the client's own glossary (or none), not the 本体's.
_GLOSSARY_OVERRIDE: contextvars.ContextVar = contextvars.ContextVar("glossary_override", default=None)
#: Set while a page translates on its own (headlines, captions, the article opened in 対訳): such
#: requests stop at AUTO_MONTHLY_CHARS a month so they never use up Azure's free 2M characters;
#: what the reader asks for with a button is never held back.
_AUTOMATIC: contextvars.ContextVar = contextvars.ContextVar("automatic", default=False)
AUTO_MONTHLY_CHARS = 1_600_000
#: Set for a visitor of the public (テスト公開) site: all their translations count as automatic.
visitor_translations: contextvars.ContextVar = contextvars.ContextVar("visitor_translations", default=False)


class automatic:
    """``with automatic(flag):`` marks the translations inside as not asked for by a button."""

    def __init__(self, on: bool):
        self.on = bool(on)

    def __enter__(self):
        self._token = _AUTOMATIC.set(self.on)
        return self

    def __exit__(self, *exc):
        _AUTOMATIC.reset(self._token)
        return False


def _check_automatic() -> None:
    """Raises once the month's automatic translations reach AUTO_MONTHLY_CHARS (inside ``automatic``)."""
    if not (_AUTOMATIC.get() or visitor_translations.get()):
        return
    from newsreader.services import cache
    used = cache.monthly_translation_usage()["providers"].get("azure", {}).get("metered_chars", 0)
    if used >= AUTO_MONTHLY_CHARS:
        raise TranslationError("今月の自動翻訳の目安に達したため、見出しなどの自動翻訳を止めています（ボタンからの翻訳は使えます）。")


def glossary_for(target: str) -> list[tuple[str, str]]:
    """(name, fixed translation) pairs for a target, longest name first."""
    override = _GLOSSARY_OVERRIDE.get()
    if override is not None:
        entries = list(override)
    else:
        from newsreader.services.settings import glossary
        entries = [(e["source"], e["target"]) for e in glossary() if e["lang"] == target]
    return sorted(dict(entries).items(), key=lambda pair: (-len(pair[0]), pair[0]))


def _key_prefix(provider: str, target: str) -> str:
    """Cache keys change with the glossary (and stay as before without one)."""
    entries = glossary_for(target)
    if not entries:
        return provider
    return provider + "\0g" + hashlib.sha256(json.dumps(entries, ensure_ascii=False).encode("utf-8")).hexdigest()[:16]


def apply_glossary(text: str, entries: list[tuple[str, str]]) -> str:
    """Mark each name with its fixed translation (Azure dynamic dictionary markup)."""
    if not entries:
        return text
    fixed = dict(entries)
    pattern = re.compile("|".join(re.escape(name) for name, _ in entries))
    return pattern.sub(lambda m: f'<mstrans:dictionary translation="{fixed[m.group(0)]}">{m.group(0)}</mstrans:dictionary>',
                       text)


def active_provider() -> str:
    """"azure" (this PC's key) or "relay" (another NewsBridge translates, relay.py). The provider
    name is part of every cache key, so the two never mix."""
    return "relay" if get_settings().get("translation_source") == "relay" else "azure"


def provider_ready(provider: str | None = None) -> bool:
    if (provider or active_provider()) == "relay":
        from newsreader.services import relay
        return relay.client_configured()
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
    if provider == "relay":
        from newsreader.services import relay
        try:
            out = relay.translate(texts, target)
        except (relay.RelayError, SecretStoreError) as exc:
            raise TranslationError(str(exc)) from None
        return [polished(target, source, text) for source, text in zip(texts, out)]
    if provider == "azure":
        _check_automatic()
        try:
            key = get_secret("azure_api_key")
        except SecretStoreError as exc:
            raise TranslationError(str(exc)) from None
        if not key:
            raise TranslationError("Azure Translatorのキーが未設定です。設定画面から登録してください。")
        region = get_settings().get("azure_region", "").strip()
        # Sentence lengths cost nothing extra; kept for sentence-by-sentence pairing (sentences.py).
        query = urllib.parse.urlencode({"api-version": "3.0", "to": _codes(target)["azure"],
                                        "includeSentenceLength": "true"})
        entries = glossary_for(target)
        sent = [apply_glossary(text, entries) for text in texts]
        body = json.dumps([{"Text": text} for text in sent], ensure_ascii=False).encode("utf-8")
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
        from newsreader.services import sentences
        for source, marked, item in zip(texts, sent, payload):
            translations = item.get("translations") if isinstance(item, dict) else None
            translated = translations[0].get("text") if isinstance(translations, list) and translations and isinstance(translations[0], dict) else None
            if not isinstance(translated, str):
                raise TranslationError("Azure Translatorから予期しない形式の応答がありました。")
            lengths = translations[0].get("sentLen")
            if isinstance(lengths, dict) and marked == source:  # lengths of marked-up text would not fit
                sentences.remember(target, source, translated, lengths.get("srcSentLen"), lengths.get("transSentLen"))
            results.append(polished(target, source, translated))
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
    return (active_provider() in ("azure", "relay") and target in ("ja", "ko", "zh-hant") and len(text.strip()) <= 40
            and _DICTIONARY_WORD.fullmatch(text.strip()) is not None)


def dictionary_lookup(text: str, target: str) -> list[dict]:
    """Senses of an English word from the Azure Translator dictionary, grouped by part of speech:
    ``[{"pos": "動詞", "terms": ["飛ぶ", "飛行する"]}, ...]`` (most likely first). Cached per word
    and target, including "not found" (an empty list), so a word is sent once."""
    word = text.strip().replace("’", "'")
    provider = active_provider()
    key = hashlib.sha256((("azure" if provider == "azure" else "relay") + "\0" + target + "\0" + word.lower()).encode("utf-8")).hexdigest()
    cached = cache_get("dictionary", key)
    if isinstance(cached, list):
        return cached
    if provider == "relay":
        from newsreader.services import relay
        try:
            senses = relay.dictionary(word, target)
        except (relay.RelayError, SecretStoreError) as exc:
            raise TranslationError(str(exc)) from None
        cache_put("dictionary", key, senses, max_entries=DICTIONARY_CACHE_ENTRIES)
        return senses
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
    return TARGETS.get(target) or {}


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
    prefix = _key_prefix(provider, target)
    keys = [hashlib.sha256((prefix + "\0" + target + "\0" + title).encode("utf-8")).hexdigest() for title in cleaned]
    results = [cache_get(kind, key) if title else "" for title, key in zip(cleaned, keys)]
    for i, (title, value) in enumerate(zip(cleaned, results)):
        if title and isinstance(value, str) and (fixed := _finished(kind, target, title, value)) != value:
            results[i] = fixed
            cache_put(kind, keys[i], fixed, max_entries=TITLE_CACHE_ENTRIES)
    missing = [i for i, value in enumerate(results) if not isinstance(value, str)]
    if missing:
        fresh: list[str] = []
        for batch in _batches([cleaned[i] for i in missing]):
            fresh.extend(_translate_batch(provider, batch, target))
        for i, text in zip(missing, fresh):
            results[i] = text = _finished(kind, target, cleaned[i], text)
            cache_put(kind, keys[i], text, max_entries=TITLE_CACHE_ENTRIES)
    return [value if isinstance(value, str) else "" for value in results]


def _finished(kind: str, target: str, source: str, text: str) -> str:
    """A translated headline or opening as it is shown: polished, and a headline in headline form."""
    text = polished(target, source, text)
    if kind == "title" and target == "ja":
        from newsreader.services import ja_polish
        text = ja_polish.headline(text)
    return text


def cached_titles(titles: list[str], target: str) -> list[str]:
    """The headline translations already kept (never sends anything): "" where none is kept yet.
    Lets a page print the translated headlines it already has instead of fetching them after load."""
    if target not in TARGETS:
        return ["" for _ in titles]
    try:
        provider = active_provider()
    except Exception:  # noqa: BLE001 - no provider configured: nothing kept to show
        return ["" for _ in titles]
    prefix = _key_prefix(provider, target)
    out: list[str] = []
    for title in titles:
        title = title.strip()[:1_000]
        value = cache_get("title", hashlib.sha256((prefix + "\0" + target + "\0" + title).encode("utf-8")).hexdigest()) if title else ""
        if not isinstance(value, str) or not value:
            out.append("")
        else:  # kept values are polished already (translate_titles rewrites old ones when it meets them)
            from newsreader.services import ja_polish
            out.append(ja_polish.headline(value) if target == "ja" else value)
    return out


#: Fixed names the engine gets wrong, applied when the source names them: (in source, wrong, right).
_JA_NAMES = (("中央社", re.compile("中央通信(?!社)"), "中央社"),)


def polished(target: str, source: str, translated: str) -> str:
    """The engine's Japanese as one consistently written article (ja_polish), keeping the
    sentence-by-sentence pairing: the kept sentence lengths are moved to the polished text."""
    if target != "ja" or not translated:
        return translated
    from newsreader.services import ja_polish, sentences
    kept = sentences.reported(target, source, translated)
    lengths = sentences._as_utf16(translated, kept["tr"]) if kept and isinstance(kept.get("tr"), list) else None
    pieces = sentences.cut(translated, lengths) if lengths else [translated]
    out = ja_polish.polish_pieces(pieces)
    for name, wrong, right in _JA_NAMES:
        if name in source:
            out = [wrong.sub(right, piece) for piece in out]
    text = "".join(out)
    if text != translated and lengths:
        sentences.remember(target, source, text, kept["src"], sentences.utf16_lengths(out))
    return text


def _run_batches(provider: str, target: str, batches: list[list[str]]) -> list[str]:
    """Translate batches in order, reusing the per-batch cache; returns one result per input piece."""
    translated: list[str] = []
    for batch in batches:
        part_digest = hashlib.sha256((_key_prefix(provider, target) + "\0" + target + "\0"
                                      + json.dumps(batch, ensure_ascii=False)).encode("utf-8")).hexdigest()
        part_cached = cache_get("translation_part", part_digest)
        if isinstance(part_cached, list) and len(part_cached) == len(batch) and all(isinstance(item, str) for item in part_cached):
            # Kept before ja_polish existed: polished now, and kept that way.
            fixed = [polished(target, source, text) for source, text in zip(batch, part_cached)]
            if fixed != part_cached:
                cache_put("translation_part", part_digest, fixed, max_entries=1000)
            translated.extend(fixed)
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
    digest = hashlib.sha256((_key_prefix(provider, target) + "\0" + target + "\0" + text).encode("utf-8")).hexdigest()
    cached = cache_get("translation", digest)
    if isinstance(cached, str):
        return polished(target, text, cached)
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
