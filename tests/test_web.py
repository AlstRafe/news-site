"""Phase 4: web layer — no parsing or network on page render, HTTP caching, security headers,
central CSRF, on-demand images. Uses fixtures only; nothing touches the real instance/."""

import base64
import gzip
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_extractor import CNA, NAVER, NORMAL

from newsreader.library import index
from newsreader.services import images as image_service
from newsreader.web import create_app, security

H = {"Host": "localhost"}
JPEG = b"\xff\xd8\xff\xe0" + b"0" * 2000
IMG = b'<img src="https://imgcdn.cna.com.tw/www/WebPhotos/1024/photo.jpg" alt="Caption">'
CNA_WITH_IMAGE = CNA.replace(b"</div></article>", IMG + b"</div></article>")


class WebTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.root = self.tmp / "articles"
        for site, name, data in [
            ("BBC", "a.html", NORMAL),
            ("NAVER NEWS", "n.html", NAVER),
            ("CNA", "c.html", CNA_WITH_IMAGE),
        ]:
            (self.root / site).mkdir(parents=True, exist_ok=True)
            (self.root / site / name).write_bytes(data)
        self.db = self.tmp / "library.sqlite3"
        index._last_sync.clear()
        index.sync(self.root, self.db, force=True)
        self.app = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32)
        self.client = self.app.test_client()
        self.cna = index.find_by_file(self.db, "CNA", "c.html")["id"]
        image_service._failures.clear()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def csrf(self):
        self.client.get("/a/%d" % self.cna, headers=H)
        with self.client.session_transaction() as session:
            return session["csrf"]


class NoParsingNoNetworkTests(WebTestCase):
    def test_list_page_never_opens_article_html(self):
        real_open = open

        def guarded_open(file, *args, **kwargs):
            if str(file).endswith((".html", ".htm")) and str(self.root) in str(file):
                raise AssertionError(f"list page opened {file}")
            return real_open(file, *args, **kwargs)

        real_read_bytes = Path.read_bytes

        def guarded_read_bytes(path):
            if path.suffix.lower() in (".html", ".htm"):
                raise AssertionError(f"list page read {path}")
            return real_read_bytes(path)

        with (
            mock.patch("builtins.open", guarded_open),
            mock.patch.object(Path, "read_bytes", guarded_read_bytes),
            mock.patch("newsreader.sources.extract_article", side_effect=AssertionError("parsed")),
        ):
            page = self.client.get("/", headers=H)
        self.assertEqual(page.status_code, 200)
        self.assertIn("CNA headline", page.get_data(as_text=True))

    def test_article_page_neither_parses_nor_uses_the_network(self):
        with (
            mock.patch("newsreader.web.pages.extract_article", side_effect=AssertionError("re-parsed")),
            mock.patch("urllib.request.urlopen", side_effect=AssertionError("network")),
            mock.patch.object(image_service, "_download", side_effect=AssertionError("image download")),
        ):
            page = self.client.get(f"/a/{self.cna}", headers=H)
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn("First CNA paragraph.", html)
        self.assertRegex(html, rf'<img src="/img/{self.cna}/0\?k=[0-9a-f]{{12}}"')

    def test_row_without_body_falls_back_to_parsing_once(self):
        with index.connect(self.db) as db:
            db.execute("UPDATE articles SET content = NULL, content_key = NULL WHERE id = ?", (self.cna,))
        page = self.client.get(f"/a/{self.cna}", headers=H)
        self.assertEqual(page.status_code, 200)
        self.assertIn("First CNA paragraph.", page.get_data(as_text=True))


class HttpCachingTests(WebTestCase):
    def test_article_page_etag_304_and_private_cache(self):
        first = self.client.get(f"/a/{self.cna}", headers=H)
        self.assertEqual(first.headers["Cache-Control"], "private, max-age=60")
        etag = first.headers["ETag"]
        self.assertTrue(etag.startswith('W/"'))
        again = self.client.get(f"/a/{self.cna}", headers={**H, "If-None-Match": etag})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(again.get_data(), b"")

    def test_list_revalidates_and_api_is_never_cached(self):
        self.assertEqual(self.client.get("/", headers=H).headers["Cache-Control"], "no-cache")
        token = self.csrf()
        api = self.client.post(
            "/api/translate-article", json={"id": 999, "target": "ja"}, headers={**H, "X-CSRF-Token": token}
        )
        self.assertEqual(api.headers["Cache-Control"], "no-store")

    def test_html_is_gzipped_when_accepted(self):
        plain = self.client.get("/", headers=H)
        packed = self.client.get("/", headers={**H, "Accept-Encoding": "gzip"})
        self.assertEqual(packed.headers["Content-Encoding"], "gzip")
        self.assertIn("Accept-Encoding", packed.headers["Vary"])
        self.assertEqual(gzip.decompress(packed.get_data()), plain.get_data())

    def test_static_assets_are_versioned_immutable_gzipped_and_304(self):
        html = self.client.get("/", headers=H).get_data(as_text=True)
        href = re.search(r'href="(/static/style\.css\?v=([0-9a-f]{12}))"', html)
        self.assertIsNotNone(href, "stylesheet must carry a content hash")
        css = self.client.get(href.group(1), headers={**H, "Accept-Encoding": "gzip"})
        self.assertIn("immutable", css.headers["Cache-Control"])
        self.assertEqual(css.headers["Content-Encoding"], "gzip")
        raw = gzip.decompress(css.get_data())
        self.assertEqual(hashlib.sha256(raw).hexdigest()[:12], href.group(2))
        again = self.client.get(href.group(1), headers={**H, "If-None-Match": css.headers["ETag"]})
        self.assertEqual(again.status_code, 304)
        self.assertEqual(
            self.client.get("/static/style.css", headers=H).headers["Cache-Control"],
            "no-cache",
            "an unversioned URL must not be cached forever",
        )

    def test_static_rejects_traversal_and_unknown_files(self):
        for path in ("/static/../app.py", "/static/..%2fapp.py", "/static/nope.css", "/static/"):
            self.assertIn(self.client.get(path, headers=H).status_code, (404, 308), path)


class SecurityTests(WebTestCase):
    def test_every_post_route_requires_the_csrf_token(self):
        posts = [rule.rule for rule in self.app.url_map.iter_rules() if "POST" in rule.methods]
        self.assertTrue(posts)
        for rule in posts:
            url = re.sub(r"<[^>]+>", "1", rule)
            self.assertEqual(self.client.post(url, json={}, headers=H).status_code, 400, rule)
            self.assertEqual(self.client.post(url, data={"csrf": "wrong"}, headers=H).status_code, 400, rule)

    def test_cross_site_post_is_refused_even_with_a_token(self):
        token = self.csrf()
        response = self.client.post(
            "/api/translate",
            json={"text": "x", "target": "ja"},
            headers={**H, "X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"},
        )
        self.assertEqual(response.status_code, 403)

    def test_host_header_rules(self):
        self.assertEqual(self.client.get("/", headers={"Host": "evil.example"}).status_code, 403)
        for host in ("localhost:5000", "127.0.0.1:5000", "[::1]:5000", "[::1]", "LOCALHOST."):
            self.assertEqual(self.client.get("/", headers={"Host": host}).status_code, 200, host)
        with mock.patch.dict(os.environ, {"NEWS_ALLOWED_HOSTS": "reader.tailnet.ts.net"}):
            client = create_app(self.root, self.db, start_background=False, secret_key=b"k" * 32).test_client()
        self.assertEqual(client.get("/", headers={"Host": "reader.tailnet.ts.net"}).status_code, 200)

    def test_launcher_and_app_agree_on_bind_addresses(self):
        import ipaddress

        import launcher
        from newsreader.netpolicy import is_allowed_bind_address
        from newsreader.services.settings import validate_bind_host

        samples = [
            "0.0.0.0",
            "0.1.2.3",
            "127.0.0.1",
            "10.0.0.5",
            "172.16.3.4",
            "192.168.1.5",
            "100.64.0.1",
            "100.127.255.254",
            "169.254.10.10",
            "8.8.8.8",
            "224.0.0.1",
            "255.255.255.255",
            "100.128.0.1",
        ]
        for host in samples:
            app_ok = is_allowed_bind_address(ipaddress.ip_address(host))
            try:
                launcher.validate_bind_host(host)
                launcher_ok = True
            except ValueError:
                launcher_ok = False
            try:
                settings_ok = validate_bind_host(host) == host
            except ValueError:
                settings_ok = False
            self.assertEqual((launcher_ok, settings_ok), (app_ok, app_ok), host)
        self.assertFalse(is_allowed_bind_address(ipaddress.ip_address("0.0.0.0")), "every interface, incl. public")

    def test_bind_host_must_be_local(self):
        for bad in ("0.0.0.0", "::", "8.8.8.8", "example.com"):
            with mock.patch.dict(os.environ, {"NEWS_BIND_HOST": bad}):
                with self.assertRaises(RuntimeError):
                    security.configured_bind_host()
        with mock.patch.dict(os.environ, {"NEWS_BIND_HOST": "100.101.102.103"}):
            self.assertEqual(security.configured_bind_host(), "100.101.102.103")

    def test_csp_allows_exactly_the_inline_scripts_on_every_page(self):
        for url in ("/", f"/a/{self.cna}", "/translation-settings", "/a/99999"):
            response = self.client.get(url, headers=H)
            csp = response.headers["Content-Security-Policy"]
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("unsafe-eval", csp)
            html = response.get_data(as_text=True)
            for body in re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S):
                digest = base64.b64encode(hashlib.sha256(body.encode()).digest()).decode()
                self.assertIn(f"'sha256-{digest}'", csp, f"inline script on {url} is not allowed by the CSP")
            self.assertIsNone(re.search(r"\son[a-z]+\s*=", html), f"inline event handler on {url}")
            self.assertIsNone(re.search(r"\sstyle\s*=", html), f"inline style attribute on {url}")
            for header in ("X-Content-Type-Options", "X-Frame-Options", "Referrer-Policy"):
                self.assertIn(header, response.headers)

    def test_secret_key_file_is_created_once_and_private(self):
        path = self.tmp / "instance" / "secret_key"
        first = security.persistent_secret_key(path)
        self.assertEqual(security.persistent_secret_key(path), first)
        if os.name != "nt":
            self.assertEqual(path.stat().st_mode & 0o077, 0)

    def test_errors_render_friendly_pages_and_json_for_the_api(self):
        page = self.client.get("/a/99999", headers=H)
        self.assertEqual(page.status_code, 404)
        self.assertIn("ページが見つかりません", page.get_data(as_text=True))
        token = self.csrf()
        api = self.client.post("/api/nope", json={}, headers={**H, "X-CSRF-Token": token})
        self.assertEqual(api.status_code, 404)
        self.assertIn("error", api.get_json())


class _FakeResponse(io.BytesIO):
    def __init__(self, data, url, content_type="image/jpeg"):
        super().__init__(data)
        self._url = url
        self.headers = mock.Mock()
        self.headers.get_content_type.return_value = content_type

    def geturl(self):
        return self._url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ImageTests(WebTestCase):
    def image_url(self):
        html = self.client.get(f"/a/{self.cna}", headers=H).get_data(as_text=True)
        return re.search(rf'src="(/img/{self.cna}/0\?k=[0-9a-f]+)"', html).group(1)

    def test_first_request_downloads_once_then_serves_from_disk_forever(self):
        calls = []

        def fake_open(request, timeout):
            calls.append(request.full_url)
            return _FakeResponse(JPEG, request.full_url)

        url = self.image_url()
        with mock.patch.object(image_service._opener, "open", side_effect=fake_open):
            first = self.client.get(url, headers=H)
            second = self.client.get(url, headers=H)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(first.get_data(), JPEG)
        self.assertEqual(len(calls), 1)
        self.assertIn("immutable", second.headers["Cache-Control"])
        self.assertEqual(first.headers["Content-Type"], "image/jpeg")
        cached = list((self.root / "CNA" / "c_media").glob("*.jpg"))
        self.assertEqual(len(cached), 1, "stored in the v3 cache layout")

    def test_concurrent_requests_download_a_single_time(self):
        calls = []
        gate = threading.Event()

        def slow_open(request, timeout):
            calls.append(1)
            gate.wait(2)
            return _FakeResponse(JPEG, request.full_url)

        url = self.image_url()
        results = []
        with mock.patch.object(image_service._opener, "open", side_effect=slow_open):
            threads = [
                threading.Thread(target=lambda: results.append(self.app.test_client().get(url, headers=H).status_code))
                for _ in range(6)
            ]
            for t in threads:
                t.start()
            time.sleep(0.2)
            gate.set()
            for t in threads:
                t.join(5)
        self.assertEqual(results, [200] * 6)
        self.assertEqual(len(calls), 1)

    def test_unsafe_or_bad_images_are_refused(self):
        url = self.image_url()
        cases = [
            ("redirect off-host", lambda r, timeout: _FakeResponse(JPEG, "https://evil.example/x.jpg")),
            ("not an image type", lambda r, timeout: _FakeResponse(JPEG, r.full_url, "text/html")),
            ("magic mismatch", lambda r, timeout: _FakeResponse(b"<html>" * 100, r.full_url)),
            (
                "too large",
                lambda r, timeout: _FakeResponse(
                    b"\xff\xd8\xff" + b"0" * (image_service.MAX_IMAGE_BYTES + 1), r.full_url
                ),
            ),
        ]
        for label, behaviour in cases:
            image_service._failures.clear()
            with mock.patch.object(image_service._opener, "open", side_effect=behaviour):
                response = self.client.get(url, headers=H)
            self.assertEqual(response.status_code, 404, label)
            self.assertEqual(response.headers["Cache-Control"], "no-store", label)
        self.assertFalse(list((self.root / "CNA").glob("c_media/*.jpg")))

    def test_redirect_handler_blocks_leaving_the_image_hosts(self):
        handler = image_service._StayOnImageHosts()
        with self.assertRaises(image_service.ImageUnavailable):
            handler.redirect_request(mock.Mock(), None, 302, "Found", {}, "http://192.168.0.1/admin")

    def test_failures_are_not_retried_for_a_while(self):
        url = self.image_url()
        with mock.patch.object(image_service._opener, "open", side_effect=OSError("offline")) as opener:
            self.assertEqual(self.client.get(url, headers=H).status_code, 404)
            self.assertEqual(self.client.get(url, headers=H).status_code, 404)
        self.assertEqual(opener.call_count, 1)

    def test_only_allowed_https_hosts_are_ever_fetched(self):
        for source in (
            "http://imgcdn.cna.com.tw/a.jpg",
            "https://evil.example/a.jpg",
            "https://imgcdn.cna.com.tw.evil.example/a.jpg",
        ):
            with self.assertRaises(image_service.ImageUnavailable):
                image_service.cached_or_fetch(source, self.tmp / "media")

    def test_stale_image_key_redirects_to_the_current_image(self):
        response = self.client.get(f"/img/{self.cna}/0?k=000000000000", headers=H)
        self.assertEqual(response.status_code, 302)
        self.assertIn(f"/img/{self.cna}/0?k=", response.headers["Location"])
        self.assertEqual(self.client.get(f"/img/{self.cna}/5", headers=H).status_code, 404)

    def test_list_thumbnail_points_at_img(self):
        html = self.client.get("/", headers=H).get_data(as_text=True)
        self.assertIn(f'src="/img/{self.cna}/0?k=', html)
        self.assertNotIn("imgcdn.cna.com.tw", html, "no hot-linking: the phone never contacts the news site")


class AppFactoryTests(unittest.TestCase):
    def test_importing_the_entry_point_has_no_side_effects(self):
        root = Path(__file__).resolve().parents[1]
        code = (
            "import threading, app; "
            "assert callable(app.main); "
            "assert threading.active_count() == 1, threading.enumerate()"
        )
        with tempfile.TemporaryDirectory():
            env = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            before = set((root / "instance").glob("*")) if (root / "instance").exists() else set()
            subprocess.run([sys.executable, "-c", code], cwd=root, env=env, check=True)
            after = set((root / "instance").glob("*")) if (root / "instance").exists() else set()
        self.assertEqual(before, after, "importing app.py must not create instance files")

    def test_background_sync_thread_starts_only_when_asked(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            (tmp / "articles" / "CNA").mkdir(parents=True)
            (tmp / "articles" / "CNA" / "c.html").write_bytes(CNA)
            app = create_app(tmp / "articles", tmp / "db.sqlite3", secret_key=b"k" * 32, sync_interval=30)
            worker = app.extensions["library_sync"]
            for _ in range(200):
                if index.list_articles(tmp / "db.sqlite3")["total"] == 1:
                    break
                time.sleep(0.02)
            self.assertEqual(index.list_articles(tmp / "db.sqlite3")["total"], 1)
            worker.stop()
            self.assertNotIn(
                "library_sync",
                create_app(
                    tmp / "articles", tmp / "db.sqlite3", start_background=False, secret_key=b"k" * 32
                ).extensions,
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_templates_reload_only_in_dev_mode(self):
        with mock.patch.dict(os.environ, {"NEWS_DEV": ""}):
            self.assertFalse(create_app(start_background=False, secret_key=b"k" * 32).jinja_env.auto_reload)
        with mock.patch.dict(os.environ, {"NEWS_DEV": "1"}):
            self.assertTrue(create_app(start_background=False, secret_key=b"k" * 32).jinja_env.auto_reload)

    def test_run_uses_waitress_and_reports_bad_settings(self):
        from newsreader.web import run

        with mock.patch.dict(os.environ, {"NEWS_BIND_HOST": "8.8.8.8", "NEWS_DEV": ""}):
            self.assertEqual(run.main(), 2)
        with (
            mock.patch.dict(
                os.environ,
                {"NEWS_BIND_HOST": "127.0.0.1", "NEWS_PORT": "5099", "NEWS_DEV": "", "WERKZEUG_RUN_MAIN": ""},
            ),
            mock.patch.object(run, "serve_until_redeployed", return_value=0) as serve,
            mock.patch("newsreader.web.create_app"),
        ):
            self.assertEqual(run.main(), 0)
        serve.assert_called_once()
        self.assertEqual(serve.call_args.args[1:3], ("127.0.0.1", 5099))
        self.assertFalse(serve.call_args.kwargs["supervised"], "only launcher-started servers restart themselves")

    def test_supervised_server_restarts_on_new_version_after_finishing_requests(self):
        import socket
        import urllib.request

        from newsreader import paths
        from newsreader.web import run

        tmp = Path(tempfile.mkdtemp())
        version = tmp / "VERSION"
        version.write_text("v1", encoding="utf-8")
        gate = threading.Event()
        from flask import Flask

        app = Flask("slow")

        @app.get("/slow")
        def slow():
            gate.wait(5)
            return "done"

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result, body = {}, {}
        with mock.patch.object(paths, "VERSION_PATH", version):
            server = threading.Thread(
                target=lambda: result.setdefault(
                    "code", run.serve_until_redeployed(app, "127.0.0.1", port, poll_seconds=0.1)
                )
            )
            server.start()
            time.sleep(0.3)
            client = threading.Thread(
                target=lambda: body.setdefault(
                    "text", urllib.request.urlopen(f"http://127.0.0.1:{port}/slow", timeout=10).read()
                )
            )
            client.start()
            time.sleep(0.3)
            version.write_text("v2", encoding="utf-8")  # a deployment finished
            time.sleep(0.5)
            gate.set()
            client.join(10)
            server.join(10)
        shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(body.get("text"), b"done", "the request in flight was answered")
        self.assertEqual(result.get("code"), run.RESTART_EXIT_CODE)

    def test_server_also_answers_on_tailscale_address_and_restarts_cleanly(self):
        import socket
        import urllib.request

        from newsreader import paths
        from newsreader.web import run

        tmp = Path(tempfile.mkdtemp())
        version = tmp / "VERSION"
        version.write_text("v1", encoding="utf-8")
        from flask import Flask

        app = Flask("two")
        app.get("/")(lambda: "ok")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        result = {}
        with mock.patch.object(paths, "VERSION_PATH", version):
            # 192.0.2.1 is not on this machine: that extra address is dropped, the main one still serves.
            server = threading.Thread(
                target=lambda: result.setdefault(
                    "code", run.serve_until_redeployed(app, "127.0.0.1", port, poll_seconds=0.1, also=("192.0.2.1",))
                )
            )
            server.start()
            time.sleep(0.3)
            self.assertEqual(urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=5).read(), b"ok")
            version.write_text("v2", encoding="utf-8")
            server.join(10)
            self.assertEqual(result.get("code"), run.RESTART_EXIT_CODE)
            result.clear()
            server = threading.Thread(
                target=lambda: result.setdefault(
                    "code", run.serve_until_redeployed(app, "127.0.0.1", port, poll_seconds=0.1, also=("127.0.0.2",))
                )
            )
            server.start()
            time.sleep(0.3)
            for host in ("127.0.0.1", "127.0.0.2"):
                self.assertEqual(urllib.request.urlopen(f"http://{host}:{port}/", timeout=5).read(), b"ok")
            version.write_text("v3", encoding="utf-8")
            server.join(10)
        shutil.rmtree(tmp, ignore_errors=True)
        self.assertFalse(server.is_alive(), "every listener closed on redeploy")
        self.assertEqual(result.get("code"), run.RESTART_EXIT_CODE)

    def test_other_addresses_are_added_only_when_open_to_the_network(self):
        from newsreader.web import run

        with (
            mock.patch("newsreader.netpolicy.tailscale_address", return_value="100.70.63.91"),
            mock.patch("newsreader.netpolicy.lan_address", return_value="192.168.3.12"),
        ):
            self.assertEqual(run._network_too("127.0.0.1"), ())
            self.assertEqual(run._network_too("192.168.3.12"), ("100.70.63.91", "127.0.0.1"))
            self.assertEqual(run._network_too("100.70.63.91"), ("192.168.3.12", "127.0.0.1"))
        with (
            mock.patch("newsreader.netpolicy.tailscale_address", return_value=None),
            mock.patch("newsreader.netpolicy.lan_address", return_value="192.168.3.12"),
        ):
            self.assertEqual(run._network_too("192.168.3.12"), ("127.0.0.1",))

    def test_an_address_that_comes_up_later_restarts_the_server(self):
        import socket

        from newsreader import paths
        from newsreader.web import run

        tmp = Path(tempfile.mkdtemp())
        version = tmp / "VERSION"
        version.write_text("v1", encoding="utf-8")
        from flask import Flask

        app = Flask("late")
        app.get("/")(lambda: "ok")
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        seen = {"now": ()}
        result = {}
        with mock.patch.object(paths, "VERSION_PATH", version), mock.patch.object(run, "NETWORK_POLL_ROUNDS", 1):
            server = threading.Thread(
                target=lambda: result.setdefault(
                    "code",
                    run.serve_until_redeployed(app, "127.0.0.1", port, poll_seconds=0.1, network=lambda: seen["now"]),
                )
            )
            server.start()
            time.sleep(0.4)
            self.assertTrue(server.is_alive(), "nothing new: keeps serving")
            seen["now"] = ("127.0.0.2",)  # e.g. Tailscale connected after the logon start
            server.join(10)
        shutil.rmtree(tmp, ignore_errors=True)
        self.assertEqual(result.get("code"), run.RESTART_EXIT_CODE)


if __name__ == "__main__":
    unittest.main()
