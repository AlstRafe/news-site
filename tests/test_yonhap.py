"""연합뉴스 Yonhap pages (the Korean source that replaced NAVER collection)."""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.services.images import allowed_image_url
from newsreader.sources import extract_article

YONHAP = '''<html><head>
<link rel="canonical" id="canonical" href="https://www.yna.co.kr/view/AKR20261019174900007">
<meta property="og:title" content="[전국체전] 2관왕 오른 황선우 | 연합뉴스">
<meta property="article:published_time" content="2026-10-19T20:10:22+09:00">
<meta property="og:image" content="https://img1.yna.co.kr/photo/yna/YH/2026/10/19/PYH2026101909630001300_P4.jpg">
</head><body><article id="articleWrap" class="article-wrap01">
<h1 class="tit01">[전국체전] 2관왕 오른 황선우 "20일 6관왕 도전 고비…온 힘 기울일 것"</h1>
<div class="story-news article">
<div id="newsWriterCarousel01" class="writer-zone01"><figure class="img-con01"><img src="https://img6.yna.co.kr/reporter/38333339393837.jpg" alt="김경윤"></figure>
<div class="txt-con"><strong class="tit-name">김경윤기자</strong></div></div>
<div class="comp-box photo-group"><figure class="image-zone01"><div class="img-con01"><span class="img">
<img src="//img1.yna.co.kr/photo/yna/YH/2026/10/19/PYH2026101909630001300_P4.jpg" alt="역영하는 황선우"></span></div>
<figcaption class="desc-con"><strong class="tit-cap">역영하는 황선우</strong><p class="txt-desc">(제주=연합뉴스) 류영석 기자 = 사진 설명</p></figcaption></figure></div>
<p> (제주=연합뉴스) 김경윤 기자 = 한국 수영의 에이스 황선우는 다부진 각오를 밝혔다.</p>
<p> 황선우는 19일 자유형 200ｍ 결승에서 우승했다.</p>
<aside class="aside-box004"><div class="area01 ads-article01"><p>광고</p></div></aside><p> 그는 20일 2개 종목에 출전한다.</p>
<div class="comp-box photo-group"><figure class="image-zone01"><div class="img-con01"><span class="img">
<img src="//img4.yna.co.kr/photo/yna/YH/2026/10/19/PYH2026101909510001300_P4.jpg" alt="개인종목 출전하는 황선우"></span></div></figure></div>
<p> 관건은 체력이다. </p>
</div></article></body></html>'''.encode("utf-8")


class YonhapTests(unittest.TestCase):
    def test_body_title_images_time_and_url(self):
        article = extract_article(YONHAP)
        self.assertEqual(article["kind"], "연합뉴스 Yonhap")
        self.assertTrue(article["title"].startswith("[전국체전] 2관왕 오른 황선우"))
        self.assertEqual(article["paragraphs"], [
            "(제주=연합뉴스) 김경윤 기자 = 한국 수영의 에이스 황선우는 다부진 각오를 밝혔다.",
            "황선우는 19일 자유형 200ｍ 결승에서 우승했다.",
            "그는 20일 2개 종목에 출전한다.",
            "관건은 체력이다.",
        ])  # captions, reporter card and ad slots are not body text
        self.assertEqual([image["src"] for image in article["images"]], [
            "https://img1.yna.co.kr/photo/yna/YH/2026/10/19/PYH2026101909630001300_P4.jpg",
            "https://img4.yna.co.kr/photo/yna/YH/2026/10/19/PYH2026101909510001300_P4.jpg",
        ])  # not the reporter's portrait
        self.assertEqual(article["published"], "2026-10-19T11:10:22Z")
        self.assertEqual(article["url"], "https://www.yna.co.kr/view/AKR20261019174900007")
        self.assertTrue(all(allowed_image_url(image["src"]) for image in article["images"]))
        self.assertFalse(allowed_image_url("https://evil.example/yna.co.kr.jpg"))


if __name__ == "__main__":
    unittest.main()
