"""SQLite index of the saved-HTML library.

The saved HTML files are the source of truth; this index is a rebuildable cache
(D-03). It stores, per article, the list-card fields *and* the extracted body
(zlib-compressed JSON), so neither the list nor the article page ever re-parses
HTML (Phase 3). Keeping it in step with the files is ``sync``'s job, which runs
off the request path: in a background thread of the web app and right after
each collection (D-04).

Article ids are stable for the life of a file — bookmarks are ``/a/<id>`` — so
schema changes are migrations that keep rows, never rebuilds.
"""
from __future__ import annotations

import json
import logging
import sqlite3
import threading
import time
import zlib
from contextlib import closing
from pathlib import Path
from typing import Callable

from newsreader import paths
from newsreader.library.files import FileInfo, scan_library
from newsreader.sources import MAX_HTML_BYTES, PARSER_VERSION, ReaderError, extract_article

log = logging.getLogger("newsreader.library")

# Module-level so tests can redirect the index to a temporary database.
DEFAULT_DB_PATH = paths.DEFAULT_LIBRARY_DB
LEAD_CHARS = 140
MIN_SYNC_INTERVAL_SECONDS = 30
#: Files parsed between two short write transactions. Parsing happens with no lock
#: held, so a collector writing at the same time waits milliseconds, not seconds.
WRITE_BATCH = 24

_sync_lock = threading.Lock()
#: Per (db, root) monotonic time of the last sync; ``force=False`` calls inside
#: MIN_SYNC_INTERVAL_SECONDS are skipped. Kept as a module dict for v3 tests.
_last_sync: dict[str, float] = {}

# ---------------------------------------------------------------- schema ----

# The v3 schema, verbatim: a fresh database and an upgraded v3 database go through
# the same migrations, so there is exactly one way to arrive at the current schema.
_BASE_SCHEMA = """
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


def _add_column(db: sqlite3.Connection, table: str, column: str, ddl: str) -> None:
    existing = {row[1] for row in db.execute(f"PRAGMA table_info({table})")}
    if column not in existing:
        db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _migration_1(db: sqlite3.Connection) -> None:
    """v3 -> Phase 3: extracted body in the index, and a key saying what it was parsed from."""
    _add_column(db, "articles", "content", "BLOB")
    # "<size>:<mtime_ns>:<parser version>" of the file the content came from. NULL for rows
    # written by v3 (or by v3 again after a rollback), which forces a re-parse.
    _add_column(db, "articles", "content_key", "TEXT")
    db.execute("CREATE INDEX IF NOT EXISTS idx_articles_source ON articles(source, published DESC)")


def _migration_2(db: sqlite3.Connection) -> None:
    """Phase 7: same-news grouping. Rows cascade away with their article."""
    db.execute("""CREATE TABLE IF NOT EXISTS story_items (
        article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
        title TEXT NOT NULL,          -- the headline the pivot was made from (a changed title is re-pivoted)
        lang TEXT NOT NULL,
        pivot TEXT,                   -- English headline; NULL while waiting for translation
        pivot_chars INTEGER NOT NULL DEFAULT 0,
        pivot_at TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS story_members (
        article_id INTEGER PRIMARY KEY REFERENCES articles(id) ON DELETE CASCADE,
        story_key TEXT NOT NULL,
        score REAL NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS idx_story_members_key ON story_members(story_key)")
    db.execute("""CREATE TABLE IF NOT EXISTS story_usage (
        day TEXT PRIMARY KEY,         -- local calendar day of the PC
        chars INTEGER NOT NULL)""")


def _migration_3(db: sqlite3.Connection) -> None:
    """Reader corrections to same-news grouping: pairs that are different news (a < b)."""
    db.execute("""CREATE TABLE IF NOT EXISTS story_exclusions (
        a INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        b INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        created TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (a, b))""")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [_migration_1, _migration_2, _migration_3]
SCHEMA_VERSION = len(MIGRATIONS)
_schema_lock = threading.Lock()
_wal_checked: set[str] = set()


def _ensure_schema(db: sqlite3.Connection) -> bool:
    """Create or upgrade the schema; True when it had to. Cheap to call: one PRAGMA when current.

    Not "once per process" by a flag: if someone deletes the index file while the
    app runs (allowed — it is a cache), the next connection must rebuild it instead
    of failing with "no such table".
    """
    if db.execute("PRAGMA user_version").fetchone()[0] >= SCHEMA_VERSION:
        return False
    with _schema_lock:
        db.execute("BEGIN IMMEDIATE")
        try:
            version = db.execute("PRAGMA user_version").fetchone()[0]  # re-read under the lock
            if version < SCHEMA_VERSION:
                # Statement by statement, not executescript(): executescript() commits first,
                # which would end this transaction and let two processes migrate at once.
                for statement in filter(None, (s.strip() for s in _BASE_SCHEMA.split(";"))):
                    db.execute(statement)
                for number, migrate in enumerate(MIGRATIONS, start=1):
                    if number > version:
                        migrate(db)
                db.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
    return True


def connect(db_path: Path) -> sqlite3.Connection:
    """A connection with the schema guaranteed current. Callers close it (``with closing(...)``)."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # isolation_level=None: transactions are explicit (BEGIN IMMEDIATE for writes) so a
    # reader never silently upgrades to a writer half-way and deadlocks with the collector.
    db = sqlite3.connect(db_path, timeout=10, isolation_level=None)
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA foreign_keys=ON")  # per-connection setting
        rebuilt = _ensure_schema(db)
        key = str(db_path.resolve())
        if rebuilt or key not in _wal_checked:
            # WAL is a property of the database file, so it is set once per process and
            # again whenever the file was (re)created by _ensure_schema's slow path.
            try:
                db.execute("PRAGMA journal_mode=WAL")  # the web app reads while the collector writes
                _wal_checked.add(key)
            except sqlite3.OperationalError:
                pass  # another connection holds a lock right now; the next connection retries
        db.execute("PRAGMA synchronous=NORMAL")  # safe with WAL; the index is a rebuildable cache anyway
    except BaseException:
        db.close()
        raise
    return db


# --------------------------------------------------------------- content ----

_CONTENT_FIELDS = ("kind", "page_type", "paragraphs", "images")
_LIST_COLUMNS = ("id, source, filename, title, url, published, lead, thumb_src, "
                 "paragraph_count, size, mtime, error")


def encode_content(article: dict) -> bytes:
    payload = {field: article[field] for field in _CONTENT_FIELDS}
    return zlib.compress(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"), 6)


def decode_content(blob: bytes, row) -> dict | None:
    """Rebuild the exact ``extract_article`` dictionary from a row; None if the blob is unusable."""
    try:
        data = json.loads(zlib.decompress(blob).decode("utf-8"))
        paragraphs = data["paragraphs"]
        return {
            "title": row["title"],
            "kind": data["kind"],
            "page_type": data["page_type"],
            "paragraphs": paragraphs,
            "paragraph_count": len(paragraphs),
            "body": "\n\n".join(paragraphs),
            "url": row["url"],
            "images": data["images"],
            "published": row["published"],
        }
    except (zlib.error, ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


def _content_key(size: int, mtime_ns: int) -> str:
    return f"{size}:{mtime_ns}:{PARSER_VERSION}"


def _lead(paragraphs: list[str]) -> str:
    lead = paragraphs[0] if paragraphs else ""
    if len(lead) > LEAD_CHARS:
        lead = lead[:LEAD_CHARS].rstrip() + "…"
    return lead


def _parse_file(info: FileInfo) -> dict | None:
    """One row's worth of fields, or None when the file changed while being read (retry later)."""
    base = {"title": info.path.stem, "url": "", "published": "", "lead": "", "thumb_src": "",
            "paragraph_count": 0, "size": info.size, "mtime": info.mtime, "error": "", "content": None,
            "content_key": _content_key(info.size, info.mtime_ns)}
    if info.size > MAX_HTML_BYTES:
        return {**base, "error": "サイズ制限超過"}
    try:
        data = info.path.read_bytes()
        after = info.path.stat()
    except OSError:
        return None  # vanished / locked (e.g. antivirus on Windows): next sync retries
    if after.st_size != info.size or after.st_mtime_ns != info.mtime_ns or len(data) != info.size:
        return None  # being written right now; never store content under the wrong key
    try:
        article = extract_article(data)
    except ReaderError as exc:
        return {**base, "error": str(exc)[:200]}
    except Exception as exc:  # one pathological file must not stop the whole library from indexing
        log.warning("Could not parse %s (%s)", info.path.name, type(exc).__name__)
        return {**base, "error": f"解析できませんでした（{type(exc).__name__}）"}
    images = article["images"]
    return {**base, "title": article["title"], "url": article["url"], "published": article["published"],
            "lead": _lead(article["paragraphs"]), "thumb_src": images[0]["src"] if images else "",
            "paragraph_count": article["paragraph_count"], "content": encode_content(article)}


_UPSERT = """
INSERT INTO articles(source, filename, title, url, published, lead, thumb_src, paragraph_count,
                     size, mtime, error, content, content_key)
VALUES(:source, :filename, :title, :url, :published, :lead, :thumb_src, :paragraph_count,
       :size, :mtime, :error, :content, :content_key)
ON CONFLICT(source, filename) DO UPDATE SET
  title=excluded.title, url=excluded.url, published=excluded.published, lead=excluded.lead,
  thumb_src=excluded.thumb_src, paragraph_count=excluded.paragraph_count, size=excluded.size,
  mtime=excluded.mtime, error=excluded.error, content=excluded.content, content_key=excluded.content_key
"""


def _write(db: sqlite3.Connection, statements: list[tuple[str, dict | tuple]]) -> None:
    if not statements:
        return
    db.execute("BEGIN IMMEDIATE")
    try:
        for sql, params in statements:
            db.execute(sql, params)
        db.execute("COMMIT")
    except BaseException:
        db.execute("ROLLBACK")
        raise


# ------------------------------------------------------------------ sync ----

_state_lock = threading.Lock()
_state: dict[str, dict] = {}


def _set_state(db_path: Path, **values) -> None:
    with _state_lock:
        _state.setdefault(str(db_path), {}).update(values)


def sync_state(db_path: Path) -> dict:
    """What the last/current sync in *this process* did (for the list page and /status)."""
    with _state_lock:
        return dict(_state.get(str(db_path), {}))


def sync(root: Path, db_path: Path, force: bool = False) -> dict:
    """Bring the index in line with the files on disk. Cheap when nothing changed.

    Only new files, files whose size/mtime changed, rows parsed by an older parser
    and rows without stored content are parsed. Rows whose file disappeared are
    deleted — except when *every* file disappeared at once, which looks like a
    missing drive or a mistyped HTML_DIRECTORY rather than a decision to delete
    the whole library; then the index is kept and the state says why.
    """
    key = f"{db_path}|{root}"
    now = time.monotonic()
    if not force and now - _last_sync.get(key, -1e9) < MIN_SYNC_INTERVAL_SECONDS:
        return {"added": 0, "updated": 0, "removed": 0, "errors": 0, "deferred": 0, "skipped": True}
    with _sync_lock:
        started = time.perf_counter()
        _set_state(db_path, running=True, started=time.time(), progress=[0, 0], warning="")
        try:
            result = _sync_locked(Path(root), Path(db_path))
        except Exception as exc:
            _set_state(db_path, running=False, finished=time.time(), error=str(exc) if isinstance(exc, ReaderError)
                       else f"索引の更新に失敗しました（{type(exc).__name__}）")
            raise
        result["seconds"] = round(time.perf_counter() - started, 3)
        _last_sync[key] = now
        _set_state(db_path, running=False, finished=time.time(), result=result, error="")
        return result


def _sync_locked(root: Path, db_path: Path) -> dict:
    with closing(connect(db_path)) as db:
        # Read the index *before* scanning: a file the collector adds in between is then
        # simply not seen yet, instead of its fresh row being mistaken for a deleted file.
        known = {(r["source"], r["filename"]): (r["content_key"], r["content"] is not None, r["error"])
                 for r in db.execute("SELECT source, filename, content_key, content, error FROM articles")}
        files = scan_library(root)

        gone = [k for k in known if k not in files]
        warning = ""
        if gone and not files:
            warning = "記事フォルダにHTMLが1件もないため、誤設定の可能性を考えて索引を保持しました。"
            log.warning("Library root %s is empty; keeping %d indexed rows", root, len(gone))
            gone = []
        # Re-check just before deleting: never drop a row whose file is back.
        gone = [k for k in gone if not ((root / k[0] / k[1]) if k[0] else (root / k[1])).is_file()]
        _write(db, [("DELETE FROM articles WHERE source=? AND filename=?", k) for k in gone])

        def stale(item) -> bool:
            (source, filename), info = item
            previous = known.get((source, filename))
            if previous is None:
                return True
            content_key, has_content, error = previous
            if content_key != _content_key(info.size, info.mtime_ns):
                return True
            return not has_content and not error  # migrated v3 row: parse once to fill content

        work = sorted(filter(stale, files.items()))
        added = updated = errors = deferred = 0
        _set_state(db_path, progress=[0, len(work)], warning=warning)
        batch: list[tuple[str, dict]] = []
        for done, ((source, filename), info) in enumerate(work, start=1):
            row = _parse_file(info)
            if row is None:
                deferred += 1
            else:
                batch.append((_UPSERT, {"source": source, "filename": filename, **row}))
                errors += bool(row["error"])
                if (source, filename) in known:
                    updated += 1
                else:
                    added += 1
            if len(batch) >= WRITE_BATCH:
                _write(db, batch)
                batch = []
                _set_state(db_path, progress=[done, len(work)])
        _write(db, batch)
        _set_state(db_path, progress=[len(work), len(work)])
        total = db.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
    return {"added": added, "updated": updated, "removed": len(gone), "errors": errors,
            "deferred": deferred, "total": total, "skipped": False, "warning": warning}


class BackgroundSync:
    """Keeps the index current from a daemon thread: once at start, then every ``interval`` seconds.

    The library root is resolved on every pass (``root_provider``), so a folder that
    appears after start-up (an external drive, say) is picked up without a restart.
    """

    def __init__(self, root_provider: Callable[[], Path], db_path: Path, interval: float = 60.0,
                 after_change: Callable[[dict], None] | None = None):
        self.root_provider = root_provider
        self.after_change = after_change
        self._first = True
        self.db_path = Path(db_path)
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="library-sync", daemon=True)

    def start(self) -> "BackgroundSync":
        self.thread.start()
        return self

    def trigger(self) -> None:
        """Run the next pass now instead of waiting for the interval."""
        self._wake.set()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        self.thread.join(timeout)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                result = sync(self.root_provider(), self.db_path, force=True)
                changed = result["added"] or result["updated"] or result["removed"]
                if self.after_change is not None and (changed or self._first):
                    self._first = False
                    try:
                        self.after_change(result)
                    except Exception:
                        log.exception("After-sync hook failed")
            except ReaderError as exc:  # e.g. the folder is missing: shown on the page, retried later
                _set_state(self.db_path, running=False, error=str(exc), finished=time.time())
            except Exception:
                log.exception("Background library sync failed")
            self._wake.wait(self.interval)
            self._wake.clear()


def start_background_sync(root_provider: Callable[[], Path], db_path: Path, interval: float = 60.0,
                          after_change: Callable[[dict], None] | None = None) -> BackgroundSync:
    return BackgroundSync(root_provider, db_path, interval, after_change).start()


# --------------------------------------------------------------- queries ----

def sources(db_path: Path) -> list[dict]:
    with closing(connect(db_path)) as db:
        rows = db.execute("SELECT source, COUNT(*) AS n FROM articles WHERE error='' "
                          "GROUP BY source ORDER BY source COLLATE NOCASE").fetchall()
    return [{"name": r["source"], "count": r["n"]} for r in rows]


def source_stats(db_path: Path) -> list[dict]:
    """Per-site counts, newest publication time, stored HTML bytes and unparseable files (for /status)."""
    with closing(connect(db_path)) as db:
        rows = db.execute(
            """SELECT source, COUNT(*) AS total, SUM(error = '') AS ok, SUM(error != '') AS errors,
                      MAX(CASE WHEN error = '' THEN published END) AS newest, SUM(size) AS bytes
               FROM articles GROUP BY source ORDER BY source COLLATE NOCASE""").fetchall()
    return [{"name": r["source"], "total": r["total"], "ok": r["ok"] or 0, "errors": r["errors"] or 0,
             "newest": r["newest"] or "", "bytes": r["bytes"] or 0} for r in rows]


def index_stats(db_path: Path) -> dict:
    """Totals for /status: rows, rows still waiting for content, and the index file size."""
    with closing(connect(db_path)) as db:
        total, pending = db.execute(
            "SELECT COUNT(*), SUM(content IS NULL AND error = '') FROM articles").fetchone()
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += Path(str(db_path) + suffix).stat().st_size
        except OSError:
            pass
    return {"total": total, "pending": pending or 0, "bytes": size}


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def list_articles(db_path: Path, source: str = "", query: str = "", page: int = 1, per_page: int = 24) -> dict:
    """Newest first. Articles without a parsed date fall back to file time. Never loads bodies."""
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
            f"""SELECT {_LIST_COLUMNS} FROM articles WHERE {clause}
                ORDER BY CASE WHEN published = '' THEN 1 ELSE 0 END, published DESC, mtime DESC
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page,
            "pages": max(1, -(-total // per_page))}


def get(db_path: Path, article_id: int) -> dict | None:
    """The list-card fields of one article (no body)."""
    with closing(connect(db_path)) as db:
        row = db.execute(f"SELECT {_LIST_COLUMNS} FROM articles WHERE id = ?", (article_id,)).fetchone()
    return dict(row) if row else None


def get_content(db_path: Path, article_id: int) -> dict | None:
    """The full ``extract_article`` dictionary for one article, from the index alone.

    None when the row is missing or its content is not (yet) available — a row
    migrated from v3 before its first background parse, for example.
    """
    with closing(connect(db_path)) as db:
        row = db.execute("SELECT title, url, published, content FROM articles WHERE id = ? AND error = ''",
                         (article_id,)).fetchone()
    if row is None or row["content"] is None:
        return None
    return decode_content(row["content"], row)


def find_by_file(db_path: Path, source: str, filename: str) -> dict | None:
    with closing(connect(db_path)) as db:
        row = db.execute(f"SELECT {_LIST_COLUMNS} FROM articles WHERE source = ? AND filename = ?",
                         (source, filename)).fetchone()
    return dict(row) if row else None
