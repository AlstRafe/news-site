"""연합뉴스 Yonhap News Agency (Korean) article pages, www.yna.co.kr/view/AKR…"""
from __future__ import annotations

from newsreader.sources.base import Page, SiteParse

NAME = "연합뉴스 Yonhap"
HOSTS = frozenset({"www.yna.co.kr", "yna.co.kr", "m.yna.co.kr"})

# The body is the direct <p> children of the story container; reporter cards, photo captions
# and ad slots are other elements inside it.
_BODY = "div.story-news.article"


def matches(page: Page) -> bool:
    return page.host in HOSTS or bool(page.soup.select_one(f"article#articleWrap {_BODY}"))


def parse(page: Page) -> SiteParse:
    soup = page.soup
    body = soup.select_one(_BODY)
    paragraphs = [p.get_text(" ", strip=True) for p in body.find_all("p", recursive=False)] if body else []
    return SiteParse(
        kind=NAME,
        page_type=NAME,
        title_tag=soup.select_one("h1.tit01") or soup.select_one("h1"),
        paragraphs=[text for text in paragraphs if text],
        image_selector=f"{_BODY} figure.image-zone01 img",
        url_hosts=HOSTS,
    )
