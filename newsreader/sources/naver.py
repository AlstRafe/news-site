"""NAVER NEWS article pages (``n.news.naver.com/article/<office>/<id>``)."""
from __future__ import annotations

from newsreader.sources.base import Page, SiteParse

NAME = "NAVER NEWS"
#: Hosts whose canonical URL is kept as the article's link.
HOSTS = frozenset({"n.news.naver.com", "news.naver.com"})
_ARTICLE_HOST = "n.news.naver.com"


def matches(page: Page) -> bool:
    return page.host == _ARTICLE_HOST or bool(page.soup.select_one("#dic_area"))


def parse(page: Page) -> SiteParse:
    soup = page.soup
    body = soup.select_one("#dic_area")
    if body:
        # NAVER stores paragraph breaks as pairs of <br> tags, not <p> elements.
        paragraphs = [part.strip() for part in body.get_text("\n").splitlines() if part.strip()]
    else:
        paragraphs = []
    return SiteParse(
        kind=NAME,
        page_type=NAME,
        title_tag=soup.select_one("#title_area"),
        paragraphs=paragraphs,
        image_selector="#img_area img, #dic_area img, #newsct_article figure img",
        url_hosts=HOSTS,
    )
