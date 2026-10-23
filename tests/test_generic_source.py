import json
import unittest

from newsreader.collect import autofetch
from newsreader.library import genres
from newsreader.sources import extract_article, generic
from newsreader.sources.base import Page

BODY = [f"Paragraph {n} of the story explains how the new routing protocol keeps packets moving." for n in range(6)]


def page(body_html: str, url: str = "https://thenewstack.io/a-long-article-slug", extra_head: str = "") -> bytes:
    return (f'<html><head><link rel="canonical" href="{url}"><meta property="og:image" content="https://thenewstack.io/i.jpg">'
            f'{extra_head}</head><body><header><p>Site navigation text that is long enough to count</p></header>'
            f'<main><article><h1>Routing at scale</h1>{body_html}</article></main>'
            '<aside><p>Related: another story about something else entirely</p></aside>'
            '<footer><p>Copyright 2026 The Example Company. All rights reserved.</p></footer></body></html>').encode()


class GenericExtractionTests(unittest.TestCase):
    def test_paragraphs_split_into_one_block_each_are_found_whole(self):
        blocks = "".join(f'<div class="block"><div><p>{p}</p></div></div>' for p in BODY)
        blocks = blocks.replace("</div></div><div", '</div></div><a href="/x"><p>Promo link headline that is long enough</p></a><div', 1)
        article = extract_article(page(f'<div class="body">{blocks}</div>'))
        self.assertEqual(article["paragraphs"], BODY)
        self.assertEqual(article["title"], "Routing at scale")
        self.assertEqual(article["url"], "https://thenewstack.io/a-long-article-slug")
        self.assertEqual(article["images"][0]["src"], "https://thenewstack.io/i.jpg")

    def test_text_broken_by_br_is_split_into_paragraphs(self):
        text = "<br><br>".join(BODY)
        html = page(f'<div id="articleBody">{text}</div>', url="https://zdnet.co.kr/view/?no=20261023073816")
        self.assertEqual(extract_article(html)["paragraphs"], BODY)

    def test_json_ld_body_when_the_page_gives_far_less(self):
        ld = json.dumps({"@type": "NewsArticle", "articleBody": "\n\n".join(BODY)})
        html = page("<p>Short teaser only, the rest is loaded later.</p>",
                    extra_head=f'<script type="application/ld+json">{ld}</script>')
        self.assertEqual(extract_article(html)["paragraphs"], BODY)


class TechOutletTests(unittest.TestCase):
    def test_kind_defaults_to_the_outlet_and_follows_strong_words(self):
        self.assertEqual(genres.classify("InfoQ", "", "", "Designing event-driven systems", []), "dev")
        self.assertEqual(genres.classify("iThome", "", "", "新品發表會", []), "tech")
        self.assertEqual(genres.classify("iThome", "", "", "甲骨文緊急修補Java重大漏洞", []), "dev")
        self.assertEqual(genres.classify("ZDNet Korea", "", "", "SK하이닉스 3분기 실적 매출 사상 최대", []), "economy")

    def test_zdnet_addresses_keep_their_article_number(self):
        source = next(s for s in autofetch.SOURCES if s["name"] == "ZDNet Korea")
        url = autofetch._normalize_url("https://zdnet.co.kr/view/?no=20261022155356&utm_source=rss#top", source["keep_query"])
        self.assertEqual(url, "https://zdnet.co.kr/view?no=20261022155356")
        self.assertTrue(source["is_article"](url))
        self.assertIn("20261022155356", autofetch._filename_for(source, url))
        self.assertEqual(autofetch._normalize_url("https://www.bbc.com/news/x/?at=1"), "https://www.bbc.com/news/x")

    def test_article_addresses_of_each_outlet(self):
        check = {s["name"]: s["is_article"] for s in autofetch.SOURCES}
        self.assertTrue(check["InfoQ"]("https://www.infoq.com/news/2026/10/vercel-zig-fx-agent"))
        self.assertFalse(check["InfoQ"]("https://www.infoq.com/presentations/actors-for-production-ai-agents"))
        self.assertTrue(check["The New Stack"]("https://thenewstack.io/ai-agent-trust-score"))
        self.assertFalse(check["The New Stack"]("https://thenewstack.io/category/kubernetes"))
        self.assertTrue(check["iThome"]("https://www.ithome.com.tw/news/179905"))


if __name__ == "__main__":
    unittest.main()


class RobotsTests(unittest.TestCase):
    def tearDown(self):
        autofetch._robots_cache.clear()

    def test_unavailable_robots_sets_no_rules_but_refusal_and_errors_disallow(self):
        from unittest import mock
        from urllib.error import HTTPError
        for code, allowed in ((404, True), (406, True), (410, True), (401, False), (403, False), (429, False), (503, False)):
            autofetch._robots_cache.clear()

            def refuse(request, timeout=None, code=code):
                raise HTTPError(request.full_url, code, "x", {}, None)
            with self.subTest(code=code), mock.patch.object(autofetch, "urlopen", refuse):
                self.assertEqual(autofetch._allowed_by_robots("https://feed.example.com/rss"), allowed)


class SkipTitleTests(unittest.TestCase):
    def test_personnel_notices_are_not_saved(self):
        import tempfile
        from pathlib import Path
        from unittest import mock
        source = next(s for s in autofetch.SOURCES if s["name"] == "ZDNet Korea")
        url = "https://zdnet.co.kr/view?no=20261023112503"
        html = page("".join(f"<p>{p}</p>" for p in BODY), url="https://zdnet.co.kr/view/?no=20261023112503")
        notice = html.replace(b"Routing at scale", "[인사] 신세계그룹".encode())
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.object(autofetch, "_download", side_effect=[(notice, url, "text/html"), (html, url, "text/html")]):
            self.assertEqual(autofetch._fetch_article(source, url, Path(tmp)), "unreadable")
            self.assertEqual(autofetch._fetch_article(source, url, Path(tmp)), url)
            saved = list(Path(tmp).iterdir())
            self.assertEqual(len(saved), 1)
            self.assertEqual(saved[0].read_bytes(), html, "technology outlets are kept whole, not slimmed")
