"""BBC News: regular articles and video pages (video pages give their description text only)."""
from __future__ import annotations

from urllib.parse import urlsplit

from newsreader.sources.base import Page, SiteParse

NAME = "BBC"
HOSTS = frozenset({"www.bbc.com", "www.bbc.co.uk"})

KIND_VIDEO = "動画記事（説明文）"
KIND_ARTICLE = "通常記事"

_VIDEO_SECTION = '[data-testid="video-page-video-section"]'
_LAYOUT_BLOCK = 'article [data-component="layout-block"]'
# Recommendation blocks repeat this line after the description; it is not article text.
_TRAILER = "More on this story."


def _page_type(page: Page) -> str:
    meta = page.soup.select_one('meta[name="pagetype"]')
    return str(meta.get("content", "")) if meta else ""


def _is_video(page: Page, page_type: str) -> bool:
    return (
        page_type == "Video Item"
        or bool(page.soup.select_one(_VIDEO_SECTION))
        or (page.host in HOSTS and "/news/videos/" in urlsplit(page.url).path)
    )


def _is_article(page: Page, page_type: str) -> bool:
    return (
        page_type == "News Article"
        or bool(page.soup.select_one(_LAYOUT_BLOCK))
        or (page.host in HOSTS and bool(page.soup.select_one("#main-content article")))
    )


def matches(page: Page) -> bool:
    page_type = _page_type(page)
    return _is_video(page, page_type) or _is_article(page, page_type)


def parse(page: Page) -> SiteParse:
    soup = page.soup
    page_type = _page_type(page)
    if _is_video(page, page_type):
        kind = KIND_VIDEO
        selector = f'{_VIDEO_SECTION} div[class*="TextStyled"] > p[class*="ParagraphStyled"]'
        image_selector = f'{_VIDEO_SECTION} img, #main-content article [data-testid="image"] img'
    elif soup.select_one(_LAYOUT_BLOCK):
        # Current BBC layout (2024-): paragraphs live in layout blocks under <article>.
        kind = KIND_ARTICLE
        selector = f'{_LAYOUT_BLOCK} p[class*="ParagraphStyled"]'
        image_selector = '#bbc-main [data-component*="image"] img, #bbc-main figure img'
    else:
        # Older layout: rich-text blocks under #main-content.
        kind = KIND_ARTICLE
        selector = '#main-content article [data-testid="rich-text"] p[class*="Paragraph"]'
        image_selector = '#main-content article [data-testid="image"] img'
    # ``or`` (truthiness), not ``is None``: kept identical to v3.
    title_tag = soup.select_one("#bbc-main h1") or soup.select_one("#main-content h1")
    paragraphs = [p.get_text(" ", strip=True) for p in soup.select(selector)]
    paragraphs = [p for p in paragraphs if p and p != _TRAILER]
    return SiteParse(
        kind=kind,
        page_type=page_type,
        title_tag=title_tag,
        paragraphs=paragraphs,
        image_selector=image_selector,
        url_hosts=HOSTS,
    )
