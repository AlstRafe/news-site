"""The page's language: Japanese, Korean, British English or Traditional Chinese (Taiwan).

One choice for every site of the PC: the ``lang`` cookie (path "/"), which the Home Page server
sets when an address carries ``?lang=<code>`` (News Site run on its own does the same,
web/language.py). Without a choice the browser's languages decide (Accept-Language), and
Japanese when it sends none.

Texts are written in Japanese and looked up by that Japanese (``text("記事")``); one without a
translation is shown in Japanese. The translations are in ``locale/<lang>.json``: ``pages`` for
the templates and the Python code, ``scripts`` for the browser's scripts (served as
``/i18n/<lang>.js``). A translation may be ``{"one": "…", "other": "…"}`` where the number
(``count`` or ``n``) decides the form (British English: "1 article", "3 articles").

What is translated is the site, not the news: headlines and articles stay in their own
language, beside the translation the reader asks for (翻訳先, which follows the page's
language until the reader picks another).
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from markupsafe import Markup, escape

LANGS = ("ja", "ko", "en-GB", "zh-TW")
DEFAULT = "ja"
#: Each language in its own words (the menu).
NAMES = {"ja": "日本語", "ko": "한국어", "en-GB": "English (UK)", "zh-TW": "中文（台灣）"}
#: og:locale
LOCALES = {"ja": "ja_JP", "ko": "ko_KR", "en-GB": "en_GB", "zh-TW": "zh_TW"}
#: The translation target (services.translation.TARGETS) that reads as the page's language.
TARGETS = {"ja": "ja", "ko": "ko", "en-GB": "en", "zh-TW": "zh-hant"}
COOKIE = "lang"
COOKIE_SECONDS = 365 * 24 * 3600
LOCALE_DIR = Path(__file__).resolve().parent / "locale"

_tables: dict[str, dict] = {}
_HOLE = re.compile(r"\{(\w+)\}")


def table(lang: str) -> dict:
    """``{"pages": {ja: translation}, "scripts": {ja: translation}}`` of ``lang`` ({} for Japanese)."""
    if lang not in _tables:
        try:
            loaded = json.loads((LOCALE_DIR / f"{lang}.json").read_text(encoding="utf-8")) if lang != DEFAULT else {}
        except (OSError, ValueError):
            loaded = {}
        _tables[lang] = {"pages": loaded.get("pages", {}), "scripts": loaded.get("scripts", {})}
    return _tables[lang]


def current() -> str:
    """The language of the page being made (Japanese outside a request)."""
    try:
        from flask import g, has_app_context

        if has_app_context():
            return g.get("lang", DEFAULT)
    except ImportError:  # pragma: no cover - Flask is a requirement
        pass
    return DEFAULT


def text(ja: str, lang: str | None = None, **values) -> str:
    """``ja`` in the page's language (or ``lang``), ``{name}`` filled in from ``values``."""
    said = _said(ja, lang or current(), values)
    return fill(said, values) if values else said


def markup(ja: str, **values) -> Markup:
    """``text`` for a template where a value is markup (a link, <strong>): the text is escaped, a
    Markup value is kept as it is."""
    return Markup(fill(str(escape(_said(ja, current(), values))), values, put=lambda v: str(escape(v))))


def _said(ja: str, lang: str, values: dict) -> str:
    said = table(lang)["pages"].get(ja, ja) if lang != DEFAULT else ja
    if isinstance(said, dict):
        said = said.get("one" if values.get("count", values.get("n")) == 1 else "other") or ja
    return said


def fill(said: str, values: dict, put=str) -> str:
    """``{name}`` in ``said`` replaced by ``values[name]`` (one without a value is left as it is)."""
    return _HOLE.sub(lambda m: put(values[m.group(1)]) if m.group(1) in values else m.group(0), said)


def from_browser(header: str) -> str:
    """Accept-Language to one of LANGS: zh-* reads Traditional Chinese, en-* British English; a browser
    that names none of ours gets English (more likely read than Japanese), one that sends nothing Japanese.
    (The Home Page server reads it the same way: homepage/i18n.py.)"""
    if not header.strip():
        return DEFAULT
    ranked = []
    for number, part in enumerate(header.split(",")):
        tag, _, rest = part.strip().partition(";")
        try:
            weight = float(rest.strip()[2:]) if rest.strip().startswith("q=") else 1.0
        except ValueError:
            weight = 0.0
        ranked.append((-weight, number, tag.strip().lower()))
    for weight, _, tag in sorted(ranked):
        if weight >= 0:
            continue
        for prefix, lang in (("ja", "ja"), ("ko", "ko"), ("zh", "zh-TW"), ("en", "en-GB")):
            if tag == prefix or tag.startswith(prefix + "-"):
                return lang
    return "en-GB"


def chosen(cookie: str, accept_language: str) -> str:
    """The visitor's language: the cookie's, else the browser's."""
    return cookie if cookie in LANGS else from_browser(accept_language or "")


def menu(lang: str) -> Markup:
    """The language menu every site of the PC shows in its header: a <details> that needs no script
    (each choice is a link with ?lang=), the current language marked."""
    marked = ' aria-current="true"'  # (no backslash inside an f-string's braces before Python 3.12)
    items = "".join(
        f'<li><a href="?{COOKIE}={code}" lang="{code}" hreflang="{code}"'
        f"{marked if code == lang else ''}>{escape(NAMES[code])}</a></li>"
        for code in LANGS
    )
    label = escape(text("言語", lang))
    return Markup(
        f'<details class="lang"><summary title="{label}" aria-label="{label}: {escape(NAMES[lang])}">'
        '<svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="9"/><path d="M3 12h18M12 3c2.5 2.6 3.8 5.6 3.8 9s-1.3 6.4-3.8 9c-2.5-2.6-3.8-5.6-3.8-9S9.5 5.6 12 3z"/></svg>'
        f"<span>{escape(NAMES[lang])}</span></summary><ul>{items}</ul></details>"
    )


# ---- dates and times, as people write them in each language -----------------------------------

_WEEKDAYS = {"ja": "月火水木金土日", "ko": "월화수목금토일", "zh-TW": "一二三四五六日"}
_EN_WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
_EN_MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def _weekday(lang: str, moment: datetime) -> str:
    """（金） / (금) / （週五）, after a date."""
    if lang == "ko":
        return f" ({_WEEKDAYS['ko'][moment.weekday()]})"
    return f"（{'週' if lang == 'zh-TW' else ''}{_WEEKDAYS.get(lang, _WEEKDAYS['ja'])[moment.weekday()]}）"


def day(moment: datetime, lang: str | None = None) -> str:
    """10月19日（月） / 10월 19일 (월) / Monday 19 October / 10月19日（週一）."""
    lang = lang or current()
    if lang == "en-GB":
        return f"{_EN_WEEKDAYS[moment.weekday()]} {moment.day} {_EN_MONTHS[moment.month - 1]}"
    if lang == "ko":
        return f"{moment.month}월 {moment.day}일{_weekday(lang, moment)}"
    return f"{moment.month}月{moment.day}日{_weekday(lang, moment)}"


def date_time(moment: datetime, lang: str | None = None) -> str:
    """2026年10月23日（金）16:20 / 2026년 10월 23일 (금) 16:20 / Fri 23 Oct 2026, 16:20 / 2026年10月23日（週五）16:20."""
    lang = lang or current()
    if lang == "en-GB":
        return f"{_EN_WEEKDAYS[moment.weekday()][:3]} {moment.day} {_EN_MONTHS[moment.month - 1][:3]} {moment.year}, {moment:%H:%M}"
    if lang == "ko":
        return f"{moment.year}년 {moment.month}월 {moment.day}일{_weekday(lang, moment)} {moment:%H:%M}"
    return f"{moment.year}年{moment.month}月{moment.day}日{_weekday(lang, moment)}{moment:%H:%M}"


def short_time(moment: datetime, lang: str | None = None) -> str:
    """10/13 21:00 (13 Oct 21:00 in British English, where 10/13 reads as a 10th month day 13 nowhere)."""
    if (lang or current()) == "en-GB":
        return f"{moment.day} {_EN_MONTHS[moment.month - 1][:3]} {moment:%H:%M}"
    return moment.strftime("%m/%d %H:%M")


def month_day(moment: datetime, lang: str | None = None) -> str:
    """10月27日 / 10월 27일 / 27 Oct."""
    lang = lang or current()
    if lang == "en-GB":
        return f"{moment.day} {_EN_MONTHS[moment.month - 1][:3]}"
    if lang == "ko":
        return f"{moment.month}월 {moment.day}일"
    return f"{moment.month}月{moment.day}日"


def minutes(count: int, lang: str | None = None) -> str:
    """A span of time, short: 45分 / 3時間10分 / 2日 (and their equivalents)."""
    lang = lang or current()
    if count < 60:
        return text("{n}分", lang, n=count)
    if count < 48 * 60:
        hours, rest = divmod(count, 60)
        return text("{h}時間{m}分", lang, h=hours, m=rest) if rest and hours < 6 else text("{n}時間", lang, n=hours)
    return text("{n}日", lang, n=count // (24 * 60))
