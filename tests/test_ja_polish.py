import unittest
from unittest import mock

from newsreader.services import ja_polish, sentences, translation


class StyleTests(unittest.TestCase):
    def test_polite_sentences_take_the_newspaper_style(self):
        cases = {
            "警察は女性を逮捕しました。": "警察は女性を逮捕した。",
            "彼は東京へ行きました。": "彼は東京へ行った。",
            "10時間が過ぎました。": "10時間が過ぎた。",
            "彼は死にました。読みました。急ぎました。言いました。応じました。": "彼は死んだ。読んだ。急いだ。言った。応じた。",
            "状況を注視しています。": "状況を注視している。",
            "政府は対策を講じております。": "政府は対策を講じている。",
            "可能性があります。": "可能性がある。",
            "問題はありませんでした。": "問題はなかった。",
            "誰も来ませんでした。": "誰も来なかった。",
            "二人は客でした。": "二人は客だった。",
            "非常に高いです。": "非常に高い。",
            "それは予定です。": "それは予定だ。",
            "雨が降るでしょう。": "雨が降るだろう。",
        }
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(ja_polish.polish(given), expected)

    def test_quoted_speech_keeps_its_own_style(self):
        self.assertEqual(ja_polish.polish("彼は「私は行きます。本当です」と述べました。"),
                         "彼は「私は行きます。本当です」と述べた。")
        # A quotation that runs across sentences stays untouched to its end.
        self.assertEqual(ja_polish.polish_pieces(["彼は「明日行きます。", "必ず行きます」と話しました。"]),
                         ["彼は「明日行きます。", "必ず行きます」と話した。"])

    def test_typography(self):
        self.assertEqual(ja_polish.polish('BBC の記者は ( 匿名 ) "重大な問題" と述べた。 その後、彼は 去った。'),
                         "BBCの記者は（匿名）「重大な問題」と述べた。その後、彼は去った。")
        self.assertEqual(ja_polish.polish("Ｇ７は２０日"), "G7は20日")
        self.assertEqual(ja_polish.polish("The (UN) said \"no\"."), "The (UN) said \"no\".", "English is left alone")

    def test_without_the_analyser_only_typography_changes(self):
        with mock.patch.object(ja_polish, "_analyser", return_value=None):
            self.assertEqual(ja_polish.polish("逮捕しました。 彼は"), "逮捕しました。彼は")


class TranslationTests(unittest.TestCase):
    def test_sentence_pairing_survives_polishing(self):
        source = "Police arrested her. She admitted it."
        raw = "警察は彼女を逮捕しました。 彼女はそれを認めた。"
        kept = {}
        with mock.patch.object(sentences, "cache_get", side_effect=lambda kind, key: kept.get(key)), \
                mock.patch.object(sentences, "cache_put", side_effect=lambda kind, key, value, **_: kept.__setitem__(key, value)):
            sentences.remember("ja", source, raw, [21, 16], [14, 12])
            text = translation.polished("ja", source, raw)
            self.assertEqual(text, "警察は彼女を逮捕した。彼女はそれを認めた。")
            self.assertEqual(sentences.pairs("ja", source, text),
                             [("Police arrested her.", "警察は彼女を逮捕した。"), ("She admitted it.", "彼女はそれを認めた。")])

    def test_names_the_engine_gets_wrong(self):
        self.assertEqual(translation.polished("ja", "（中央社東京20日綜合外電報導）", "（中央通信、東京、20日）"), "（中央社、東京、20日）")
        self.assertEqual(translation.polished("en", "x", "Police arrested her. "), "Police arrested her. ")


if __name__ == "__main__":
    unittest.main()


class CnaFilingCodeTests(unittest.TestCase):
    def test_filing_code_after_the_editor_is_dropped(self):
        from newsreader.sources import cna
        self.assertEqual(cna._without_filing_code(["本文。", "（編輯：楊惟敬）1151020"]), ["本文。", "（編輯：楊惟敬）"])
        self.assertEqual(cna._without_filing_code(["人口為2345678"]), ["人口為2345678"])
