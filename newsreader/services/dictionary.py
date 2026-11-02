"""Pronunciation and dictionary metadata from local eSpeak and official dictionaries."""

from __future__ import annotations

import hashlib
import json
import re
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request
from functools import lru_cache

import truststore
from bs4 import BeautifulSoup

from newsreader.services.cache import get as cache_get
from newsreader.services.cache import put as cache_put
from newsreader.services.settings import SecretStoreError, get_secret

MOE_SEARCH = "https://dict.revised.moe.edu.tw/search.jsp"
MOE_HOST = "dict.revised.moe.edu.tw"
KOREAN_SEARCH = "https://stdict.korean.go.kr/api/search.do"
KOREAN_VIEW = "https://stdict.korean.go.kr/api/view.do"
KOREAN_DICTIONARY = "https://stdict.korean.go.kr/"
MOE_DICTIONARY = "https://dict.revised.moe.edu.tw/index.jsp"
_SSL_CONTEXT = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
_ESPEAK_LOCK = threading.Lock()
_HANJA_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")
_HANGUL_WORD_RE = re.compile(r"[\uac00-\ud7a3]{1,16}")


def _fetch(url: str, *, timeout: int = 10, max_bytes: int = 2 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "NewsHtmlReader/1.0 (local dictionary lookup)"})
    with urllib.request.urlopen(request, timeout=timeout, context=_SSL_CONTEXT) as response:
        content = response.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise ValueError("Dictionary response too large")
    return content


@lru_cache(maxsize=512)
def _british_ipa_cached(text: str) -> str:
    from espeakng_loader import get_data_path, get_library_path
    from phonemizer import phonemize
    from phonemizer.backend.espeak.wrapper import EspeakWrapper

    with _ESPEAK_LOCK:
        EspeakWrapper.set_library(get_library_path())
        EspeakWrapper.set_data_path(get_data_path())
        result = phonemize(
            text, language="en-gb", backend="espeak", preserve_punctuation=True, with_stress=True, strip=True
        )
    return result.strip()


def british_ipa(text: str) -> str | None:
    if not text or not re.search(r"[A-Za-z]", text):
        return None
    try:
        output = _british_ipa_cached(text[:250])
        return output or None
    except Exception:
        # IPA is an enhancement and must not prevent the translation response.
        return None


def _normalise_word(text: str) -> str:
    return re.sub(r"\s+", "", text)


def lookup_chinese(text: str) -> dict:
    selected = text.strip()
    empty = {
        "zhuyin": None,
        "entries": [],
        "source_url": MOE_DICTIONARY,
        "source_label": "教育部《重編國語辭典修訂本》",
    }
    if not selected or len(selected) > 30:
        return empty
    cache_key = hashlib.sha256(("moedict-v2\0" + selected).encode("utf-8")).hexdigest()
    cached = cache_get("moedict", cache_key)
    if isinstance(cached, dict):
        return cached
    output = {**empty, "entries": []}
    try:
        query = urllib.parse.urlencode({"md": "1", "word": selected})
        html = _fetch(MOE_SEARCH + "?" + query, timeout=12)
        soup = BeautifulSoup(html, "html.parser")
        wanted = _normalise_word(selected)
        for row in soup.select("tr"):
            anchor = row.select_one('a[href*="dictView.jsp"]')
            if not anchor or _normalise_word(anchor.get_text("", strip=True)) != wanted:
                continue
            pronunciation = row.select_one("td.ph")
            if not pronunciation:
                continue
            link = row.get("data-link") or anchor.get("href") or ""
            parsed = urllib.parse.urlsplit(urllib.parse.urljoin("https://dict.revised.moe.edu.tw/", link))
            if parsed.scheme == "https" and parsed.hostname == MOE_HOST:
                output["source_url"] = urllib.parse.urlunsplit(parsed)
            syllables = [code.get_text("", strip=True) for code in pronunciation.select("code")]
            output["zhuyin"] = " ".join(syllables) if syllables else pronunciation.get_text("", strip=True)
            # The public entry page contains the official dictionary definition.
            # It is HTML rather than a documented JSON API, so extract the labeled
            # Interpretation row and keep the source link alongside the result.
            try:
                detail = BeautifulSoup(_fetch(output["source_url"], timeout=12), "html.parser")
                for detail_row in detail.select("tr"):
                    cells = detail_row.find_all(["th", "td"], recursive=False)
                    if len(cells) < 2:
                        cells = detail_row.find_all(["th", "td"])
                    if len(cells) < 2:
                        continue
                    label = re.sub(r"\s+", "", cells[0].get_text("", strip=True))
                    if not re.search(r"釋義|Interpretation", label, re.IGNORECASE):
                        continue
                    definition_cell = cells[-1]
                    definitions = [
                        re.sub(r"\s+", " ", item.get_text(" ", strip=True)) for item in definition_cell.select("li")
                    ]
                    if not definitions:
                        definition = re.sub(r"\s+", " ", definition_cell.get_text(" ", strip=True))
                        definitions = [definition] if definition else []
                    if definitions:
                        output["entries"] = [
                            {"word": anchor.get_text("", strip=True), "definition": definition}
                            for definition in definitions[:6]
                        ]
                    break
            except (OSError, ValueError, ssl.SSLError, urllib.error.URLError):
                pass
            break
    except (OSError, ValueError, ssl.SSLError, urllib.error.URLError):
        pass
    cache_put("moedict", cache_key, output, ttl=7 * 24 * 3600, max_entries=1000)
    return output


def _items(payload) -> list[dict]:
    if isinstance(payload, dict) and "channel" in payload:
        payload = payload["channel"]
    if isinstance(payload, dict) and "item" in payload:
        payload = payload["item"]
    if isinstance(payload, dict):
        return [payload]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _senses(item: dict) -> list[dict]:
    info = item.get("word_info") if isinstance(item.get("word_info"), dict) else item
    senses = info.get("sense") or info.get("sense_info") or info.get("senses") or item.get("sense_info") or []
    if isinstance(senses, dict):
        senses = [senses]
    if not isinstance(senses, list):
        return []
    return [sense for sense in senses if isinstance(sense, dict)]


def _hanja_and_meaning(item: dict) -> tuple[str, str]:
    info = item.get("word_info") if isinstance(item.get("word_info"), dict) else item
    origin = str(info.get("origin") or "")
    language_info = info.get("original_language_info") or info.get("original_language") or {}
    if isinstance(language_info, dict):
        origin += " " + str(language_info.get("original_language") or language_info.get("word") or "")
    elif isinstance(language_info, list):
        origin += " " + " ".join(
            str(part.get("original_language", "")) for part in language_info if isinstance(part, dict)
        )
    elif isinstance(language_info, str):
        origin += " " + language_info
    hanja = "".join(dict.fromkeys(_HANJA_RE.findall(origin)))
    definitions = []
    for sense in _senses(item):
        definition = re.sub(r"\s+", " ", str(sense.get("definition") or "")).strip()
        if definition and definition not in definitions:
            definitions.append(definition)
    return hanja, " / ".join(definitions[:3])


def _query_korean(word: str, key: str) -> list[dict]:
    params = {"key": key, "q": word, "req_type": "json", "num": "20", "method": "exact", "type2": "chinese"}
    body = _fetch(KOREAN_SEARCH + "?" + urllib.parse.urlencode(params), timeout=12)
    payload = json.loads(body.decode("utf-8-sig"))
    items = _items(payload)
    matched = []
    for item in items:
        info = item.get("word_info") if isinstance(item.get("word_info"), dict) else item
        if str(item.get("word") or info.get("word") or "").strip() == word:
            matched.append(item)
    result = []
    for item in matched:
        hanja, meaning = _hanja_and_meaning(item)
        target_code = str(item.get("target_code") or "")
        link = str(item.get("link") or "")
        if not hanja and target_code:
            view_params = {"key": key, "q": target_code, "req_type": "json", "method": "target_code"}
            try:
                view = json.loads(
                    _fetch(KOREAN_VIEW + "?" + urllib.parse.urlencode(view_params), timeout=12).decode("utf-8-sig")
                )
                entry = _items(view)
                if not entry and isinstance(view, dict):
                    entry = [view.get("channel", {}).get("item", {})]
                if entry and isinstance(entry[0], dict):
                    other_hanja, other_meaning = _hanja_and_meaning(entry[0])
                    hanja = other_hanja
                    meaning = meaning or other_meaning
            except (OSError, ValueError, ssl.SSLError, urllib.error.URLError, TypeError):
                pass
        if not hanja:
            continue
        parsed = urllib.parse.urlsplit(urllib.parse.urljoin(KOREAN_DICTIONARY, link))
        if parsed.scheme != "https" or parsed.hostname != "stdict.korean.go.kr":
            link = KOREAN_DICTIONARY + "search/searchView.do?word_no=" + urllib.parse.quote(target_code)
        else:
            link = urllib.parse.urlunsplit(parsed)
        result.append({"word": word, "hanja": hanja, "meaning": meaning, "source_url": link})
    return result


def lookup_korean_hanja(text: str) -> dict:
    try:
        key = get_secret("stdict_api_key")
    except SecretStoreError:
        return {
            "entries": [],
            "configured": False,
            "source_label": "국립국어원 표준국어대사전",
            "source_url": "https://stdict.korean.go.kr/openapi/openApiInfo.do",
        }
    if not key:
        return {
            "entries": [],
            "configured": False,
            "source_label": "국립국어원 표준국어대사전",
            "source_url": "https://stdict.korean.go.kr/openapi/openApiInfo.do",
        }
    terms = list(dict.fromkeys(_HANGUL_WORD_RE.findall(text.strip())))
    # Bound remote lookups for sentence selections; short selected words are checked exactly.
    terms = [term for term in terms if len(term) <= 12][:4]
    results = []
    for term in terms:
        cache_key = hashlib.sha256((term + "\0" + hashlib.sha256(key.encode()).hexdigest()).encode()).hexdigest()
        cached = cache_get("stdict", cache_key)
        if isinstance(cached, list):
            found = cached
        else:
            try:
                found = _query_korean(term, key)
            except (OSError, ValueError, ssl.SSLError, urllib.error.URLError, TypeError):
                found = []
            cache_put("stdict", cache_key, found, ttl=30 * 24 * 3600, max_entries=1000)
        results.extend(found)
        if len(results) >= 8:
            break
    # Keep distinct homonyms by Hanja while avoiding duplicate entries from repeated words.
    unique = []
    seen = set()
    for entry in results:
        identity = (entry["word"], entry["hanja"], entry["meaning"])
        if identity not in seen:
            seen.add(identity)
            unique.append(entry)
    return {
        "entries": unique[:8],
        "configured": True,
        "source_label": "국립국어원 표준국어대사전",
        "source_url": "https://stdict.korean.go.kr/",
    }
