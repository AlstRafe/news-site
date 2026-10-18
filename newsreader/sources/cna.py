"""中央社 CNA (Central News Agency, Taiwan) article pages."""
from __future__ import annotations

from newsreader.sources.base import Page, SiteParse

NAME = "中央社 CNA"
HOSTS = frozenset({"cna.com.tw", "www.cna.com.tw"})

# Related-story and app-download blocks share the .paragraph class with real text.
_PARAGRAPHS = "article.article .centralContent .paragraph:not(.moreArticle):not(.appDownload) > p"


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
        paragraphs=[p.get_text(" ", strip=True) for p in soup.select(_PARAGRAPHS)],
        image_selector="article.article .centralContent img",
        url_hosts=HOSTS,
    )
