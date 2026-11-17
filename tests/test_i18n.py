"""The site in Korean, British English and Traditional Chinese (newsreader/i18n.py).

1. Every text has its translation in each language (newsreader/locale/<lang>.json): the templates'
   _('…'), the Python code's texts (i18n.text, the library's _say), the messages the pages and the
   API show (errors), the library's labels shown as they are kept (genres, kinds of figures, news
   organisations, places, …) and the scripts' T('…').
2. A translation keeps the {placeholders} of its Japanese, has no kana left and is not empty; no
   translation is kept for a text that is no longer used.
3. A page asked for in a language (?lang=, the cookie, the browser) comes in that language, with no
   kana outside the news itself.

Run ``python tests/test_i18n.py --missing`` to print the texts still without a translation.
"""

from __future__ import annotations

import ast
import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
PACKAGE = ROOT / "newsreader"

from newsreader import i18n

JP = re.compile(r"[぀-ヿ㐀-鿿]")
KANA = re.compile(r"[ぁ-ゖァ-ヺ]")
HOLE = re.compile(r"\{(\w+)\}")
OTHERS = ("ko", "en-GB", "zh-TW")
#: The owner's own pages (this PC only): kept in Japanese.
OWNER_TEMPLATES = ("settings.html", "status.html")
#: Python files whose messages reach a page or a script (errors shown as they are).
MESSAGE_FILES = (
    "web/*.py",
    "services/translation.py",
    "services/relay.py",
    "services/settings.py",
    "sources/base.py",
    "library/files.py",
)


def _strings(node) -> list[str]:
    """The Japanese string constants in an expression ("a" if x else "b", {"k": "a"}[x] …); an f-string's
    pieces are not texts (it is said in Japanese)."""
    inside = {id(part) for n in ast.walk(node) if isinstance(n, ast.JoinedStr) for part in ast.walk(n)}
    return [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and JP.search(n.value) and id(n) not in inside
    ]


def _calls_to(tree, names: tuple[str, ...]) -> list[str]:
    """First arguments of calls to these functions (text(…), i18n.text(…), _say(…))."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
            whole = ast.unparse(func)
            if name in names and (isinstance(func, ast.Name) or whole in ("i18n.text", "i18n.markup")):
                out += _strings(node.args[0])
    return out


def _messages(tree) -> list[str]:
    """Messages to show: what errors are raised with, ``message = "…"``, {"error": "…"}, *_MESSAGE(S) and _HEADINGS."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and node.args:
            name = ast.unparse(node.func)
            if name.endswith("Error") and name != "RuntimeError":
                out += _strings(node.args[0])
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [ast.unparse(t) for t in targets]
            if node.value is not None and any(re.search(r"message|MESSAGE|_HEADINGS|^error$", n) for n in names):
                out += _strings(node.value)
        elif isinstance(node, ast.Dict):
            for key, value in zip(node.keys, node.values):
                if isinstance(key, ast.Constant) and key.value == "error" and value is not None:
                    out += _strings(value)
    return out


def template_texts() -> set[str]:
    call = re.compile(r"""\b_m?\(\s*('(?:\\.|[^'\\])*'|"(?:\\.|[^"\\])*")""")
    out = set()
    for path in sorted((PACKAGE / "templates").glob("*.html")):
        if path.name not in OWNER_TEMPLATES:
            out |= {ast.literal_eval(lit) for lit in call.findall(path.read_text(encoding="utf-8"))}
    return out


def code_texts() -> set[str]:
    out = set()
    for path in sorted(PACKAGE.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        out |= set(_calls_to(tree, ("text", "_say", "markup")))
    for pattern in MESSAGE_FILES:
        for path in sorted(PACKAGE.glob(pattern)):
            out |= set(_messages(ast.parse(path.read_text(encoding="utf-8"))))
    return out


def data_texts() -> set[str]:
    """What the library keeps in Japanese and the pages show through _() / |tr / |outlet."""
    from newsreader import branding
    from newsreader.library import compare, genres, provenance

    out = {"未分類", "その他", branding.APP_TAGLINE}
    out |= set(genres.LABELS.values()) | set(compare.LABELS.values())
    out |= (
        {org.label for org in provenance.ORGS} | set(provenance.PLACES.values()) | set(provenance.KIND_LABELS.values())
    )
    out |= {label for patterns in provenance._OWN.values() for _, label in patterns}
    out |= {text for pair in branding.OUTLET_INTRO for text in pair}
    return {text for text in out if JP.search(text)}


def page_texts() -> set[str]:
    return template_texts() | code_texts() | data_texts()


def script_texts() -> set[str]:
    call = re.compile(r"""(?<![\w.$])T\(\s*("(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|`(?:\\.|[^`\\])*`)""")
    out = set()
    for path in sorted((PACKAGE / "static").glob("*.js")):
        for lit in call.findall(path.read_text(encoding="utf-8")):
            if lit.startswith("`") and "${" in lit:
                continue
            text = json.loads('"' + lit[1:-1].replace('"', '\\"') + '"') if lit[0] != '"' else json.loads(lit)
            if JP.search(text):
                out.add(text)
    return out


def missing(lang: str) -> dict[str, list[str]]:
    table = i18n.table(lang)
    return {
        "pages": sorted(page_texts() - set(table["pages"])),
        "scripts": sorted(script_texts() - set(table["scripts"])),
    }


def _forms(value) -> list[str]:
    return list(value.values()) if isinstance(value, dict) else [value]


class TranslationTests(unittest.TestCase):
    def test_every_text_has_its_translation(self):
        for lang in OTHERS:
            with self.subTest(lang=lang):
                gaps = missing(lang)
                self.assertEqual(gaps, {"pages": [], "scripts": []}, f"no translation in {lang}")

    def test_the_translations_keep_their_placeholders_and_have_no_kana(self):
        for lang in OTHERS:
            for part, texts in i18n.table(lang).items():
                for ja, said in texts.items():
                    for form in _forms(said):
                        with self.subTest(lang=lang, part=part, text=ja):
                            self.assertTrue(form.strip() or not ja.strip(), "empty")
                            self.assertFalse(KANA.search(form), f"kana left in {form!r}")
                            self.assertEqual(sorted(set(HOLE.findall(form))), sorted(set(HOLE.findall(ja))), form)

    def test_no_translation_is_kept_for_a_text_no_longer_used(self):
        used = {"pages": page_texts(), "scripts": script_texts()}
        for lang in OTHERS:
            for part, texts in i18n.table(lang).items():
                with self.subTest(lang=lang, part=part):
                    self.assertEqual(sorted(set(texts) - used[part]), [])

    def test_the_languages_have_the_same_texts(self):
        tables = [i18n.table(lang) for lang in OTHERS]
        for part in ("pages", "scripts"):
            self.assertEqual(
                {lang: len(t[part]) for lang, t in zip(OTHERS, tables)}, {lang: len(tables[0][part]) for lang in OTHERS}
            )

    def test_text_fills_in_and_picks_the_form_by_the_number(self):
        self.assertEqual(i18n.text("{count} 件", "ja", count=3), "3 件")
        self.assertEqual(i18n.text("{count} 件", "en-GB", count=1), "1 item")
        self.assertEqual(i18n.text("{count} 件", "en-GB", count=3), "3 items")
        self.assertEqual(i18n.text("まだない文", "ko"), "まだない文")  # no translation: Japanese
        self.assertEqual(i18n.from_browser("en-US,en;q=0.9"), "en-GB")
        self.assertEqual(i18n.from_browser("zh-CN"), "zh-TW")
        self.assertEqual(i18n.from_browser("fr-FR"), "en-GB")
        self.assertEqual(i18n.from_browser(""), "ja")
        self.assertEqual(i18n.from_browser("de;q=0.9,ko;q=0.8"), "ko")


from test_stories import NOBEL_ZH, NOBEL_ZH_EN, FakeTranslator, StoryTestCase

H = {"Host": "localhost"}
VISITOR = {"Host": "haruxera.tail1234.ts.net"}
#: Elements that carry the news itself (headlines, articles, translations of them) or data, not the site's words.
_NOT_THE_SITE = re.compile(
    r"<(script|style|title)\b.*?</\1>|<meta\b[^>]*>|<[^>]*\slang=\"(?:ja|zh-Hant|ko|en)\"[^>]*>.*?</", re.S
)


class PageTests(StoryTestCase):
    def setUp(self):
        super().setUp()
        self.refresh(FakeTranslator({NOBEL_ZH: NOBEL_ZH_EN}))
        from newsreader.web import create_app

        self.app = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32)
        self.app.config["STATUS_PATH"] = self.status
        self.client = self.app.test_client()
        from newsreader.library import stories

        self.key = stories.list_stories(self.db)["stories"][0]["key"]
        self.article = self.id_of("BBC", "a.html")

    def pages(self):
        return [
            "/",
            "/?q=Nobel",
            "/?genre=other",
            f"/a/{self.article}",
            "/stories",
            f"/stories/{self.key}",
            "/digest",
            "/outlets",
            "/saved",
            "/words",
            "/no-such-page",
        ]

    def test_the_choice_is_remembered_for_the_whole_host_and_the_address_comes_back_clean(self):
        response = self.client.get("/stories?scope=all&lang=ko", headers=H)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["Location"], "/stories?scope=all")
        # an address starting "//" stays on the site (not a protocol-relative link to another host)
        odd = self.client.get(
            "/", query_string="lang=ko", headers=H, environ_overrides={"PATH_INFO": "//other.example/"}
        )
        self.assertEqual(odd.headers["Location"], "/other.example/")
        cookie = response.headers["Set-Cookie"]
        self.assertIn("lang=ko", cookie)
        self.assertIn("Path=/", cookie)
        page = self.client.get("/stories?scope=all", headers=H)
        html = page.get_data(as_text=True)
        self.assertIn('<html lang="ko">', html)
        self.assertIn("Cookie", page.headers["Vary"])
        self.assertIn('<meta name="translation-default" content="ko">', html)
        self.assertRegex(
            html, r'<script src="/i18n/ko\.js\?v=\w+" defer></script><script src="/static/i18n\.js\?v=\w+" defer>'
        )
        self.assertEqual(self.client.get("/?lang=xx", headers=H).status_code, 200)  # not a language: no switch

    def test_without_a_choice_the_browser_decides(self):
        page = self.client.get("/", headers={**H, "Accept-Language": "en-US,en;q=0.8"}).get_data(as_text=True)
        self.assertIn('<html lang="en-GB">', page)
        self.assertIn('<meta property="og:locale" content="en_GB">', page)
        self.assertIn('aria-current="true">English (UK)</a>', page)
        self.assertNotIn("/i18n/", self.client.get("/", headers=H).get_data(as_text=True))  # Japanese needs no texts

    def test_the_scripts_texts(self):
        self.client.get("/?lang=zh-TW", headers=H)
        page = self.client.get("/", headers=H).get_data(as_text=True)
        url = re.search(r'src="(/i18n/zh-TW\.js\?v=\w+)"', page).group(1)
        response = self.client.get(url, headers=H)
        self.assertEqual(response.status_code, 200)
        self.assertIn("immutable", response.headers["Cache-Control"])
        body = response.get_data(as_text=True)
        texts = json.loads(body[body.index("=") + 1 : body.rindex(";")])
        self.assertEqual(texts, i18n.table("zh-TW")["scripts"])
        self.assertEqual(self.client.get("/i18n/ja.js", headers=H).status_code, 404)

    def test_every_page_comes_in_the_language(self):
        for lang in OTHERS:
            for headers in (H, VISITOR):  # the cookie of each host
                self.client.set_cookie("lang", lang, domain=headers["Host"])
            for path in self.pages():
                for headers in (H, VISITOR):
                    if headers is VISITOR and path in ("/saved", "/words"):
                        continue
                    with self.subTest(lang=lang, path=path, visitor=headers is VISITOR):
                        html = self.client.get(path, headers=headers).get_data(as_text=True)
                        self.assertIn(f'<html lang="{lang}">', html)
                        site = _NOT_THE_SITE.sub("", html[html.index("<body") :])
                        site = re.sub(r'<p class="foot-outlets">.*?</p>', "", site, flags=re.S)  # outlet names
                        left = sorted({m.group(0) for m in re.finditer(r"[^<>\"]*[ぁ-ゖァ-ヺ][^<>\"]*", site)})
                        self.assertEqual(left, [])

    def test_api_messages_come_in_the_language(self):
        self.client.set_cookie("lang", "en-GB")
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        response = self.client.post(
            "/api/translate-titles", json={"ids": [], "target": "ja"}, headers={**H, "X-CSRF-Token": csrf}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.get_json()["error"], i18n.text("記事または翻訳先の指定が不正です。", "en-GB"))
        self.assertFalse(KANA.search(response.get_json()["error"]))

    def test_dates_and_figures_in_each_language(self):
        from datetime import datetime

        from newsreader.library.compare import say_fact

        moment = datetime(2026, 10, 23, 16, 20)
        self.assertEqual(i18n.date_time(moment, "ja"), "2026年10月23日（金）16:20")
        self.assertEqual(i18n.date_time(moment, "ko"), "2026년 10월 23일 (금) 16:20")
        self.assertEqual(i18n.date_time(moment, "en-GB"), "Fri 23 Oct 2026, 16:20")
        self.assertEqual(i18n.date_time(moment, "zh-TW"), "2026年10月23日（週五）16:20")
        self.assertEqual(i18n.minutes(190, "en-GB"), i18n.text("{h}時間{m}分", "en-GB", h=3, m=10))
        self.assertEqual(
            [say_fact("少なくとも 53人", lang) for lang in OTHERS], ["최소 53명", "at least 53", "至少53人"]
        )
        self.assertEqual([say_fact("1.9兆ドル", lang) for lang in OTHERS], ["1.9조 달러", "$1.9 trillion", "1.9兆美元"])
        self.assertEqual([say_fact("50人超", lang) for lang in OTHERS], ["50명 이상", "more than 50", "超過50人"])
        self.assertEqual(say_fact("ほかの形", "ko"), "ほかの形")

    def test_the_front_pages_preview_in_the_language(self):
        from newsreader import hosting

        now = hosting.news_now(self.app, "en-GB")
        self.assertEqual(now["title"], i18n.text("各社が伝えた話題", "en-GB"))
        self.assertFalse(KANA.search(now["title"] + now["note"]))


if __name__ == "__main__":
    if "--missing" in sys.argv:
        for lang in OTHERS:
            gaps = missing(lang)
            print(f"{lang}: {len(gaps['pages'])} page texts, {len(gaps['scripts'])} script texts without a translation")
        print(json.dumps(missing(OTHERS[0]), ensure_ascii=False, indent=1))
    else:
        unittest.main()
