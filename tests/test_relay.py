"""Translation relay (D-50): a NewsBridge translates for test builds that have no key of their own."""
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

H = {"Host": "localhost"}
REAL_BATCH = ts._translate_batch


class RelayTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / "articles").mkdir()
        self.calls = []

        def fake_batch(provider, texts, target):
            self.calls.append((provider, list(texts), target))
            return [f"[{target}] {t}" for t in texts]

        self.patches = [mock.patch.object(settings, "SETTINGS_PATH", self.tmp / "settings.json"),
                        mock.patch.object(cache, "CACHE_PATH", self.tmp / "cache.sqlite3"),
                        mock.patch.object(ts, "_translate_batch", fake_batch)]
        for patch in self.patches:
            patch.start()
        self.client = create_app(self.tmp / "articles", self.tmp / "library.sqlite3", start_background=False,
                                 secret_key=b"r" * 32).test_client()

    def tearDown(self):
        for patch in reversed(self.patches):
            patch.stop()
        settings.delete_secret("relay_token")
        shutil.rmtree(self.tmp, ignore_errors=True)

    def post(self, path, body, token=None):
        headers = dict(H, **({"Authorization": f"Bearer {token}"} if token else {}))
        return self.client.post(path, headers=headers, json=body)


class ServerTests(RelayTestCase):
    def test_only_a_digest_of_the_token_is_kept(self):
        token = relay.issue_token("テスト版", daily_chars=1000)
        stored = (self.tmp / "settings.json").read_text(encoding="utf-8")
        self.assertNotIn(token, stored)
        self.assertEqual(len(relay.tokens()), 1)
        self.assertIsNotNone(relay.check_token(f"Bearer {token}"))
        self.assertIsNone(relay.check_token(f"Bearer {token}x"))
        self.assertIsNone(relay.check_token(token))

    def test_translates_for_a_valid_token_within_its_daily_allowance(self):
        token = relay.issue_token("テスト版", daily_chars=30)
        self.assertEqual(self.post("/relay/v1/translate", {"texts": ["hello"], "target": "ja"}).status_code, 400)  # no token: an ordinary POST, refused by CSRF
        self.assertEqual(self.post("/relay/v1/translate", {"texts": ["hello"], "target": "ja"}, "nbr_wrong").status_code, 401)
        reply = self.post("/relay/v1/translate", {"texts": ["hello", "world"], "target": "ja"}, token)
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.get_json(), {"translations": ["[ja] hello", "[ja] world"]})
        # the same texts again come from the 本体's cache (no provider call) but still count
        self.post("/relay/v1/translate", {"texts": ["hello", "world"], "target": "ja"}, token)
        self.assertEqual(len(self.calls), 1)
        over = self.post("/relay/v1/translate", {"texts": ["x" * 25], "target": "ja"}, token)
        self.assertEqual(over.status_code, 429)
        self.assertEqual(self.post("/relay/v1/translate", {"texts": ["a"], "target": "xx"}, token).status_code, 400)
        self.assertEqual(self.post("/relay/v1/translate", {"texts": "a", "target": "ja"}, token).status_code, 400)

    def test_dictionary_through_the_relay(self):
        token = relay.issue_token("テスト版", daily_chars=100)
        senses = [{"pos": "動詞", "terms": ["飛ぶ"]}]
        from newsreader.web import relay_view
        with mock.patch.object(relay_view, "dictionary_lookup", return_value=senses):
            reply = self.post("/relay/v1/dictionary", {"text": "fly", "target": "ja"}, token)
        self.assertEqual(reply.get_json(), {"senses": senses})
        self.assertEqual(self.post("/relay/v1/dictionary", {"text": 3, "target": "ja"}, token).status_code, 400)

    def test_a_relay_client_does_not_relay_again(self):
        token = relay.issue_token("テスト版")
        settings.save_translation_source("relay", "http://100.64.0.1:5000")
        settings.set_secret("relay_token", "nbr_test")
        self.assertEqual(self.post("/relay/v1/translate", {"texts": ["a"], "target": "ja"}, token).status_code, 503)
        self.assertEqual(self.calls, [])

    def test_pages_still_need_csrf_and_relay_needs_its_token(self):
        self.assertEqual(self.client.post("/api/translate", headers=H, json={"text": "a", "target": "ja"}).status_code, 400)
        self.assertEqual(self.client.post("/relay/v1/translate", headers=H, json={"texts": ["a"], "target": "ja"}).status_code, 400)

    def test_settings_page_issues_a_token_once(self):
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        page = self.client.post("/translation-settings", headers=H, data={"csrf": csrf, "action": "issue_relay_token"})
        html = page.get_data(as_text=True)
        self.assertIn('<code class="token">nbr_', html)
        self.assertNotIn('<code class="token">', self.client.get("/translation-settings", headers=H).get_data(as_text=True))
        relay.issue_token("二本目")
        first = relay.tokens()[0]["hash"][:16]
        self.assertIn(f'value="revoke_token:{first}"', self.client.get("/translation-settings", headers=H).get_data(as_text=True))
        self.client.post("/translation-settings", headers=H, data={"csrf": csrf, "action": f"revoke_token:{first}"})
        self.assertEqual([t["name"] for t in relay.tokens()], ["二本目"])
        self.assertFalse(relay.revoke("zz"))
        self.client.post("/translation-settings", headers=H, data={"csrf": csrf, "action": "revoke_relay_tokens"})
        self.assertEqual(relay.tokens(), [])


class PingTests(RelayTestCase):
    def test_ping_reports_the_allowance_without_charging(self):
        token = relay.issue_token("テスト版", daily_chars=500)
        self.post("/relay/v1/translate", {"texts": ["abcd"], "target": "ja"}, token)
        reply = self.post("/relay/v1/ping", {}, token)
        self.assertEqual(reply.get_json(), {"ok": True, "daily_chars": 500, "used_today": 4})
        self.post("/relay/v1/ping", {}, token)
        self.assertEqual(self.post("/relay/v1/ping", {}, token).get_json()["used_today"], 4)
        self.assertEqual(self.post("/relay/v1/ping", {}, "nbr_wrong").status_code, 401)

    def test_settings_check_saves_then_reports(self):
        self.client.get("/", headers=H)
        with self.client.session_transaction() as session:
            csrf = session["csrf"]
        form = {"csrf": csrf, "action": "check_relay", "translation_source": "relay", "relay_url": "http://100.64.0.1:5000",
                "relay_token": "nbr_typed", "azure_region": "", "network_bind_host": "", "network_port": ""}
        with mock.patch.object(relay, "_post", return_value={"ok": True, "daily_chars": 1000, "used_today": 250}) as post:
            html = self.client.post("/translation-settings", headers=H, data=form).get_data(as_text=True)
        post.assert_called_once_with("/relay/v1/ping", {})
        self.assertIn("あと 750 文字", html)
        self.assertEqual(settings.get_secret("relay_token"), "nbr_typed")
        with mock.patch.object(relay, "_post", side_effect=relay.RelayError("翻訳の中継先に接続できませんでした。")):
            html = self.client.post("/translation-settings", headers=H, data=dict(form, relay_token="")).get_data(as_text=True)
        self.assertIn("設定は保存しました。ただし翻訳の中継先に接続できませんでした。", html)
        self.assertNotIn("nbr_typed", html)


class ClientTests(RelayTestCase):
    def test_a_build_without_a_key_translates_through_the_relay(self):
        settings.save_translation_source("relay", "http://100.64.0.1:5000/")
        settings.set_secret("relay_token", "nbr_test")
        self.assertEqual(settings.get_settings()["relay_url"], "http://100.64.0.1:5000")
        self.assertEqual(ts.active_provider(), "relay")
        self.assertTrue(ts.provider_ready())
        sent = []

        def fake_post(path, body):
            sent.append((path, body))
            return {"translations": [f"<{body['target']}> {t}" for t in body["texts"]]}

        with mock.patch.object(ts, "_translate_batch", REAL_BATCH), \
                mock.patch.object(relay, "_post", fake_post):
            self.assertEqual(ts.translate_text("Hello", "ja", limit=100), "<ja> Hello")
            self.assertEqual(ts.translate_titles(["A", "B"], "ko"), ["<ko> A", "<ko> B"])
        self.assertEqual([path for path, _ in sent], ["/relay/v1/translate", "/relay/v1/translate"])

    def test_large_inputs_are_split_within_the_relay_limits(self):
        sent = []

        def fake_post(path, body):
            sent.append(sum(len(t) for t in body["texts"]))
            return {"translations": body["texts"]}

        with mock.patch.object(relay, "_post", fake_post):
            out = relay.translate(["x" * 5000] * 5, "ja")
        self.assertEqual(len(out), 5)
        self.assertTrue(all(size <= relay.MAX_REQUEST_CHARS for size in sent))
        self.assertGreater(len(sent), 1)

    def test_bad_urls_and_sources_are_refused(self):
        for url in ("ftp://x", "javascript:alert(1)", "http://", "http://a/b?c=1"):
            self.assertEqual(settings.relay_url(url), "", url)
        with self.assertRaises(ValueError):
            settings.save_translation_source("relay", "not a url")
        with self.assertRaises(ValueError):
            settings.save_translation_source("deepl", "")

    def test_errors_never_carry_the_token(self):
        settings.save_translation_source("relay", "http://127.0.0.1:9")
        settings.set_secret("relay_token", "nbr_secret_value")
        with self.assertRaises(relay.RelayError) as raised:
            relay.translate(["a"], "ja")
        self.assertNotIn("nbr_secret_value", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
