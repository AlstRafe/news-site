"""Site adapters: contract, extensibility and equivalence with the frozen v3 extractor.

The real-data part runs only when NEWS_REALDATA points at an ``articles`` folder
(``<root>/<SOURCE>/*.html``); it is read-only. Everything else uses fixtures.
"""

import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from oracle import v3_extractor
from oracle.intended import intended_input
from test_extractor import CNA, NAVER, NORMAL, VIDEO

from newsreader import sources
from newsreader.sources import Page, ReaderError, SiteParse, extract_article

EDGE_CASES = {
    "empty": b"",
    "bare": b"<html></html>",
    "binary": bytes(range(256)),
    "naver-empty-body": b'<article id="dic_area"></article>',
    "cna-outer-h1": b'<h1>outer</h1><article class="article"><div class="centralContent">'
    b'<div class="paragraph"><p>x</p></div></div></article>',
    "bbc-and-naver": b'<meta name="pagetype" content="News Article"><article id="dic_area">n</article>'
    b'<article><div data-component="layout-block"><p class="ParagraphStyled">b</p></div></article>',
    "naver-and-cna": b'<article id="dic_area">n</article><article class="article"><div class="centralContent">'
    b'<div class="paragraph"><p>c</p></div></div></article>',
    "host-only-cna": b'<link rel="canonical" href="https://www.cna.com.tw/news/x.aspx"><p>x</p>',
    "bbc-videos-path": b'<link rel="canonical" href="https://www.bbc.com/news/videos/abc"><p>x</p>',
    "kst-time": b'<article id="dic_area">x</article><span class="_ARTICLE_DATE_TIME" data-date-time="2026-10-11 15:53:12"></span>',
    "nine-images": b'<article class="article"><div class="centralContent"><div class="paragraph"><p>x</p></div>'
    + b"".join(b'<img src="https://imgcdn.cna.com.tw/%d.jpg">' % i for i in range(9))
    + b"</div></article>",
    "srcset": b'<meta name="pagetype" content="News Article"><main id="bbc-main"><h1>t</h1><article>'
    b'<div data-component="layout-block"><p class="ParagraphStyled">b</p></div>'
    b'<figure><img srcset="a.jpg 2000w, b.jpg 3000w"></figure></article></main>',
}
MUTATIONS = {
    "no-canonical": lambda h: re.sub(rb'<link[^>]+rel="canonical"[^>]*>', b"", h),
    "no-h1": lambda h: re.sub(rb"<h1\b[^>]*>.*?</h1>", b"", h, flags=re.S),
    "no-img": lambda h: re.sub(rb"<img\b[^>]*>", b"", h),
    "half": lambda h: h[: len(h) // 2],
}


def outcome(fn, data):
    try:
        return ("ok", fn(data))
    except Exception as exc:
        return ("error", type(exc).__name__, str(exc))


class EquivalenceTests(unittest.TestCase):
    def assertSameAsV3(self, data, label):
        # Equal to v3 up to the documented intended differences (tests/oracle/intended.py).
        self.assertEqual(
            outcome(extract_article, data), outcome(v3_extractor.extract_article, intended_input(data)), label
        )

    def test_template_images_are_the_only_intended_difference(self):
        page = (
            b'<meta name="pagetype" content="News Article"><main id="bbc-main"><h1>T</h1><article>'
            b'<div data-component="layout-block"><p class="ParagraphStyled">body</p></div>'
            b'<template><figure><img src="https://ichef.bbci.co.uk/images/ic/1920x1080/x.jpg"></figure></template>'
            b'<figure><img src="https://ichef.bbci.co.uk/news/1024/real.jpg" alt="real"></figure></article></main>'
        )
        new, old = extract_article(page), v3_extractor.extract_article(page)
        self.assertEqual([i["src"] for i in new["images"]], ["https://ichef.bbci.co.uk/news/1024/real.jpg"])
        self.assertEqual(len(old["images"]), 2)  # v3 also took the never-rendered placeholder
        self.assertEqual(new, v3_extractor.extract_article(intended_input(page)))

    def test_fixtures_and_edge_cases(self):
        corpus = {"VIDEO": VIDEO, "NORMAL": NORMAL, "NAVER": NAVER, "CNA": CNA, **EDGE_CASES}
        for name, data in corpus.items():
            self.assertSameAsV3(data, name)
            for label, mutate in MUTATIONS.items():
                self.assertSameAsV3(mutate(data), f"{name}/{label}")

    @unittest.skipUnless(os.environ.get("NEWS_REALDATA"), "NEWS_REALDATA not set")
    def test_real_articles(self):
        files = sorted(Path(os.environ["NEWS_REALDATA"]).glob("*/*.html"))
        self.assertTrue(files, "NEWS_REALDATA has no <SOURCE>/*.html files")
        for path in files:
            self.assertSameAsV3(path.read_bytes(), f"{path.parent.name}/{path.name}")


class AdapterContractTests(unittest.TestCase):
    def test_every_site_has_the_adapter_interface(self):
        self.assertEqual(
            [site.NAME for site in sources.SITES],
            ["BBC", "NAVER NEWS", "中央社 CNA", "연합뉴스 Yonhap", "InfoQ", "The New Stack", "iThome", "ZDNet Korea"],
        )
        for site in sources.SITES:
            self.assertTrue(callable(site.matches) and callable(site.parse), site.NAME)
            self.assertIsInstance(site.HOSTS, frozenset)

    def test_detection_precedence_is_bbc_then_naver_then_cna(self):
        self.assertIs(sources.detect(Page.from_html(EDGE_CASES["bbc-and-naver"])), sources.bbc)
        self.assertIs(sources.detect(Page.from_html(EDGE_CASES["naver-and-cna"])), sources.naver)
        self.assertIsNone(sources.detect(Page.from_html(b"<p>plain</p>")))

    def test_a_new_site_is_one_module_appended_to_sites(self):
        class Example:
            NAME = "Example"
            HOSTS = frozenset({"news.example"})

            @staticmethod
            def matches(page):
                return page.host in Example.HOSTS

            @staticmethod
            def parse(page):
                return SiteParse(
                    kind="Example",
                    page_type="Example",
                    title_tag=page.soup.select_one("h1"),
                    paragraphs=[p.get_text(strip=True) for p in page.soup.select("p")],
                    image_selector="img",
                    url_hosts=Example.HOSTS,
                )

        html = b'<link rel="canonical" href="https://news.example/a"><h1>T</h1><p>one</p><p>two</p>'
        with self.assertRaises(ReaderError):
            extract_article(html)
        saved = sources.SITES
        sources.SITES = saved + (Example,)
        try:
            article = extract_article(html)
        finally:
            sources.SITES = saved
        self.assertEqual(
            (article["title"], article["paragraphs"], article["url"]), ("T", ["one", "two"], "https://news.example/a")
        )

    def test_canonical_on_a_foreign_host_is_not_trusted_as_the_article_link(self):
        html = NAVER.replace(b"https://n.news.naver.com/article", b"https://evil.example/article")
        self.assertEqual(extract_article(html)["url"], "")


if __name__ == "__main__":
    unittest.main()
