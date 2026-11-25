"""Line breaks between phrases (newsreader/phrases): every browser breaks Japanese and Chinese between words."""

import unittest

from newsreader.phrases import chunks, mark


class PhraseTests(unittest.TestCase):
    def test_japanese_breaks_between_phrases_only(self):
        self.assertEqual(
            mark("台風が台湾東部に接近、学校と職場が休みに", "ja"),
            "台風が<wbr>台湾東部に<wbr>接近、<wbr>学校と<wbr>職場が<wbr>休みに",
        )

    def test_the_text_picks_the_model_not_the_page(self):
        # a Japanese headline on the English page is cut too; English and Korean are left as they are
        self.assertIn("<wbr>", mark("Rustの2026エディションが公開、コンパイルが高速に", "en-GB"))
        self.assertEqual(mark("Typhoon nears Taiwan", "ja"), "Typhoon nears Taiwan")
        self.assertEqual(mark("태풍 대만 동부 접근", "ko"), "태풍 대만 동부 접근")

    def test_long_phrases_and_korean_joins_get_a_place_to_break(self):
        self.assertEqual(mark("颱風逼近東部 花蓮台東停班停課", "zh-TW"), "颱風逼近東部 花蓮台東<wbr>停班停課")
        self.assertEqual(mark("휴교·휴업", "ja"), "휴교·<wbr>휴업")

    def test_text_is_escaped(self):
        self.assertEqual(mark("<b>台風が接近</b>", "ja").replace("<wbr>", ""), "&lt;b&gt;台風が接近&lt;/b&gt;")

    def test_pieces_for_the_scripts(self):
        self.assertEqual(chunks("台風が台湾東部に接近", "ja"), ["台風が", "台湾東部に", "接近"])
        self.assertEqual(chunks("A & B", "ja"), ["A & B"])


if __name__ == "__main__":
    unittest.main()
