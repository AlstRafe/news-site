"""Make a test build (alpha) of NewsBridge as one ZIP to hand to a tester (D-53).

The ZIP holds exactly the code this installation runs (the files make_portable_zip.py would
ship, VERSION included), plus what makes it a test build — and nothing else differs:

* ``articles/``            a few sample articles (default 5, chosen so one news story is
                           reported by several outlets and the 読み比べ view has something to show)
* ``preset/settings.json`` translation through this installation (relay), listen on 127.0.0.1
                           only, a library limit (the collector adds nothing beyond it), no
                           start-at-logon; launcher.py applies it once
* ``preset/relay_token``   a token this installation issues now; moved into the tester's
                           credential store on first start. It can be revoked on the settings
                           page (テスト版への翻訳の中継) and has a daily character allowance.
* ``はじめにお読みください.txt``

Run it on the installation that holds the translation key (it records the token's digest in
its instance/settings.json). ``--dry-run`` shows the choice and writes nothing.

    python make_alpha_zip.py [--relay-url http://100.x.y.z:5000] [--articles 5] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import zipfile
from contextlib import closing
from datetime import datetime
from pathlib import Path

from make_portable_zip import PROJECT_ROOT, should_include

FOLDER = "NewsBridge-alpha"
DEFAULT_ARTICLES = 5
DEFAULT_DAILY_CHARS = 100_000
#: Mean similarity every member of the sample's 読み比べ group should reach (stories_core scores).
COHESIVE_SCORE = 0.3
#: Articles of the sample's 読み比べ group (one per outlet first).
STORY_ARTICLES = 3

README = """NewsBridge テスト版（{version}）

■ 動かし方（Windows）
1. この ZIP を好きな場所に展開する（「すべて展開」。ZIP の中から直接は起動できません）。
2. 展開したフォルダーの start_windows.bat をダブルクリック。
   Python 3.10 以上が無い PC では、公式の Python を自動でダウンロードして入れます（このユーザーだけ、検証付き）。
   初回は部品の準備に数分かかります。インターネット接続が必要です。
3. ブラウザーが開きます（http://127.0.0.1:{port}/ ）。このPCの中だけで動き、ほかの機器からは見えません。
4. 翻訳が使えるかは「設定」の「保存して接続を確認」で確かめられます。

■ 中身
・本番と同じプログラムです。違いは設定だけ: 翻訳は配布元の NewsBridge が中継（APIキーの取得は不要）、
  記事はサンプル {count} 本（毎時の自動取得とログオン時の自動起動は登録しません）。
・翻訳の中継先: {relay_url}
  配布元の PC が起動していて、このPCからその宛先に届く必要があります（Tailscale で共有されたネットワークなど）。
  届かないときは翻訳ボタンで「中継先に接続できませんでした」と表示されます。

■ 注意
・この ZIP には翻訳の中継用の合言葉が入っています。ほかの人に渡さないでください。
  初回起動時にこの PC の資格情報ストアへ移され、ファイルは消えます。配布元はいつでも無効にできます。
・終了はコマンドプロンプトの窓を閉じるか Ctrl+C。
・使い終わったらフォルダーを削除するだけです（Windows のタスクは登録しません）。準備した部品は
  %LOCALAPPDATA%\\NewsHtmlReader に、合言葉は Windows の資格情報マネージャー（NewsHtmlReader）に残るので、不要なら削除してください。
"""


def library_root() -> Path:
    try:
        configured = json.loads((PROJECT_ROOT / "instance" / "collector.json").read_text(encoding="utf-8")).get("library_root", "")
    except (OSError, ValueError, TypeError, AttributeError):
        configured = ""
    return Path(configured) if configured else PROJECT_ROOT / "articles"


def choose_articles(db_path: Path, count: int, sources: set[str] | None = None) -> list[dict]:
    """``count`` readable articles: first the newest story reported by the most outlets (one
    article per outlet), then the newest article of each outlet not yet included, then newest."""
    if not db_path.is_file():
        raise SystemExit(f"記事の索引が見つかりません: {db_path}（アプリを一度起動してください）")
    uri = db_path.resolve().as_uri() + "?mode=ro"
    with closing(sqlite3.connect(uri, uri=True)) as db:
        db.row_factory = sqlite3.Row
        readable = "a.error = '' AND a.paragraph_count >= 3"
        if sources:  # only outlets that are still collected (saved NAVER pages are not)
            readable += " AND a.source IN (" + ",".join("'" + s.replace("'", "''") + "'" for s in sorted(sources)) + ")"
        # A clearly matched group if there is one (every member close to the rest), preferring
        # more outlets, then one whose outlets disagree on a figure (数字を比べる has something to
        # show), then the most closely matched; otherwise any group of the most outlets.
        has_facts = db.execute("SELECT 1 FROM sqlite_master WHERE name = 'story_facts'").fetchone() is not None
        disputed = ("(SELECT COUNT(*) FROM story_facts f WHERE f.story_key = m.story_key AND f.differs = 1)"
                    if has_facts else "0")
        story = None
        for cohesion in (COHESIVE_SCORE, -1.0):
            story = story or db.execute(f"""SELECT m.story_key FROM story_members m JOIN articles a ON a.id = m.article_id
                                         WHERE {readable} GROUP BY m.story_key
                                         HAVING COUNT(DISTINCT a.source) > 1 AND MIN(m.score) >= ?
                                         ORDER BY COUNT(DISTINCT a.source) DESC, MAX({disputed}) DESC,
                                                  AVG(m.score) DESC, MAX(a.published) DESC LIMIT 1""",
                                        (cohesion,)).fetchone()
        chosen: list[dict] = []
        if story:
            members = [dict(row) for row in db.execute(
                f"""SELECT a.id, a.source, a.filename, a.title FROM story_members m
                    JOIN articles a ON a.id = m.article_id WHERE m.story_key = ? AND {readable}
                    ORDER BY m.score DESC, a.published DESC""", (story[0],))]
            for row in members:  # one per outlet, then the group's other reports (an update, say)
                if row["source"] not in {c["source"] for c in chosen}:
                    chosen.append(row)
            chosen += [row for row in members if row not in chosen][:max(0, STORY_ARTICLES - len(chosen))]
        rest = [dict(r) for r in db.execute(f"SELECT a.id, a.source, a.filename, a.title FROM articles a WHERE {readable}"
                                            " ORDER BY a.published DESC, a.id DESC LIMIT 400")]
    taken = {c["id"] for c in chosen}
    for row in rest:  # one more per outlet first, so every outlet is represented
        if len(chosen) >= count:
            break
        if row["id"] not in taken and row["source"] not in {c["source"] for c in chosen}:
            chosen.append(row)
            taken.add(row["id"])
    for row in rest:
        if len(chosen) >= count:
            break
        if row["id"] not in taken:
            chosen.append(row)
            taken.add(row["id"])
    return chosen[:count]


def default_relay_url() -> str:
    try:
        url = json.loads((PROJECT_ROOT / "instance" / "status.json").read_text(encoding="utf-8"))["web"]["url"]
    except (OSError, ValueError, TypeError, KeyError):
        return ""
    return str(url).rstrip("/")


def build(out_dir: Path, articles: list[tuple[Path, str]], relay_url: str, token: str, count: int,
          stamp: str, port: str = "5000") -> Path:
    """Write the ZIP. ``articles`` = (file on disk, path under articles/)."""
    version = (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip() if (PROJECT_ROOT / "VERSION").is_file() else "dev"
    preset = {"preset_id": stamp, "build_channel": "alpha", "translation_source": "relay", "relay_url": relay_url,
              "network_bind_host": "127.0.0.1", "library_limit": count, "start_at_logon": False,
              "collect_hourly": False}
    archive_path = out_dir / f"{FOLDER}-{stamp}.zip"
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for file_path in sorted(PROJECT_ROOT.rglob("*")):
            if file_path.is_file():
                relative = file_path.relative_to(PROJECT_ROOT)
                if should_include(file_path, relative):
                    archive.write(file_path, f"{FOLDER}/{relative.as_posix()}")
        for source, name in articles:
            archive.write(source, f"{FOLDER}/articles/{name}")
        archive.writestr(f"{FOLDER}/preset/settings.json", json.dumps(preset, ensure_ascii=False, indent=2))
        archive.writestr(f"{FOLDER}/preset/relay_token", token)
        archive.writestr(f"{FOLDER}/はじめにお読みください.txt",
                         README.format(version=version, port=port, count=count, relay_url=relay_url).replace("\n", "\r\n"))
    return archive_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--relay-url", default="", help="このインストールにテスト版から届く URL（既定: 起動中の待受けアドレス）")
    parser.add_argument("--articles", type=int, default=DEFAULT_ARTICLES)
    parser.add_argument("--daily-chars", type=int, default=DEFAULT_DAILY_CHARS, help="合言葉ごとの1日の翻訳上限（文字）")
    parser.add_argument("--out", default=str(PROJECT_ROOT.parent))
    parser.add_argument("--library", default="", help="記事フォルダ（既定: このインストールの収集先）")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    from newsreader import paths
    from newsreader.services import relay, settings

    relay_url = settings.relay_url(args.relay_url or default_relay_url())
    if not relay_url:
        print("中継先の URL を --relay-url で指定してください（例: http://100.x.y.z:5000）。", file=sys.stderr)
        return 2
    if not 1 <= args.articles <= 50:
        print("--articles は 1〜50 で指定してください。", file=sys.stderr)
        return 2
    root = Path(args.library) if args.library else library_root()
    from newsreader.collect.autofetch import SOURCES
    chosen = choose_articles(paths.DEFAULT_LIBRARY_DB, args.articles, {source["directory"] for source in SOURCES})
    files = []
    for row in chosen:
        path = (root / row["source"] / row["filename"]).resolve()  # index: (site folder, file name)
        if not path.is_file() or not path.is_relative_to(root.resolve()):
            print(f"記事ファイルが見つかりません: {row['filename']}", file=sys.stderr)
            return 1
        files.append((path, path.relative_to(root.resolve()).as_posix()))
        print(f"  {row['source']:<12} {row['title'][:60]}")
    print(f"中継先: {relay_url}")
    if args.dry_run:
        print("（ドライラン: 合言葉の発行も ZIP の作成もしていません）")
        return 0
    stamp = datetime.now().astimezone().strftime("%Y%m%d-%H%M")
    token = relay.issue_token(f"テスト版 {stamp}", daily_chars=args.daily_chars)
    archive = build(Path(args.out), files, relay_url, token, args.articles, stamp)
    print(f"テスト版を作成しました: {archive}")
    print("合言葉の digest をこのインストールの設定に記録しました（設定画面の「テスト版への翻訳の中継」で無効化できます）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
