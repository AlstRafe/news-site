"""Same news across outlets (§10): pivot headlines to English, cluster, store, query.

``refresh`` is the only writer. It
1. gives every article an English *pivot* headline: English titles as they are; other titles
   translated through the configured provider, **headlines only**, within a daily character
   budget (D-08). Pivots are stored and never translated again. With no key, over budget or
   on a provider error the title simply waits (shown on /status) — English titles still group;
2. clusters all articles that have a pivot and a publication time with ``stories_core``;
3. replaces ``story_members``. A story's key is ``s<smallest member id>``, so its URL survives
   later articles joining it.

Readers (``list_stories``, ``get_story``, ``related``) only query.
"""
from __future__ import annotations

import logging
import threading
import time
from contextlib import closing
from datetime import datetime
from pathlib import Path
from typing import Callable

from newsreader.library import index, status
from newsreader.library import stories_core as core

log = logging.getLogger("newsreader.stories")

#: Clustering looks at the newest N articles; older groups stay as they were last computed.
MAX_CLUSTER_ARTICLES = 3_000
_refresh_lock = threading.Lock()

Translator = Callable[[list[str]], list[str]]


def _today() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d")


def _used_today(db) -> int:
    row = db.execute("SELECT chars FROM story_usage WHERE day = ?", (_today(),)).fetchone()
    return int(row[0]) if row else 0


def _write(db, statements) -> None:
    index._write(db, statements)


def refresh(db_path: Path, *, translate: Translator | None, enabled: bool = True,
            daily_chars: int = 8_000, status_path: Path | None = None) -> dict:
    """Bring pivots and groups up to date. ``translate`` None = no provider key (English only)."""
    with _refresh_lock:
        started = time.perf_counter()
        result = _refresh_locked(Path(db_path), translate, enabled, daily_chars)
        result["seconds"] = round(time.perf_counter() - started, 2)
        status.update("stories", {**result, "refreshed": status.now_iso(), "enabled": enabled,
                                  "daily_limit": daily_chars, "key_configured": translate is not None},
                      path=status_path or Path(db_path).parent / "status.json")
        return result


def _refresh_locked(db_path: Path, translate: Translator | None, enabled: bool, daily_chars: int) -> dict:
    error = ""
    with closing(index.connect(db_path)) as db:
        # Orphans can appear if an older version (without foreign keys) deleted articles.
        _write(db, [("DELETE FROM story_items WHERE article_id NOT IN (SELECT id FROM articles)", ()),
                    ("DELETE FROM story_members WHERE article_id NOT IN (SELECT id FROM articles)", ())])
        rows = db.execute(
            """SELECT a.id, a.title, a.published, s.title AS pivot_title, s.pivot
               FROM articles a LEFT JOIN story_items s ON s.article_id = a.id
               WHERE a.error = '' ORDER BY a.published DESC, a.id DESC""").fetchall()

        # 1. pivots for new or re-titled articles: English at once, others queued for translation.
        statements, waiting = [], []
        for row in rows:
            if row["pivot_title"] == row["title"] and (row["pivot"] is not None or not enabled):
                continue
            if row["pivot_title"] == row["title"] and row["pivot"] is None:
                waiting.append(row)
                continue
            lang = core.detect_lang(row["title"])
            if lang == "en":
                statements.append(("INSERT OR REPLACE INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) "
                                   "VALUES(?,?,?,?,0,?)", (row["id"], row["title"], lang, row["title"], status.now_iso())))
            else:
                statements.append(("INSERT OR REPLACE INTO story_items(article_id, title, lang, pivot, pivot_chars, pivot_at) "
                                   "VALUES(?,?,?,NULL,0,'')", (row["id"], row["title"], lang)))
                waiting.append(row)
        _write(db, statements)

        translated = 0
        used = _used_today(db)
        if enabled and translate is not None and waiting:
            budget = max(0, daily_chars - used)
            batch, size = [], 0
            for row in waiting:  # newest first
                cost = len(row["title"])
                if size + cost > budget:
                    break
                batch.append(row)
                size += cost
            if batch:
                try:
                    pivots = translate([row["title"] for row in batch])
                    now = status.now_iso()
                    _write(db, [("UPDATE story_items SET pivot=?, pivot_chars=?, pivot_at=? WHERE article_id=?",
                                 (pivot or row["title"], len(row["title"]), now, row["id"]))
                                for row, pivot in zip(batch, pivots)]
                           + [("INSERT INTO story_usage(day, chars) VALUES(?, ?) "
                               "ON CONFLICT(day) DO UPDATE SET chars = chars + excluded.chars", (_today(), size))])
                    translated, used = len(batch), used + size
                except Exception as exc:  # provider down / quota: titles wait for the next refresh
                    error = str(exc)[:200] if exc.__class__.__name__ == "TranslationError" else type(exc).__name__
                    log.warning("Headline translation failed: %s", error)

        # 2. cluster everything that has a pivot and a time.
        items, shapes = {}, {}
        for row in db.execute(
                """SELECT a.id, a.published, s.pivot FROM articles a JOIN story_items s ON s.article_id = a.id
                   WHERE a.error = '' AND s.pivot IS NOT NULL AND a.published != ''
                   ORDER BY a.published DESC LIMIT ?""", (MAX_CLUSTER_ARTICLES,)):
            moment = core.parse_utc(row["published"])
            feats = core.features(row["pivot"])
            if moment and feats:
                items[row["id"]] = (feats, moment)
                shapes[row["id"]] = core.shapes(row["pivot"])
        forbidden = {(row[0], row[1]) for row in db.execute("SELECT a, b FROM story_exclusions")}
        clusters = core.cluster(items, shapes, forbidden) if len(items) > 1 else []

        # 3. replace the groups of the articles that took part in this run.
        statements = [("DELETE FROM story_members WHERE article_id IN (%s)" % ",".join("?" * len(items)), tuple(items))] if items else []
        for members in clusters:
            key = f"s{min(members)}"
            statements += [("INSERT OR REPLACE INTO story_members(article_id, story_key, score) VALUES(?,?,?)",
                            (article_id, key, round(score, 4))) for article_id, score in members.items()]
        # A group computed before may straddle the clustering window (newest MAX_CLUSTER_ARTICLES):
        # its newer members were just regrouped; never leave the rest as a one-article "group".
        statements.append(("DELETE FROM story_members WHERE story_key IN (SELECT story_key FROM story_members "
                           "GROUP BY story_key HAVING COUNT(*) < 2)", ()))
        _write(db, statements)

        pending = db.execute("SELECT COUNT(*) FROM story_items WHERE pivot IS NULL").fetchone()[0]
        multi = db.execute(
            """SELECT COUNT(*) FROM (SELECT m.story_key FROM story_members m JOIN articles a ON a.id = m.article_id
               GROUP BY m.story_key HAVING COUNT(DISTINCT a.source) > 1)""").fetchone()[0]
    return {"groups": len(clusters), "multi_outlet": multi, "pending": pending, "translated": translated,
            "chars_today": used, "error": error}


# ---- queries -----------------------------------------------------------------

def _members(db, keys: list[str]) -> dict[str, list[dict]]:
    if not keys:
        return {}
    rows = db.execute(
        f"""SELECT m.story_key, m.score, a.id, a.source, a.title, a.published, a.lead, a.thumb_src, s.pivot, s.lang
            FROM story_members m JOIN articles a ON a.id = m.article_id
            LEFT JOIN story_items s ON s.article_id = a.id
            WHERE m.story_key IN ({",".join("?" * len(keys))}) AND a.error = ''
            ORDER BY a.published DESC, a.id DESC""", keys).fetchall()
    grouped: dict[str, list[dict]] = {key: [] for key in keys}
    for row in rows:
        grouped[row["story_key"]].append(dict(row))
    return grouped


def list_stories(db_path: Path, scope: str = "multi", page: int = 1, per_page: int = 20) -> dict:
    """Groups, newest first. scope "multi" = reported by more than one outlet; "all" = any group."""
    having = "HAVING COUNT(DISTINCT a.source) > 1" if scope != "all" else ""
    page = max(1, page)
    with closing(index.connect(db_path)) as db:
        base = f"""SELECT m.story_key, MAX(a.published) AS newest, COUNT(*) AS n, COUNT(DISTINCT a.source) AS outlets
                   FROM story_members m JOIN articles a ON a.id = m.article_id WHERE a.error = ''
                   GROUP BY m.story_key {having}"""
        total = db.execute(f"SELECT COUNT(*) FROM ({base})").fetchone()[0]
        heads = db.execute(f"{base} ORDER BY newest DESC LIMIT ? OFFSET ?", (per_page, (page - 1) * per_page)).fetchall()
        members = _members(db, [h["story_key"] for h in heads])
    stories = []
    for h in heads:
        group = members.get(h["story_key"], [])
        central = max(group, key=lambda m: (m["score"], m["published"])) if group else None
        lead_id = central["id"] if central else 0
        stories.append({"key": h["story_key"], "newest": h["newest"], "count": h["n"], "outlets": h["outlets"],
                        "members": group, "lead_title": central["title"] if central else "",
                        "lead_id": lead_id, "preview": preview(group, lead_id), **_summary(group)})
    return {"stories": stories, "total": total, "page": page, "pages": max(1, -(-total // per_page)), "scope": scope}


#: Headlines shown under the lead on the list page; the rest are on the group page.
PREVIEW_HEADLINES = 3


def preview(members: list[dict], lead_id: int, limit: int = PREVIEW_HEADLINES) -> list[dict]:
    """Headlines to show under the lead: first the newest article of every *other* outlet (how
    the others headlined it), then the newest remaining ones. ``members`` is newest first."""
    lead = next((m for m in members if m["id"] == lead_id), None)
    rest = [m for m in members if m["id"] != lead_id]
    seen = {lead["source"]} if lead else set()
    chosen: list[dict] = []
    for member in rest:
        if member["source"] not in seen:
            seen.add(member["source"])
            chosen.append(member)
    chosen += [m for m in rest if m not in chosen]
    return chosen[:limit]


def _summary(members: list[dict]) -> dict:
    """Outlets (most articles first), first report and the time span of a group."""
    counts: dict[str, int] = {}
    for member in members:
        counts[member["source"] or "未分類"] = counts.get(member["source"] or "未分類", 0) + 1
    dated = sorted((m for m in members if m.get("published")), key=lambda m: (m["published"], m["id"]))
    return {"outlet_counts": sorted(counts.items(), key=lambda kv: (-kv[1], kv[0].casefold())),
            "first": dated[0] if dated else None, "oldest": dated[0]["published"] if dated else "",
            "newest_time": dated[-1]["published"] if dated else ""}


def exclude(db_path: Path, article_id: int, key: str) -> int:
    """The reader says ``article_id`` is not the same news as the rest of group ``key``: remember
    every such pair (never grouped again). Returns the number of new pairs; the caller re-groups."""
    with closing(index.connect(db_path)) as db:
        others = [row[0] for row in db.execute(
            "SELECT article_id FROM story_members WHERE story_key = ? AND article_id != ?", (key, article_id))]
        if not others or db.execute("SELECT 1 FROM story_members WHERE story_key = ? AND article_id = ?",
                                    (key, article_id)).fetchone() is None:
            return 0
        before = db.execute("SELECT COUNT(*) FROM story_exclusions").fetchone()[0]
        _write(db, [("INSERT OR IGNORE INTO story_exclusions(a, b, created) VALUES(?,?,?)",
                     (min(article_id, other), max(article_id, other), status.now_iso())) for other in others]
               + [("DELETE FROM story_members WHERE article_id = ?", (article_id,))])
        return db.execute("SELECT COUNT(*) FROM story_exclusions").fetchone()[0] - before


def exclusion_count(db_path: Path) -> int:
    with closing(index.connect(db_path)) as db:
        return db.execute("SELECT COUNT(*) FROM story_exclusions").fetchone()[0]


def clear_exclusions(db_path: Path) -> int:
    with closing(index.connect(db_path)) as db:
        count = db.execute("SELECT COUNT(*) FROM story_exclusions").fetchone()[0]
        _write(db, [("DELETE FROM story_exclusions", ())])
        return count


def get_story(db_path: Path, key: str) -> dict | None:
    """One group with its members split into columns per outlet."""
    with closing(index.connect(db_path)) as db:
        members = _members(db, [key]).get(key, [])
    if not members:
        return None
    columns: dict[str, list[dict]] = {}
    for member in members:
        columns.setdefault(member["source"] or "未分類", []).append(member)
    # Heading: the most central member (highest mean similarity), ties to the newest.
    central = max(members, key=lambda m: (m["score"], m["published"]))
    timeline = sorted(members, key=lambda m: (m["published"], m["id"]))
    return {"key": key, "members": members, "columns": sorted(columns.items(), key=lambda kv: kv[0].casefold()),
            "newest": members[0]["published"], "lead_title": central["title"], "lead_id": central["id"],
            "timeline": timeline, **_summary(members)}


def badges(db_path: Path, article_ids: list[int]) -> dict[int, dict]:
    """For list cards: {article_id: {"key", "count", "outlets"}} for articles in a multi-outlet group."""
    if not article_ids:
        return {}
    marks = ",".join("?" * len(article_ids))
    with closing(index.connect(db_path)) as db:
        rows = db.execute(
            f"""SELECT m.article_id, g.story_key, g.n, g.outlets FROM story_members m
                JOIN (SELECT m2.story_key, COUNT(*) AS n, COUNT(DISTINCT a.source) AS outlets
                      FROM story_members m2 JOIN articles a ON a.id = m2.article_id WHERE a.error = ''
                      GROUP BY m2.story_key) g ON g.story_key = m.story_key
                WHERE m.article_id IN ({marks}) AND g.outlets > 1""", list(article_ids)).fetchall()
    return {row[0]: {"key": row[1], "count": row[2], "outlets": row[3]} for row in rows}


def related(db_path: Path, article_id: int) -> dict | None:
    """The other articles grouped with ``article_id`` (None when it is in no group)."""
    with closing(index.connect(db_path)) as db:
        row = db.execute("SELECT story_key FROM story_members WHERE article_id = ?", (article_id,)).fetchone()
        if row is None:
            return None
        members = _members(db, [row["story_key"]]).get(row["story_key"], [])
    others = [m for m in members if m["id"] != article_id]
    return {"key": row["story_key"], "members": others} if others else None
