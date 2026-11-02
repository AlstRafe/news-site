"""Body text of a news page without a hand-written adapter, for the technology outlets.

The paragraphs are found the way reading modes find them: the element whose own ``<p>``
children hold the most text is the article body, together with sibling blocks of the same
parent that also hold paragraphs (sites that break the body up around ads and figures).
An ``articleBody`` in the page's JSON-LD is used instead when the page gave far less text.

``site(...)`` makes an adapter module-like object (NAME, HOSTS, matches, parse) for a set of
hosts, so adding an outlet is one line in ``sources/__init__.py`` and one feed in the collector.
"""

from __future__ import annotations

import json
import re
from types import SimpleNamespace

from bs4 import BeautifulSoup, NavigableString, Tag

from newsreader.sources.base import Page, SiteParse

#: Never body text, wherever it sits.
_DROP = "script, style, noscript, template, nav, header, footer, aside, form, figure, figcaption, button, iframe, svg"
#: A paragraph shorter than this (characters) is not counted when looking for the body.
MIN_PARAGRAPH = 20
#: The body holds at least this share of all paragraph text on the page.
SHARE = 0.7
#: Lines that end or interrupt articles on many sites and are not the article.
_BOILERPLATE = re.compile(
    r"^(?:copyright|©|\(c\)|all rights reserved|read more|related:|see also|subscribe|sign up|advertisement|"
    r"follow us|share this|this article (?:was|is) |版權所有|著作權|더보기|무단\s?전재|저작권자|<저작권자)",
    re.I,
)


def _text(tag: Tag) -> str:
    # No separator: inline links and emphasis sit inside sentences (a space would split CJK words).
    return re.sub(r"\s+", " ", tag.get_text("")).strip()


def _json_ld_body(soup: BeautifulSoup) -> list[str]:
    best: list[str] = []
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            data = json.loads(script.string or "")
        except (ValueError, TypeError):
            continue
        stack = data if isinstance(data, list) else [data]
        while stack:
            item = stack.pop()
            if isinstance(item, list):
                stack.extend(item)
            elif isinstance(item, dict):
                stack.extend(v for k, v in item.items() if k == "@graph")
                body = item.get("articleBody")
                if isinstance(body, str):
                    parts = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n|\r?\n", body)]
                    parts = [p for p in parts if p]
                    if sum(map(len, parts)) > sum(map(len, best)):
                        best = parts
    return best


def _br_paragraphs(work: BeautifulSoup) -> list[str]:
    """Bodies written as text broken by <br> (common on Korean sites): the element with the most
    text of its own, split at the breaks."""
    best, best_len = None, 0
    for element in work.find_all(["div", "section", "article", "td"]):
        own = sum(len(str(child).strip()) for child in element.children if isinstance(child, NavigableString))
        if own > best_len:
            best, best_len = element, own
    if best is None or best_len < 200:
        return []
    lines: list[str] = []
    current: list[str] = []
    for child in best.children:
        if isinstance(child, Tag) and child.name == "br":
            lines.append(" ".join(current))
            current = []
        elif isinstance(child, NavigableString):
            current.append(str(child))
        elif isinstance(child, Tag) and child.name in ("b", "strong", "a", "span", "em", "i"):
            current.append(child.get_text(" "))
    lines.append(" ".join(current))
    return [re.sub(r"\s+", " ", line).strip() for line in lines if line.strip()]


def paragraphs(soup: BeautifulSoup) -> list[str]:
    """The article's paragraphs in order, or [] when no body is found."""
    from_ld = _json_ld_body(soup)
    work = BeautifulSoup(str(soup.body or soup), "html.parser")
    for tag in work.select(_DROP):
        tag.decompose()
    # The body is the deepest element that still holds most of the page's paragraph text:
    # start at the top and step into a child as long as one child holds SHARE of it all.
    texts = {id(p): _text(p) for p in work.find_all("p")}
    counted = [p for p in work.find_all("p") if len(texts[id(p)]) >= MIN_PARAGRAPH and not p.find_parent("a")]
    found: list[str] = []
    if counted:
        held: dict[int, int] = {}
        for p in counted:
            for ancestor in p.parents:
                held[id(ancestor)] = held.get(id(ancestor), 0) + len(texts[id(p)])
        body = work
        total = held.get(id(work), 0)
        while True:
            inner = [c for c in body.find_all(recursive=False) if held.get(id(c), 0) >= total * SHARE]
            if not inner:
                break
            body = inner[0]
        for p in body.find_all("p"):
            text = texts.get(id(p)) or _text(p)
            if text and not p.find("p") and not p.find_parent("a"):
                found.append(text)
    if sum(map(len, found)) < 200:
        found = _br_paragraphs(work) or found
    # JSON-LD bodies often lose the paragraph breaks: only when the page itself gave far less.
    if len(" ".join(found)) < len(" ".join(from_ld)) * 0.5:
        found = [p for p in from_ld if not _BOILERPLATE.match(p)]
    return [p for p in found if not _BOILERPLATE.match(p)]


def site(name: str, hosts: set[str], kind: str = "記事") -> SimpleNamespace:
    host_set = frozenset(hosts)

    def matches(page: Page) -> bool:
        return page.host in host_set

    def parse(page: Page) -> SiteParse:
        soup = page.soup
        return SiteParse(
            kind=kind,
            page_type=name,
            title_tag=soup.select_one("article h1") or soup.select_one("main h1") or soup.select_one("h1"),
            paragraphs=paragraphs(soup),
            image_selector="article figure img, main figure img",
            url_hosts=host_set,
        )

    return SimpleNamespace(NAME=name, HOSTS=host_set, matches=matches, parse=parse)
