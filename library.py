"""SQLite index of saved articles.

The home page reads this index instead of opening every HTML file. Each saved
article is parsed once (and again only when its size or mtime changes); the
extracted title, lead, date and thumbnail are stored here. The HTML files stay
the source of truth, so the index can be deleted and rebuilt at any time.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from extractor import ALLOWED_EXTENSIONS, MAX_HTML_BYTES, ReaderError, extract_article

DEFAULT_DB_PATH = Path(__file__).resolve().parent / "instance" / "library.sqlite3"
LEAD_CHARS = 140
MIN_SYNC_INTERVAL_SECONDS = 30
_sync_lock = threading.Lock()
_last_sync: dict[str, float] = {}

SCHEMA = """
CREATE TABLE IF NOT EXISTS articles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    filename TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    url TEXT NOT NULL DEFAULT '',
    published TEXT NOT NULL DEFAULT '',
    lead TEXT NOT NULL DEFAULT '',
    thumb_src TEXT NOT NULL DEFAULT '',
    paragraph_count INTEGER NOT NULL DEFAULT 0,
    size INTEGER NOT NULL,
    mtime REAL NOT NULL,
    error TEXT NOT NULL DEFAULT '',
    UNIQUE(source, filename)
);
CREATE INDEX IF NOT EXISTS idx_articles_sort ON articles(published DESC, mtime DESC);
"""


def connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path, timeout=10)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA journal_mode=WAL")  # the web app reads while the collector writes
    except sqlite3.DatabaseError:
        pass
    db.executescript(SCHEMA)
    return db


def _scan(root: Path) -> dict[tuple[str, str], Path]:
    """Map (source, filename) -> path for ``root/<source>/*.html`` and ``root/*.html``."""
    found: dict[tuple[str, str], Path] = {}
    try:
        top = list(root.iterdir())
    except OSError as exc:
        raise ReaderError("記事ライブラリを読み取れません。") from exc
    for entry in top:
        if entry.is_symlink():
            continue
        if entry.is_file() and entry.suffix.lower() in ALLOWED_EXTENSIONS:
            found[("", entry.name)] = entry
        elif entry.is_dir():
            try:
                children = list(entry.iterdir())
            except OSError:
                continue
            for child in children:
                if child.is_file() and not child.is_symlink() and child.suffix.lower() in ALLOWED_EXTENSIONS:
                    found[(entry.name, child.name)] = child
    return found


def sync(root: Path, db_path: Path, force: bool = False) -> dict:
    """Bring the index in line with the files on disk. Cheap when nothing changed."""
    key = f"{db_path}|{root}"
    now = time.monotonic()
    if not force and now - _last_sync.get(key, -1e9) < MIN_SYNC_INTERVAL_SECONDS:
        return {"added": 0, "updated": 0, "removed": 0, "skipped": True}
    with _sync_lock:
        files = _scan(root)
        added = updated = 0
        with closing(connect(db_path)) as db:
            known = {(r["source"], r["filename"]): (r["size"], r["mtime"]) for r in db.execute("SELECT source, filename, size, mtime FROM articles")}
            for (source, filename), path in files.items():
                try:
                    stat = path.stat()
                except OSError:
                    continue
                previous = known.get((source, filename))
                if previous and previous[0] == stat.st_size and abs(previous[1] - stat.st_mtime) < 0.001:
                    continue
                record = _parse(path, stat)
                db.execute(
                    """INSERT INTO articles(source, filename, title, url, published, lead, thumb_src,
                                            paragraph_count, size, mtime, error)
                       VALUES(:source,:filename,:title,:url,:published,:lead,:thumb_src,
                              :paragraph_count,:size,:mtime,:error)
                       ON CONFLICT(source, filename) DO UPDATE SET
                         title=excluded.title, url=excluded.url, published=excluded.published,
                         lead=excluded.lead, thumb_src=excluded.thumb_src,
                         paragraph_count=excluded.paragraph_count, size=excluded.size,
                         mtime=excluded.mtime, error=excluded.error""",
                    {"source": source, "filename": filename, **record},
                )
                if previous:
                    updated += 1
                else:
                    added += 1
            gone = [k for k in known if k not in files]
            for source, filename in gone:
                db.execute("DELETE FROM articles WHERE source=? AND filename=?", (source, filename))
            db.commit()
        _last_sync[key] = now
        return {"added": added, "updated": updated, "removed": len(gone), "skipped": False}


def _parse(path: Path, stat) -> dict:
    base = {"title": path.stem, "url": "", "published": "", "lead": "", "thumb_src": "",
            "paragraph_count": 0, "size": stat.st_size, "mtime": stat.st_mtime, "error": ""}
    if stat.st_size > MAX_HTML_BYTES:
        return {**base, "error": "サイズ制限超過"}
    try:
        article = extract_article(path.read_bytes())
    except (ReaderError, OSError) as exc:
        return {**base, "error": str(exc)[:200]}
    lead = article["paragraphs"][0] if article["paragraphs"] else ""
    if len(lead) > LEAD_CHARS:
        lead = lead[:LEAD_CHARS].rstrip() + "…"
    images = article["images"]
    return {**base, "title": article["title"], "url": article["url"], "published": article["published"],
            "lead": lead, "thumb_src": images[0]["src"] if images else "",
            "paragraph_count": article["paragraph_count"]}


def sources(db_path: Path) -> list[dict]:
    with closing(connect(db_path)) as db:
        rows = db.execute("SELECT source, COUNT(*) AS n FROM articles WHERE error='' GROUP BY source ORDER BY source COLLATE NOCASE").fetchall()
    return [{"name": r["source"], "count": r["n"]} for r in rows]


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_articles(db_path: Path, source: str = "", query: str = "", page: int = 1, per_page: int = 24) -> dict:
    """Newest first. Articles without a parsed date fall back to file time."""
    where, params = ["error = ''"], []
    if source:
        where.append("source = ?")
        params.append(source)
    query = query.strip()[:100]
    if query:
        where.append("(title LIKE ? ESCAPE '\\' OR lead LIKE ? ESCAPE '\\')")
        params += [_like(query), _like(query)]
    clause = " AND ".join(where)
    page = max(1, page)
    with closing(connect(db_path)) as db:
        total = db.execute(f"SELECT COUNT(*) FROM articles WHERE {clause}", params).fetchone()[0]
        rows = db.execute(
            f"""SELECT * FROM articles WHERE {clause}
                ORDER BY CASE WHEN published = '' THEN 1 ELSE 0 END, published DESC, mtime DESC
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "pages": max(1, -(-total // per_page))}


def get(db_path: Path, article_id: int) -> dict | None:
    with closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM articles WHERE id = ?", (article_id,)).fetchone()
    return dict(row) if row else None


def find_by_file(db_path: Path, source: str, filename: str) -> dict | None:
    with closing(connect(db_path)) as db:
        row = db.execute("SELECT * FROM articles WHERE source = ? AND filename = ?", (source, filename)).fetchone()
    return dict(row) if row else None
