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

import copy
import functools
import json
import logging
import os
import re
import sqlite3
import threading
import time
import zlib
from collections.abc import Callable
from contextlib import closing
from pathlib import Path

from newsreader import paths
from newsreader.library import genres
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
#: The background sync looks at every file at least this often even when neither the library's
#: folders nor the database changed (an HTML file rewritten in place changes neither).
FULL_PASS_SECONDS = 600.0
#: A read kept by ``kept_until_changed`` is used for at most this long, even when the database
#: looks unchanged (a coarse file clock could hide a write).
KEEP_SECONDS = 60.0
#: Answers kept per read before all of them are dropped.
KEEP_ENTRIES = 128

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


def _migration_4(db: sqlite3.Connection) -> None:
    """読み比べの数字 (D-59): per news group, the figures compared across its outlets."""
    db.execute("""CREATE TABLE IF NOT EXISTS story_facts (
        story_key TEXT PRIMARY KEY,
        members TEXT NOT NULL,        -- the article ids compared, sorted, comma-separated
        newest TEXT NOT NULL DEFAULT '',
        differs INTEGER NOT NULL DEFAULT 0,
        cohesive INTEGER NOT NULL DEFAULT 1,  -- every member close to the rest (not a loose chain)
        summary TEXT NOT NULL DEFAULT '[]',
        computed TEXT NOT NULL DEFAULT '')""")
    db.execute("""CREATE TABLE IF NOT EXISTS facts_usage (
        day TEXT PRIMARY KEY,
        chars INTEGER NOT NULL)""")


def _migration_5(db: sqlite3.Connection) -> None:
    """Reader corrections the other way (D-72): pairs that ARE the same news (a < b)."""
    db.execute("""CREATE TABLE IF NOT EXISTS story_joins (
        a INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        b INTEGER NOT NULL REFERENCES articles(id) ON DELETE CASCADE,
        created TEXT NOT NULL DEFAULT '',
        PRIMARY KEY (a, b))""")


def _migration_6(db: sqlite3.Connection) -> None:
    """The kind of news (genres.py): filled as each file is parsed again (PARSER_VERSION 4)."""
    _add_column(db, "articles", "genre", "TEXT NOT NULL DEFAULT ''")


MIGRATIONS: list[Callable[[sqlite3.Connection], None]] = [
    _migration_1,
    _migration_2,
    _migration_3,
    _migration_4,
    _migration_5,
    _migration_6,
]
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
_LIST_COLUMNS = (
    "id, source, filename, title, url, published, lead, thumb_src, paragraph_count, size, mtime, error, genre"
)


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


def _parse_file(info: FileInfo, source: str = "") -> dict | None:
    """One row's worth of fields, or None when the file changed while being read (retry later)."""
    base = {
        "title": info.path.stem,
        "url": "",
        "published": "",
        "lead": "",
        "thumb_src": "",
        "genre": "",
        "paragraph_count": 0,
        "size": info.size,
        "mtime": info.mtime,
        "error": "",
        "content": None,
        "content_key": _content_key(info.size, info.mtime_ns),
    }
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
    genre = genres.classify(
        source, article["url"], genres.section_of(data), article["title"], article["paragraphs"][:6]
    )
    return {
        **base,
        "title": article["title"],
        "url": article["url"],
        "published": article["published"],
        "genre": genre,
        "lead": _lead(article["paragraphs"]),
        "thumb_src": images[0]["src"] if images else "",
        "paragraph_count": article["paragraph_count"],
        "content": encode_content(article),
    }


_UPSERT = """
INSERT INTO articles(source, filename, title, url, published, lead, thumb_src, paragraph_count,
                     size, mtime, error, content, content_key, genre)
VALUES(:source, :filename, :title, :url, :published, :lead, :thumb_src, :paragraph_count,
       :size, :mtime, :error, :content, :content_key, :genre)
ON CONFLICT(source, filename) DO UPDATE SET
  title=excluded.title, url=excluded.url, published=excluded.published, lead=excluded.lead,
  thumb_src=excluded.thumb_src, paragraph_count=excluded.paragraph_count, size=excluded.size,
  mtime=excluded.mtime, error=excluded.error, content=excluded.content, content_key=excluded.content_key,
  genre=excluded.genre
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
            _set_state(
                db_path,
                running=False,
                finished=time.time(),
                error=str(exc) if isinstance(exc, ReaderError) else f"索引の更新に失敗しました（{type(exc).__name__}）",
            )
            raise
        result["seconds"] = round(time.perf_counter() - started, 3)
        _last_sync[key] = now
        _set_state(db_path, running=False, finished=time.time(), result=result, error="")
        return result


def _sync_locked(root: Path, db_path: Path) -> dict:
    with closing(connect(db_path)) as db:
        # Read the index *before* scanning: a file the collector adds in between is then
        # simply not seen yet, instead of its fresh row being mistaken for a deleted file.
        # content IS NOT NULL, not the content itself: every article's body is not read for a yes/no.
        known = {
            (r["source"], r["filename"]): (r["content_key"], bool(r["has_content"]), r["error"])
            for r in db.execute(
                "SELECT source, filename, content_key, content IS NOT NULL AS has_content, error FROM articles"
            )
        }
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
            row = _parse_file(info, source)
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
    return {
        "added": added,
        "updated": updated,
        "removed": len(gone),
        "errors": errors,
        "deferred": deferred,
        "total": total,
        "skipped": False,
        "warning": warning,
    }


class BackgroundSync:
    """Keeps the index current from a daemon thread: once at start, then every ``interval`` seconds.

    The library root is resolved on every pass (``root_provider``), so a folder that
    appears after start-up (an external drive, say) is picked up without a restart.
    """

    def __init__(
        self,
        root_provider: Callable[[], Path],
        db_path: Path,
        interval: float = 60.0,
        after_change: Callable[[dict], None] | None = None,
    ):
        self.root_provider = root_provider
        self.after_change = after_change
        self._first = True
        self.db_path = Path(db_path)
        self.interval = interval
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._run, name="library-sync", daemon=True)

    def start(self) -> BackgroundSync:
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
        looked: tuple | None = None
        full = 0.0
        asked = False
        while not self._stop.is_set():
            try:
                root = self.root_provider()
                # Nothing new in the library's folders and no write to the index (the collector indexes
                # what it saves itself): no pass, so an idle minute costs a few stat calls, not a read
                # of every row and every file (a small server's whole CPU, every minute). A pass
                # asked for (trigger) always runs.
                now = (folders_stamp(root), db_stamp(self.db_path))
                if now == looked and time.monotonic() - full < FULL_PASS_SECONDS and not asked:
                    asked = self._wake.wait(self.interval)
                    self._wake.clear()
                    continue
                result = sync(root, self.db_path, force=True)
                looked, full = (folders_stamp(root), db_stamp(self.db_path)), time.monotonic()
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
            asked = self._wake.wait(self.interval)
            self._wake.clear()


def start_background_sync(
    root_provider: Callable[[], Path],
    db_path: Path,
    interval: float = 60.0,
    after_change: Callable[[dict], None] | None = None,
) -> BackgroundSync:
    return BackgroundSync(root_provider, db_path, interval, after_change).start()


def folders_stamp(root: Path) -> tuple:
    """The library folder and its site folders as they are now: a file saved, renamed or removed
    in one of them changes it (the folder's own time). () when the folder cannot be read.

    Each site folder is stat'ed itself: on Windows ``DirEntry.stat()`` answers from the parent's
    listing, which NTFS updates late, so a new article in a site folder went unseen there."""
    try:
        with os.scandir(root) as entries:
            folders = [entry for entry in entries if entry.is_dir(follow_symlinks=False)]
        found = [(entry.name, os.stat(entry.path, follow_symlinks=False).st_mtime_ns) for entry in folders]
        return (os.stat(root).st_mtime_ns, tuple(sorted(found)))
    except OSError:
        return ()


def db_stamp(db_path: Path) -> tuple:
    """The index file and its write-ahead log as they are now: a commit by any process changes it.

    An empty log is left out: SQLite makes a new, empty one whenever a connection opens after the
    last one closed, which is no change to the data."""
    try:
        main = os.stat(db_path)
    except OSError:
        return ()
    try:
        wal = os.stat(f"{db_path}-wal")
        log_file = (wal.st_mtime_ns, wal.st_size) if wal.st_size else ()
    except OSError:
        log_file = ()
    return (main.st_ino, main.st_mtime_ns, main.st_size, log_file)


def _frozen(value):
    if isinstance(value, (list, tuple)):
        return tuple(_frozen(item) for item in value)
    if isinstance(value, dict):
        return tuple(sorted((key, _frozen(item)) for key, item in value.items()))
    return value


def kept_until_changed(read):
    """For a read of the index that depends on its arguments alone: the answer is kept until the
    index changes (``db_stamp``, at most KEEP_SECONDS), so a list page asked for again runs none of
    its counting queries. Every caller gets its own copy to change as it likes."""
    kept: dict = {}

    @functools.wraps(read)
    def kept_read(db_path, *args, **kwargs):
        key = (str(db_path), _frozen(args), _frozen(kwargs))
        stamp = db_stamp(db_path)
        hit = kept.get(key)
        if hit is not None and hit[0] == stamp and stamp and time.monotonic() - hit[1] < KEEP_SECONDS:
            return copy.deepcopy(hit[2])
        value = read(db_path, *args, **kwargs)
        if len(kept) >= KEEP_ENTRIES:
            kept.clear()
        kept[key] = (stamp, time.monotonic(), value)
        return copy.deepcopy(value)

    kept_read.forget = kept.clear
    return kept_read


# --------------------------------------------------------------- queries ----


def _counts(
    db: sqlite3.Connection, column: str, query: str, pivot_words: list[str] | None, where: str = "", params: tuple = ()
) -> tuple[list[str], dict[str, int]]:
    """Every value of ``column`` (so chips never disappear) and how many articles have it,
    within a search and the extra condition."""
    values = [
        r[0]
        for r in db.execute(f"SELECT DISTINCT {column} FROM articles WHERE error='' ORDER BY {column} COLLATE NOCASE")
    ]
    counts: dict[str, int] = {}
    condition = f" AND {where}" if where else ""
    query = query.strip()[:100]
    if query:
        ids = search_ids(db, query, pivot_words)
        for start in range(0, len(ids), 900):
            chunk = ids[start : start + 900]
            for value, n in db.execute(
                f"SELECT {column}, COUNT(*) FROM articles WHERE id IN ({','.join('?' * len(chunk))})"
                f"{condition} GROUP BY {column}",
                (*chunk, *params),
            ):
                counts[value] = counts.get(value, 0) + n
    else:
        counts = dict(
            db.execute(
                f"SELECT {column}, COUNT(*) FROM articles WHERE error=''{condition} GROUP BY {column}", params
            ).fetchall()
        )
    return values, counts


@kept_until_changed
def sources(db_path: Path, query: str = "", pivot_words: list[str] | None = None, genre: str = "") -> list[dict]:
    """Outlets with their article counts — with a search or a kind of news, how many of each
    match it (every outlet is still listed, so the chips do not jump around)."""
    with closing(connect(db_path)) as db:
        names, counts = _counts(
            db,
            "source",
            query,
            pivot_words,
            "genre = ?" if genre else "",
            ("" if genre == genres.OTHER else genre,) if genre else (),
        )
    return [{"name": name, "count": counts.get(name, 0)} for name in names]


@kept_until_changed
def genre_counts(
    db_path: Path, source: str = "", query: str = "", pivot_words: list[str] | None = None
) -> dict[str, int]:
    """How many articles of each kind of news (genres.py) — within an outlet and a search."""
    with closing(connect(db_path)) as db:
        _, counts = _counts(
            db, "genre", query, pivot_words, "source = ?" if source else "", (source,) if source else ()
        )
    return counts


def source_stats(db_path: Path) -> list[dict]:
    """Per-site counts, newest publication time, stored HTML bytes and unparseable files (for /status)."""
    with closing(connect(db_path)) as db:
        rows = db.execute(
            """SELECT source, COUNT(*) AS total, SUM(error = '') AS ok, SUM(error != '') AS errors,
                      MAX(CASE WHEN error = '' THEN published END) AS newest, SUM(size) AS bytes
               FROM articles GROUP BY source ORDER BY source COLLATE NOCASE"""
        ).fetchall()
    return [
        {
            "name": r["source"],
            "total": r["total"],
            "ok": r["ok"] or 0,
            "errors": r["errors"] or 0,
            "newest": r["newest"] or "",
            "bytes": r["bytes"] or 0,
        }
        for r in rows
    ]


def index_stats(db_path: Path) -> dict:
    """Totals for /status: rows, rows still waiting for content, and the index file size."""
    with closing(connect(db_path)) as db:
        total, pending = db.execute("SELECT COUNT(*), SUM(content IS NULL AND error = '') FROM articles").fetchone()
    size = 0
    for suffix in ("", "-wal"):
        try:
            size += Path(str(db_path) + suffix).stat().st_size
        except OSError:
            pass
    return {"total": total, "pending": pending or 0, "bytes": size}


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


#: The order of the article lists (newest first; undated articles last, by file time). The id
#: only settles exact ties so that "next article" below follows the list exactly.
_LIST_ORDER = "CASE WHEN published = '' THEN 1 ELSE 0 END, published DESC, mtime DESC, id DESC"


_SEARCH_STOP = frozenset(
    "the and for with from into over after about than that this what when where who why how "
    "are was were has have had its his her their our not but says said will would can could "
    "new news".split()
)


def search_words(text: str) -> list[str]:
    """Lower-case English words of a search (at most 6) for matching the English headlines that
    same-news grouping keeps for every article (story_items.pivot)."""
    words: list[str] = []
    for raw in re.findall(r"[A-Za-z][A-Za-z0-9'’-]*|\d{2,}", text):
        word = re.sub(r"['’]s$", "", raw.lower()).strip("'’-")
        if len(word) > 4 and word.endswith("s") and not word.endswith("ss"):
            word = word[:-1]
        if len(word) >= 3 and word not in _SEARCH_STOP and word not in words:
            words.append(word)
    return words[:6]


#: At most this many articles are found through their English headline (an SQL parameter list).
MAX_PIVOT_MATCHES = 900
#: Highest page number a list goes to: past it the offset would not fit SQLite's integers
#: (``?page=99999999999999999999`` was a server error).
MAX_PAGE = 100_000


def _pivot_matches(db: sqlite3.Connection, words: list[str]) -> list[int]:
    """Articles whose English headline has the words at word starts ("strike" finds "strikes").
    One or two words must all be there; of more, two thirds."""
    patterns = [re.compile(r"(?<![A-Za-z0-9])" + re.escape(word), re.IGNORECASE) for word in words]
    need = len(patterns) if len(patterns) <= 2 else -(-2 * len(patterns) // 3)
    found = []
    for article_id, pivot in db.execute(
        "SELECT article_id, pivot FROM story_items WHERE pivot IS NOT NULL ORDER BY article_id DESC"
    ):
        if sum(1 for pattern in patterns if pattern.search(pivot)) >= need:
            found.append(article_id)
            if len(found) >= MAX_PIVOT_MATCHES:
                break
    return found


def search_ids(db: sqlite3.Connection, query: str, pivot_words: list[str] | None) -> list[int]:
    """Articles whose headline or opening contains ``query``, or whose English headline matches
    ``pivot_words`` (at most MAX_PIVOT_MATCHES of the latter)."""
    ids = [
        row[0]
        for row in db.execute(
            "SELECT id FROM articles WHERE error = '' AND (title LIKE ? ESCAPE '\\' OR lead LIKE ? ESCAPE '\\')",
            (_like(query), _like(query)),
        )
    ]
    return list(dict.fromkeys(ids + (_pivot_matches(db, pivot_words) if pivot_words else [])))


@kept_until_changed
def list_articles(
    db_path: Path,
    source: str = "",
    query: str = "",
    page: int = 1,
    per_page: int = 24,
    pivot_words: list[str] | None = None,
    genre: str = "",
) -> dict:
    """Newest first. Articles without a parsed date fall back to file time. Never loads bodies.

    ``pivot_words`` (from ``search_words``) also finds articles whose English headline matches,
    so an English (or translated) search reaches headlines written in Chinese or Korean."""
    where, params = ["error = ''"], []
    if source:
        where.append("source = ?")
        params.append(source)
    if genre:
        where.append("genre = ?")
        params.append("" if genre == genres.OTHER else genre)
    query = query.strip()[:100]
    page = min(max(1, page), MAX_PAGE)
    with closing(connect(db_path)) as db:
        if query:
            matches = _pivot_matches(db, pivot_words) if pivot_words else []
            extra = f" OR id IN ({','.join('?' * len(matches))})" if matches else ""
            where.append(f"(title LIKE ? ESCAPE '\\' OR lead LIKE ? ESCAPE '\\'{extra})")
            params += [_like(query), _like(query), *matches]
        clause = " AND ".join(where)
        total = db.execute(f"SELECT COUNT(*) FROM articles WHERE {clause}", params).fetchone()[0]
        rows = db.execute(
            f"""SELECT {_LIST_COLUMNS} FROM articles WHERE {clause}
                ORDER BY {_LIST_ORDER}
                LIMIT ? OFFSET ?""",
            params + [per_page, (page - 1) * per_page],
        ).fetchall()
    return {"items": [dict(r) for r in rows], "total": total, "page": page, "pages": max(1, -(-total // per_page))}


def neighbours(db_path: Path, article_id: int) -> dict:
    """The articles just before ("newer") and after ("older") this one in its outlet's list."""
    with closing(connect(db_path)) as db:
        row = db.execute("SELECT source FROM articles WHERE id = ?", (article_id,)).fetchone()
        if row is None:
            return {"newer": None, "older": None}
        ids = [
            r[0]
            for r in db.execute(
                f"SELECT id FROM articles WHERE error = '' AND source = ? ORDER BY {_LIST_ORDER}", (row["source"],)
            )
        ]
        if article_id not in ids:
            return {"newer": None, "older": None}
        at = ids.index(article_id)
        wanted = {"newer": ids[at - 1] if at > 0 else None, "older": ids[at + 1] if at + 1 < len(ids) else None}
        found = {
            r["id"]: dict(r)
            for r in db.execute(
                f"SELECT {_LIST_COLUMNS} FROM articles WHERE id IN (?, ?)", (wanted["newer"] or 0, wanted["older"] or 0)
            )
        }
    return {side: found.get(i) if i else None for side, i in wanted.items()}


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
        row = db.execute(
            "SELECT title, url, published, content FROM articles WHERE id = ? AND error = ''", (article_id,)
        ).fetchone()
    if row is None or row["content"] is None:
        return None
    return decode_content(row["content"], row)


def find_by_file(db_path: Path, source: str, filename: str) -> dict | None:
    with closing(connect(db_path)) as db:
        row = db.execute(
            f"SELECT {_LIST_COLUMNS} FROM articles WHERE source = ? AND filename = ?", (source, filename)
        ).fetchone()
    return dict(row) if row else None
