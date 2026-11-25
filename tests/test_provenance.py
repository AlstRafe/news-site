"""情報の出どころ (D-73): datelines, cited organisations and own reporting, read from the text."""

import shutil
import sys
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.library import index, stories
from newsreader.library import provenance as pv
from newsreader.web import create_app

H = {"Host": "localhost"}


def labels(entries):
    return [e["label"] for e in entries]


class DatelineTests(unittest.TestCase):
    def test_cna(self):
        wire = pv.dateline("CNA", "（中央社巴黎19日綜合外電報導）法國爆發多年來最嚴重的街頭暴力衝突後")
        self.assertEqual((wire["kind"], wire["label"]), ("wire", "外電のまとめ（パリ発）"))
        own = pv.dateline("CNA", "（中央社記者 戴雅真 東京19日專電）日本首相")
        self.assertEqual((own["kind"], own["label"]), ("correspondent", "東京の特派員"))
        home = pv.dateline("CNA", "（中央社記者 余曉涵 台北19日電）行政院")
        self.assertEqual((home["kind"], home["label"]), ("staff", "台北の記者"))
        many = pv.dateline("CNA", "（中央社巴黎／米蘭／地拉那18日綜合外電報導）歐洲多地")
        self.assertEqual(many["label"], "外電のまとめ（パリ・ミラノ・ティラナ発）")
        self.assertIsNone(pv.dateline("CNA", "沒有電頭的段落。"))

    def test_yonhap(self):
        abroad = pv.dateline("Yonhap", "(워싱턴=연합뉴스) 백나리 특파원 = 미국 상원 민주당 의원들이")
        self.assertEqual((abroad["kind"], abroad["label"]), ("correspondent", "ワシントンの特派員"))
        home = pv.dateline("Yonhap", "(서울=연합뉴스) 곽민서 기자 = 일론 머스크")
        self.assertEqual((home["kind"], home["label"]), ("staff", "ソウルの記者"))
        unknown = pv.dateline("Yonhap", "(카라치=연합뉴스) 홍길동 특파원 = 파키스탄")
        self.assertEqual(unknown["label"], "카라치の特派員")  # not in the table: kept as written

    def test_bbc_has_none(self):
        self.assertIsNone(pv.dateline("BBC", "(London) Something happened."))


class AnalyzeTests(unittest.TestCase):
    def test_english(self):
        info = pv.analyze(
            "BBC",
            [
                "The ministry told news agency Reuters that 20 people died.",
                "According to the BBC's US partner CBS News, the plan was dropped.",
                "Trump blocked CNN and Politico from the White House.",
                "A spokesman told the BBC the talks would resume. BBC Verify has checked the footage.",
                "The Guardian Council rejected the bill.",
            ],
        )
        self.assertEqual(labels(info["cites"]), ["ロイター", "CBS"])
        self.assertEqual(labels(info["named"]), ["CNN", "ポリティコ"])  # named without a reporting verb
        self.assertEqual(info["own"], ["独自インタビュー", "BBC Verify の検証"])
        self.assertIsNone(info["dateline"])
        self.assertNotIn("ガーディアン", labels(info["cites"] + info["named"]))

    def test_chinese(self):
        info = pv.analyze(
            "CNA",
            [
                "（中央社巴黎19日綜合外電報導）法國爆發衝突。",
                "法新社報導，總理辦公室說，4座城市實施宵禁。",
                "英國廣播公司（BBC）稍早報導，示威演變成暴力事件。",
                "「華盛頓郵報」（The Washington Post）報導，川普將出席。",
                "川普禁止CNN等3家媒體進入白宮。",
                "部長接受中央社訪問時表示，將持續關注。",
                "在日本西村朝日法律事務所任職的分析師說。",
            ],
        )
        self.assertEqual(info["dateline"]["kind"], "wire")
        self.assertEqual(labels(info["cites"]), ["AFP通信", "BBC", "ワシントン・ポスト"])
        self.assertEqual(labels(info["named"]), ["CNN"])
        self.assertEqual(info["own"], ["独自取材"])
        self.assertNotIn("朝日新聞", labels(info["cites"] + info["named"]))
        self.assertNotIn("中央社", labels(info["cites"] + info["named"]))  # itself

    def test_korean(self):
        info = pv.analyze(
            "Yonhap",
            [
                "(도쿄=연합뉴스) 조성미 특파원 = 일본 정부가",
                "19일 교도통신에 따르면 정부는 법안을 제출했다.",
                '로이터통신은 장관이 최근 그리스를 방문했다면서 "패턴에 들어맞는다"고 꼬집었다.',
                "관련 규정을 공개했다고 블룸버그·AP 통신이 보도했다.",
                '한 공화당 상원의원은 더힐에 "너무 지나친 일"이라고 말했다.',
                "트럼프 대통령은 폭스뉴스를 비판했다.",
                "그는 교도소에 수감됐다.",
                "nari@yna.co.kr 연합뉴스",
            ],
        )
        self.assertEqual(info["dateline"]["label"], "東京の特派員")
        self.assertEqual(labels(info["cites"]), ["共同通信", "ロイター", "ブルームバーグ", "AP通信", "ザ・ヒル"])
        self.assertEqual(labels(info["named"]), ["FOXニュース"])
        self.assertEqual(pv.summary_line(info), "東京の特派員 · 出典: 共同通信・ロイター・ブルームバーグ ほか2")

    def test_triggers_cover_every_alternative(self):
        import re

        for org, (key, triggers, _) in zip(pv.ORGS, pv._SCAN):
            alternatives = re.split(r"\|(?![^(]*\))", org.pattern)
            self.assertEqual(len(alternatives), len(triggers), key)


class GroupTests(unittest.TestCase):
    def test_compare(self):
        rows = [
            {
                "id": 1,
                "source": "CNA",
                "info": pv.analyze(
                    "CNA",
                    ["（中央社巴黎19日綜合外電報導）衝突。", "法新社報導，宵禁。英國廣播公司（BBC）稍早報導，衝突。"],
                ),
            },
            {
                "id": 2,
                "source": "Yonhap",
                "info": pv.analyze(
                    "Yonhap", ["(파리=연합뉴스) 김 특파원 = 프랑스", "AFP통신에 따르면 통금이 시행됐다."]
                ),
            },
            {"id": 3, "source": "BBC", "info": pv.analyze("BBC", ["Riots spread, AFP news agency reported."])},
        ]
        notes = pv.compare(rows)
        self.assertEqual(notes["shared"], [{"label": "AFP通信", "outlets": ["CNA", "Yonhap", "BBC"]}])
        self.assertEqual(notes["internal"], [{"from": "CNA", "to": "BBC", "id": 1}])

    def test_outlet_summary_groups_places(self):
        self.assertEqual(pv._kind_label("wire", ["キーウ", "モスクワ"]), "外電のまとめ（キーウ・モスクワ発）")
        self.assertEqual(pv._kind_label("correspondent", list("ABCDEF")), "特派員（A・B・C・D ほか2）")


class PageTests(unittest.TestCase):
    def setUp(self):
        # Published two days ago, whenever the tests run: 媒体くらべ only counts the last days.
        day = datetime.now(UTC) - timedelta(days=2)
        bbc_time, cna_time = (f"{(day + timedelta(hours=h)):%Y-%m-%dT%H:%M:%SZ}" for h in (0, 2))
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        for site in ("BBC", "CNA"):
            (self.root / site).mkdir(parents=True)
        (self.root / "BBC" / "a.html").write_bytes(
            f"""<html><head><meta name="pagetype" content="News Article">
<meta property="article:published_time" content="{bbc_time}"></head><body>
<main id="bbc-main"><h1>Paris curfew as France riots spread</h1><article>
<div data-component="layout-block"><p class="Paragraph-styles__ParagraphStyled-x">Officials told the BBC a curfew begins tonight, AFP news agency reported.</p></div>
</article></main></body></html>""".encode()
        )
        (self.root / "CNA" / "z.html").write_bytes(
            f"""<html><head><link rel="canonical" href="https://www.cna.com.tw/news/aopl/x.aspx">
<meta property="article:published_time" content="{cna_time}"></head><body>
<article class="article"><h1>法國暴動擴大 巴黎宵禁</h1><div class="centralContent">
<div class="paragraph"><p>（中央社巴黎19日綜合外電報導）法國暴動擴大。</p><p>法新社報導，巴黎實施宵禁。英國廣播公司（BBC）稍早報導，衝突持續。</p></div></div></article></body></html>""".encode()
        )
        self.db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(self.root, self.db, force=True)
        stories.refresh(
            self.db,
            translate=lambda titles: ["Paris curfew as France riots spread" for _ in titles],
            status_path=self.tmp / "status.json",
        )
        self.client = create_app(self.root, self.db, start_background=False, secret_key=b"p" * 32).test_client()
        self.cna = index.find_by_file(self.db, "CNA", "z.html")["id"]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_article_page(self):
        page = self.client.get(f"/a/{self.cna}", headers=H).get_data(as_text=True)
        self.assertIn('<span class="prov-item">外電のまとめ（パリ発）</span>', page)
        self.assertIn("出典: AFP通信・BBC", page)
        group = stories.related(self.db, self.cna)
        self.assertIsNotNone(group)
        self.assertIn('class="prov-item prov-link"', page)  # the BBC article of the same group

    def test_story_page(self):
        key = stories.related(self.db, self.cna)["key"]
        page = self.client.get(f"/stories/{key}", headers=H).get_data(as_text=True)
        self.assertIn("情報の出どころ", page.replace("<wbr>", ""))
        self.assertIn("AFP通信を BBC・CNA がどちらも出典にしています", page.replace("CNA・BBC", "BBC・CNA"))
        self.assertIn("CNA の記事は、このグループの BBC の報道を引用しています。", page)
        self.assertIn('<p class="story-prov">外電のまとめ（パリ発） · 出典: AFP通信・BBC</p>', page)

    def test_outlets_page(self):
        page = self.client.get("/outlets?days=30", headers=H).get_data(as_text=True)
        self.assertIn("取材のかたち", page)
        self.assertIn("外電のまとめ <strong>1</strong>", page)
        self.assertIn("独自取材の跡", page)  # told the BBC


if __name__ == "__main__":
    unittest.main()
