"""中央社 CNA (Central News Agency, Taiwan) article pages."""
from __future__ import annotations

import re

from newsreader.sources.base import Page, SiteParse

NAME = "中央社 CNA"
HOSTS = frozenset({"cna.com.tw", "www.cna.com.tw"})

# Related-story and app-download blocks share the .paragraph class with real text.
_PARAGRAPHS = "article.article .centralContent .paragraph:not(.moreArticle):not(.appDownload) > p"
#: The desk's filing code after the editor's name, e.g. "（編輯：楊惟敬）1151020" (ROC year 115, 10/20).
_FILING_CODE = re.compile(r"(?<=[)）])\s*\d{7}$")


def matches(page: Page) -> bool:
    return page.host in HOSTS or bool(page.soup.select_one("article.article .centralContent"))


def parse(page: Page) -> SiteParse:
    soup = page.soup
    title_tag = soup.select_one("article.article h1")
    if title_tag is None:  # ``is None`` here (not truthiness) exactly as in v3
        title_tag = soup.select_one("h1")
    return SiteParse(
        kind=NAME,
        page_type=NAME,
        title_tag=title_tag,
        paragraphs=_without_filing_code([p.get_text(" ", strip=True) for p in soup.select(_PARAGRAPHS)]),
        image_selector="article.article .centralContent img",
        url_hosts=HOSTS,
    )


def _without_filing_code(paragraphs: list[str]) -> list[str]:
    if paragraphs:
        paragraphs[-1] = _FILING_CODE.sub("", paragraphs[-1])
    return paragraphs
