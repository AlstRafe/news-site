"""What the reader did with the articles: read, saved for later, and the words they kept.

Kept in its own database beside the index (``reader.sqlite3``), because the index can be rebuilt
from the article files at any time and this cannot. Articles are named by outlet and file name
(``source/filename``), which survive a rebuild; ids are looked up in the index when shown. One
server serves the PC and the phone, so what is saved on one is there on the other.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

_SCHEMA = """
CREATE TABLE IF NOT EXISTS marks (
    key TEXT PRIMARY KEY, source TEXT NOT NULL, filename TEXT NOT NULL, title TEXT NOT NULL DEFAULT '',
    read_at TEXT NOT NULL DEFAULT '', saved_at TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS marks_saved ON marks(saved_at);
CREATE TABLE IF NOT EXISTS words (
    id INTEGER PRIMARY KEY AUTOINCREMENT, word TEXT NOT NULL, target TEXT NOT NULL, meaning TEXT NOT NULL DEFAULT '',
    sentence TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT '', article_key TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '', added TEXT NOT NULL, UNIQUE(word, target));
"""
#: Read marks older than this many are forgotten (the list only needs the recent ones).
MAX_READ_MARKS = 20_000
MAX_WORDS = 5_000
_lock = threading.Lock()


def path_for(library_db: Path) -> Path:
    return Path(library_db).parent / "reader.sqlite3"


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def key_of(record: dict) -> str:
    return f"{record.get('source') or ''}/{record.get('filename') or ''}"


def _upsert(db, record: dict, column: str, value: str) -> None:
    db.execute(f"""INSERT INTO marks (key, source, filename, title, {column}) VALUES (?, ?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET {column} = excluded.{column}, title = excluded.title""",
               (key_of(record), record.get("source") or "", record.get("filename") or "", record.get("title") or "", value))


def mark_read(path: Path, record: dict) -> None:
    with _lock, closing(_connect(path)) as db, db:
        _upsert(db, record, "read_at", _now())
        count = db.execute("SELECT COUNT(*) FROM marks WHERE read_at != ''").fetchone()[0]
        if count > MAX_READ_MARKS:
            db.execute("""UPDATE marks SET read_at = '' WHERE key IN (SELECT key FROM marks WHERE read_at != ''
                          AND saved_at = '' ORDER BY read_at LIMIT ?)""", (count - MAX_READ_MARKS,))
            db.execute("DELETE FROM marks WHERE read_at = '' AND saved_at = ''")


def set_saved(path: Path, record: dict, on: bool) -> None:
    with _lock, closing(_connect(path)) as db, db:
        _upsert(db, record, "saved_at", _now() if on else "")
        db.execute("DELETE FROM marks WHERE read_at = '' AND saved_at = ''")


def states(path: Path, records: list[dict]) -> dict[int, dict]:
    """{article id: {"read": bool, "saved": bool}} for the given index records."""
    if not records or not Path(path).exists():
        return {}
    keys = {key_of(r): r["id"] for r in records}
    with closing(_connect(path)) as db:
        rows = db.execute(f"SELECT key, read_at, saved_at FROM marks WHERE key IN ({','.join('?' * len(keys))})",
                          list(keys)).fetchall()
    return {keys[row["key"]]: {"read": bool(row["read_at"]), "saved": bool(row["saved_at"])} for row in rows}


def saved(path: Path) -> list[dict]:
    """Saved articles, newest saved first: source, filename, title, saved_at, read."""
    if not Path(path).exists():
        return []
    with closing(_connect(path)) as db:
        return [dict(row) | {"read": bool(row["read_at"])} for row in db.execute(
            "SELECT key, source, filename, title, saved_at, read_at FROM marks WHERE saved_at != '' ORDER BY saved_at DESC")]


# ---- 単語帳 ------------------------------------------------------------------------------

def add_word(path: Path, word: str, target: str, meaning: str, sentence: str, record: dict | None) -> None:
    word, meaning, sentence = word.strip()[:80], meaning.strip()[:400], sentence.strip()[:600]
    if not word:
        return
    record = record or {}
    with _lock, closing(_connect(path)) as db, db:
        db.execute("""INSERT INTO words (word, target, meaning, sentence, source, article_key, title, added)
                      VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                      ON CONFLICT(word, target) DO UPDATE SET meaning = excluded.meaning, sentence = excluded.sentence,
                        source = excluded.source, article_key = excluded.article_key, title = excluded.title,
                        added = excluded.added""",
                   (word, target, meaning, sentence, record.get("source") or "", key_of(record) if record else "",
                    record.get("title") or "", _now()))
        extra = db.execute("SELECT COUNT(*) FROM words").fetchone()[0] - MAX_WORDS
        if extra > 0:
            db.execute("DELETE FROM words WHERE id IN (SELECT id FROM words ORDER BY added LIMIT ?)", (extra,))


def remove_word(path: Path, word_id: int) -> None:
    with _lock, closing(_connect(path)) as db, db:
        db.execute("DELETE FROM words WHERE id = ?", (word_id,))


def words(path: Path) -> list[dict]:
    if not Path(path).exists():
        return []
    with closing(_connect(path)) as db:
        return [dict(row) for row in db.execute("SELECT * FROM words ORDER BY added DESC, id DESC")]


def has_word(path: Path, word: str, target: str) -> bool:
    if not Path(path).exists():
        return False
    with closing(_connect(path)) as db:
        return db.execute("SELECT 1 FROM words WHERE word = ? AND target = ?", (word.strip(), target)).fetchone() is not None
