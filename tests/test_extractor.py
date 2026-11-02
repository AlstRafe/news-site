import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.library.files import list_html_files, resolve_html_file, validate_directory
from newsreader.sources import ReaderError, extract_article

VIDEO = b"""<html><head><meta name="pagetype" content="Video Item"></head><body>
<main id="bbc-main"><h1>Video Headline</h1>
<div data-testid="video-page-video-section"><div class="VideoDetailsPage-styles__TextStyled-test">
<p class="Paragraph-styles__ParagraphStyled-test">First video paragraph.</p>
<p class="Paragraph-styles__ParagraphStyled-test">Second video paragraph.</p>
<p class="Paragraph-styles__ParagraphStyled-test">More on this story.</p></div></div>
<p>Related content that must NOT appear</p></main></body></html>"""
NORMAL = b"""<html><head><meta name="pagetype" content="News Article"></head><body>
<main id="bbc-main"><h1>News Headline</h1><article>
<div data-component="layout-block"><p class="Paragraph-styles__ParagraphStyled-x">News paragraph one.</p></div>
<div data-component="layout-block"><p class="Paragraph-styles__ParagraphStyled-x">News paragraph two.</p></div>
</article><p>Recommendation that must NOT appear</p></main></body></html>"""
NAVER = b"""<html><head><meta property="og:url" content="https://n.news.naver.com/article/449/0000363671"></head><body>
<h2 id="title_area">NAVER headline</h2><div id="newsct_article"><article id="dic_area">
First Naver paragraph.<br><br>Second Naver paragraph.
</article><p>Recommendation that must NOT appear</p></div></body></html>"""
CNA = b"""<html><head><link rel="canonical" href="https://www.cna.com.tw/news/aopl/example.aspx"></head><body>
<article class="article"><h1>CNA headline</h1><div class="centralContent">
<div class="paragraph"><p>First CNA paragraph.</p></div><div class="paragraph"><p>Second CNA paragraph.</p></div>
<div class="paragraph moreArticle"><p>Related story that must NOT appear.</p></div>
<div class="paragraph appDownload"><p>Site notice that must NOT appear.</p></div>
</div></article></body></html>"""


class ExtractorTests(unittest.TestCase):
    def test_video(self):
        data = extract_article(VIDEO)
        self.assertEqual(data["kind"], "動画記事（説明文）")
        self.assertEqual(data["paragraphs"], ["First video paragraph.", "Second video paragraph."])

    def test_article(self):
        data = extract_article(NORMAL)
        self.assertEqual(data["title"], "News Headline")
        self.assertEqual(data["paragraph_count"], 2)
        self.assertNotIn("Recommendation", data["body"])

    def test_naver(self):
        data = extract_article(NAVER)
        self.assertEqual(data["kind"], "NAVER NEWS")
        self.assertEqual(data["title"], "NAVER headline")
        self.assertEqual(data["paragraphs"], ["First Naver paragraph.", "Second Naver paragraph."])
        self.assertNotIn("Recommendation", data["body"])

    def test_cna(self):
        data = extract_article(CNA)
        self.assertEqual(data["kind"], "中央社 CNA")
        self.assertEqual(data["title"], "CNA headline")
        self.assertEqual(data["paragraphs"], ["First CNA paragraph.", "Second CNA paragraph."])
        self.assertNotIn("Related story", data["body"])
        self.assertNotIn("Site notice", data["body"])

    def test_unsupported(self):
        with self.assertRaises(ReaderError):
            extract_article(b"<html><title>Unsupported</title></html>")

    def test_directory_listing_and_path_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = validate_directory(tmp)
            (folder / "page.html").write_bytes(VIDEO)
            (folder / "ignore.txt").write_text("ignore")
            files = list_html_files(folder)
            self.assertEqual([item["name"] for item in files], ["page.html"])
            self.assertEqual(resolve_html_file(folder, "page.html").name, "page.html")
            for invalid in ("../outside.html", "..\\outside.html", "ignore.txt"):
                if invalid == "..\\outside.html" and sys.platform != "win32":
                    continue
                with self.assertRaises(ReaderError):
                    resolve_html_file(folder, invalid)


if __name__ == "__main__":
    unittest.main()
