import unittest

from newsreader.library import genres


class GenreTests(unittest.TestCase):
    def test_the_outlets_own_section_decides_when_it_names_a_kind(self):
        self.assertEqual(genres.classify("BBC", "https://www.bbc.com/news/articles/x", "Technology", "Anything", []), "tech")
        self.assertEqual(genres.classify("Yonhap", "", "경제", "아무거나", []), "economy")
        self.assertEqual(genres.classify("CNA", "https://www.cna.com.tw/news/ait/202610220412.aspx", "", "標題", []), "tech")
        self.assertEqual(genres.classify("NAVER NEWS", "", "101", "제목", []), "economy")
        # A region is not a kind: the words decide.
        self.assertEqual(genres.classify("BBC", "", "US & Canada", "Fed holds interest rates as inflation cools", []), "economy")

    def test_words_in_each_language(self):
        cases = [
            ("Nvidia unveils new AI chip for data centres", "tech"),
            ("Hospital wards fill as flu outbreak spreads", "health"),
            ("Ceasefire talks resume as missiles hit Kyiv", "politics"),
            ("Fed主席華許表態後　10月維持利率不變預期升溫", "economy"),
            ("中國駭客網攻斐濟區域組織　美使館示警太平洋電信網路恐遭鎖定", "tech"),
            ("男子在日本大阪地院前遇襲重傷　犯嫌自首被捕", "society"),
            ("美 주간 신규 실업수당 청구 19만4천건…3개월만에 최저", "economy"),
            ("모더나·머크 암백신, 흑색종 재발·사망 42%↓…3상 공개", "health"),
        ]
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(genres.classify("", "", "", title, []), expected)

    def test_too_little_evidence_gives_no_kind(self):
        self.assertEqual(genres.classify("", "", "", "Obama says drift is not inevitable", ["He spoke on Tuesday."]), "")
        # Body words alone need to add up.
        self.assertEqual(genres.from_words("A quiet day", ["The hospital said.", "Nothing else."]), "")

    def test_section_is_read_from_the_page(self):
        self.assertEqual(genres.section_of(b'<meta data-rh="true" property="article:section" content="Science &amp; Environment"/>'),
                         "Science & Environment")
        self.assertEqual(genres.section_of(b'<meta content="\xea\xb2\xbd\xec\xa0\x9c" property="article:section"/>'), "경제")
        self.assertEqual(genres.section_of(b'<meta name="napp-site-analysis" content="gdid=1,service=news,section=105">'), "105")
        self.assertEqual(genres.section_of(b"<html></html>"), "")

    def test_menu_lists_every_kind_and_the_rest(self):
        menu = genres.menu({"economy": 4, "": 2})
        self.assertEqual([m["key"] for m in menu][-1], genres.OTHER)
        self.assertEqual({m["key"]: m["count"] for m in menu}["economy"], 4)
        self.assertEqual(menu[-1]["count"], 2)
