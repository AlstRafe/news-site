"""訳語の指定 (D-63): fixed translations of names, sent as Azure dynamic-dictionary markup."""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from newsreader.services import cache, relay, settings
from newsreader.services import translation as ts
from newsreader.web import create_app

REAL_BATCH = ts._translate_batch  # before the provider guard (conftest) replaces it
H = {"Host": "localhost"}


class GlossaryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "articles").mkdir()
        self.patches = [
            mock.patch.object(settings, "SETTINGS_PATH", self.tmp / "settings.json"),
            mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
        ]
        for patch in self.patches:
            patch.start()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        settings.delete_secret("azure_api_key")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_entries_are_validated_and_replaced(self):
        settings.save_glossary_entry(" 다카이치 ", "高市", "ja")
        settings.save_glossary_entry("다카이치", "高市早苗", "ja")  # same name and target: replaced
        settings.save_glossary_entry("다카이치", "Takaichi", "en")
        self.assertEqual(
            [(e["source"], e["target"], e["lang"]) for e in settings.glossary()],
            [("다카이치", "Takaichi", "en"), ("다카이치", "高市早苗", "ja")],
        )
        for bad in (
            ("", "x", "ja"),
            ("a", "", "ja"),
            ("a", "b", "fr"),
            ("a<b", "c", "ja"),
            ('a"', "c", "ja"),
            ("x" * 41, "y", "ja"),
        ):
            with self.assertRaises(ValueError):
                settings.save_glossary_entry(*bad)
        settings.remove_glossary_entry(0)
        self.assertEqual(len(settings.glossary()), 1)

    def test_markup_longest_name_first(self):
        entries = [("賴清德總統", "頼清徳総統"), ("賴清德", "頼清徳")]
        self.assertEqual(
            ts.apply_glossary("賴清德總統與賴清德", entries),
            '<mstrans:dictionary translation="頼清徳総統">賴清德總統</mstrans:dictionary>與'
            '<mstrans:dictionary translation="頼清徳">賴清德</mstrans:dictionary>',
        )
        self.assertEqual(ts.apply_glossary("no names", []), "no names")

    def test_azure_receives_markup_and_cached_translations_are_redone(self):
        settings.set_secret("azure_api_key", "k")
        sent = []

        def post(url, headers, data, provider):
            import json

            texts = [item["Text"] for item in json.loads(data)]
            sent.append(texts)
            return [{"translations": [{"text": f"T({t})"}]} for t in texts], {}

        with (
            mock.patch.object(ts, "active_provider", lambda: "azure"),
            mock.patch.object(ts, "_translate_batch", REAL_BATCH),
            mock.patch.object(ts, "_post_json", post),
        ):
            ts.translate_titles(["다카이치 총리"], "ja")
            ts.translate_titles(["다카이치 총리"], "ja")  # cached
            self.assertEqual(len(sent), 1)
            settings.save_glossary_entry("다카이치", "高市", "ja")
            ts.translate_titles(["다카이치 총리"], "ja")  # the glossary changed: translated again, with markup
            self.assertEqual(sent[-1], ['<mstrans:dictionary translation="高市">다카이치</mstrans:dictionary> 총리'])
            ts.translate_titles(["다카이치 총리"], "en")  # another target: no entry, no markup
            self.assertEqual(sent[-1], ["다카이치 총리"])

    def test_relay_uses_the_clients_glossary_not_the_servers(self):
        settings.save_glossary_entry("다카이치", "高市", "ja")
        seen = []

        def fake(provider, texts, target):
            seen.append(ts.glossary_for(target))
            return list(texts)

        with mock.patch.object(relay, "_post", return_value={"translations": ["x"]}) as post:
            relay._translate_batch(["다카이치"], "ja")
        self.assertEqual(post.call_args.args[1]["glossary"], [["다카이치", "高市"]])
        token = relay.issue_token("t")
        client = create_app(
            self.tmp / "articles", self.tmp / "lib.sqlite3", start_background=False, secret_key=b"g" * 32
        ).test_client()
        with mock.patch.object(ts, "_translate_batch", fake), mock.patch.object(ts, "active_provider", lambda: "azure"):
            auth = {**H, "Authorization": f"Bearer {token}"}
            client.post(
                "/relay/v1/translate", json={"texts": ["a"], "target": "ja", "glossary": [["Lee", "李"]]}, headers=auth
            )
            client.post("/relay/v1/translate", json={"texts": ["b"], "target": "ja"}, headers=auth)
            bad = client.post(
                "/relay/v1/translate", json={"texts": ["c"], "target": "ja", "glossary": [["<x>", "y"]]}, headers=auth
            )
        self.assertEqual(seen, [[("Lee", "李")], []])  # never the 本体's own 다카이치 entry
        self.assertEqual(bad.status_code, 400)

    def test_settings_page_adds_prefills_and_removes(self):
        client = create_app(
            self.tmp / "articles", self.tmp / "lib.sqlite3", start_background=False, secret_key=b"g" * 32
        ).test_client()
        page = client.get("/translation-settings?gsrc=다카이치&gdst=タカイチ&glang=ja", headers=H).get_data(
            as_text=True
        )
        self.assertIn('value="다카이치"', page)
        self.assertIn('value="タカイチ"', page)
        with client.session_transaction() as session:
            csrf = session["csrf"]
        client.post(
            "/translation-settings",
            headers=H,
            data={
                "csrf": csrf,
                "action": "add_glossary",
                "glossary_source": "다카이치",
                "glossary_target": "高市",
                "glossary_lang": "ja",
            },
        )
        self.assertEqual(settings.glossary()[0]["target"], "高市")
        page = client.get("/translation-settings", headers=H).get_data(as_text=True)
        self.assertIn('value="remove_glossary:0"', page)
        client.post("/translation-settings", headers=H, data={"csrf": csrf, "action": "remove_glossary:0"})
        self.assertEqual(settings.glossary(), [])


if __name__ == "__main__":
    unittest.main()
