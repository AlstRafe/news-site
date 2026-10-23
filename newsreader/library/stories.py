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
import math
import threading
import time
from collections import Counter
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
        # Two articles in one language whose texts share almost nothing are different news, however
        # alike their headlines (D-75: 「日相高市」 tied a spy arrest, an Okinawa case and more)
        bodies = _body_vectors(db_path, [dict(row) for row in db.execute(
            f"SELECT id, title, published FROM articles WHERE id IN ({','.join('?' * len(items))})", tuple(items))]) \
            if items else {}

        def apart(a: int, b: int) -> bool:
            body = _body_cosine(bodies, a, b) if a in bodies and b in bodies else None
            return body is not None and body < BODY_APART and min(len(bodies[a][1]), len(bodies[b][1])) >= BODY_APART_TERMS

        clusters = core.cluster(items, shapes, forbidden, apart) if len(items) > 1 else []
        joins = [(row[0], row[1]) for row in db.execute("SELECT a, b FROM story_joins") if row[0] in items and row[1] in items]
        clusters = _apply_joins(clusters, joins, forbidden)

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


#: The similarity recorded for an article the reader put into a group (D-72): above the
#: "closely matched" lines, since the reader confirmed it.
JOINED_SCORE = 0.3
#: A reader's joins may make a group somewhat larger than automatic grouping does (core.MAX_CLUSTER).
JOINED_MAX = 16


def _apply_joins(clusters: list[dict[int, float]], joins: list[tuple[int, int]],
                 forbidden: set[tuple[int, int]]) -> list[dict[int, float]]:
    """Merge the groups (or lone articles) of every pair the reader said is the same news,
    unless that would put a pair the reader separated into one group."""
    owner = {article: index for index, members in enumerate(clusters) for article in members}
    groups = [dict(members) for members in clusters]
    for a, b in joins:
        for article in (a, b):
            if article not in owner:
                owner[article] = len(groups)
                groups.append({article: JOINED_SCORE})
        ga, gb = owner[a], owner[b]
        if ga == gb or len(groups[ga]) + len(groups[gb]) > JOINED_MAX:
            continue
        if any((min(x, y), max(x, y)) in forbidden for x in groups[ga] for y in groups[gb]):
            continue
        for article, score in groups[gb].items():
            groups[ga][article] = max(score, JOINED_SCORE) if article in (a, b) else score
            owner[article] = ga
        groups[ga][a] = max(groups[ga][a], JOINED_SCORE)
        groups[gb] = {}
    return [members for members in groups if len(members) > 1]


def join(db_path: Path, article_id: int, other_id: int) -> bool:
    """The reader says two articles are the same news: remember the pair (and forget an earlier
    "別のニュース" between exactly these two). Returns False when either article is unknown."""
    if article_id == other_id:
        return False
    a, b = min(article_id, other_id), max(article_id, other_id)
    with closing(index.connect(db_path)) as db:
        if db.execute("SELECT COUNT(*) FROM articles WHERE id IN (?, ?)", (a, b)).fetchone()[0] != 2:
            return False
        _write(db, [("INSERT OR IGNORE INTO story_joins(a, b, created) VALUES(?,?,?)", (a, b, status.now_iso())),
                    ("DELETE FROM story_exclusions WHERE a = ? AND b = ?", (a, b))])
    return True


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


def list_stories(db_path: Path, scope: str = "multi", page: int = 1, per_page: int = 20,
                 query: str = "", pivot_words: list[str] | None = None, keys: list[str] | None = None) -> dict:
    """Groups, newest first. scope "multi" = reported by more than one outlet; "all" = any group.
    ``query`` keeps the groups with an article that matches it (as in the article search);
    ``keys`` only those groups (数字が違う, D-67)."""
    having = "HAVING COUNT(DISTINCT a.source) > 1" if scope != "all" else ""
    page = max(1, page)
    query = query.strip()[:100]
    with closing(index.connect(db_path)) as db:
        restrict, params = "", []
        if query:
            ids = index.search_ids(db, query, pivot_words)[:900]
            restrict = (f" AND m.story_key IN (SELECT story_key FROM story_members WHERE article_id IN"
                        f" ({','.join('?' * len(ids)) or 'NULL'}))")
            params = ids
        if keys is not None:
            restrict += f" AND m.story_key IN ({','.join('?' * len(keys)) or 'NULL'})"
            params = params + list(keys)
        base = f"""SELECT m.story_key, MAX(a.published) AS newest, COUNT(*) AS n, COUNT(DISTINCT a.source) AS outlets
                   FROM story_members m JOIN articles a ON a.id = m.article_id WHERE a.error = ''{restrict}
                   GROUP BY m.story_key {having}"""
        total = db.execute(f"SELECT COUNT(*) FROM ({base})", params).fetchone()[0]
        heads = db.execute(f"{base} ORDER BY newest DESC LIMIT ? OFFSET ?",
                           params + [per_page, (page - 1) * per_page]).fetchall()
        members = _members(db, [h["story_key"] for h in heads])
    stories = []
    for h in heads:
        group = members.get(h["story_key"], [])
        central = max(group, key=lambda m: (m["score"], m["published"])) if group else None
        lead_id = central["id"] if central else 0
        stories.append({"key": h["story_key"], "newest": h["newest"], "count": h["n"], "outlets": h["outlets"],
                        "members": group, "lead_title": central["title"] if central else "",
                        "lead_source": central["source"] if central else "",
                        "lead_id": lead_id, "preview": preview(group, lead_id), **_summary(group)})
    return {"stories": stories, "total": total, "page": page, "pages": max(1, -(-total // per_page)), "scope": scope,
            "query": query}


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
               + [("DELETE FROM story_joins WHERE a = ? AND b = ?", (min(article_id, other), max(article_id, other)))
                  for other in others]
               + [("DELETE FROM story_members WHERE article_id = ?", (article_id,))])
        return db.execute("SELECT COUNT(*) FROM story_exclusions").fetchone()[0] - before


def exclusion_count(db_path: Path) -> int:
    with closing(index.connect(db_path)) as db:
        return db.execute("SELECT COUNT(*) FROM story_exclusions").fetchone()[0]


def join_count(db_path: Path) -> int:
    with closing(index.connect(db_path)) as db:
        return db.execute("SELECT COUNT(*) FROM story_joins").fetchone()[0]


def clear_joins(db_path: Path) -> int:
    with closing(index.connect(db_path)) as db:
        count = db.execute("SELECT COUNT(*) FROM story_joins").fetchone()[0]
        _write(db, [("DELETE FROM story_joins", ())])
        return count


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
            "lead_source": central["source"],
            "timeline": timeline, "languages": languages(members), **_summary(members)}


#: Headline language (story_items.lang) -> translation target code.
LANG_TARGETS = {"en": "en", "zh": "zh-hant", "ko": "ko", "ja": "ja"}


def languages(members: list[dict]) -> list[dict]:
    """The languages of a group, for "read them all in one outlet's language": each with its
    target code and outlets, most articles first. Sets ``lang_target`` on every member."""
    found: dict[str, dict] = {}
    for member in members:
        target = LANG_TARGETS.get(member.get("lang") or "", "")
        member["lang_target"] = target
        if target:
            entry = found.setdefault(target, {"target": target, "outlets": [], "count": 0})
            entry["count"] += 1
            source = member["source"] or "未分類"
            if source not in entry["outlets"]:
                entry["outlets"].append(source)
    return sorted(found.values(), key=lambda e: (-e["count"], e["target"]))


def headline_language(db_path: Path, article_id: int) -> str:
    """"en" / "zh" / "ko" / "ja" / "other" as detected for grouping ("" when not known yet)."""
    with closing(index.connect(db_path)) as db:
        row = db.execute("SELECT lang FROM story_items WHERE article_id = ?", (article_id,)).fetchone()
    return row[0] if row else ""


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


#: 似た記事 (D-66): looser than grouping (PAIR_THRESHOLD), for the reader to judge.
SIMILAR_MIN = 0.2
SIMILAR_HOURS = 72


def similar(db_path: Path, article_id: int, limit: int = 5) -> list[dict]:
    """Articles of other outlets whose English headlines resemble this one's, within three days,
    that are not already in its group: [{"id", "source", "title", "published", "score"}]."""
    from datetime import timedelta
    with closing(index.connect(db_path)) as db:
        me = db.execute("""SELECT a.id, a.source, a.published, s.pivot, m.story_key FROM articles a
                           JOIN story_items s ON s.article_id = a.id LEFT JOIN story_members m ON m.article_id = a.id
                           WHERE a.id = ? AND s.pivot IS NOT NULL""", (article_id,)).fetchone()
        moment = core.parse_utc(me["published"]) if me else None
        if me is None or moment is None:
            return []
        low = (moment - timedelta(hours=SIMILAR_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        high = (moment + timedelta(hours=SIMILAR_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        rows = db.execute("""SELECT a.id, a.source, a.title, a.published, s.pivot, m.story_key FROM articles a
                             JOIN story_items s ON s.article_id = a.id LEFT JOIN story_members m ON m.article_id = a.id
                             WHERE a.error = '' AND s.pivot IS NOT NULL AND a.published BETWEEN ? AND ?""",
                          (low, high)).fetchall()
    items = {row["id"]: (core.features(row["pivot"]), core.parse_utc(row["published"]) or moment) for row in rows}
    if article_id not in items:
        return []
    corpus = core.Corpus(items)
    pivots = {row["id"]: row["pivot"] for row in rows}

    def names(pivot: str) -> set[str]:
        return {word for word, shape in core.shapes(pivot).items() if shape & {"name", "quoted"}}

    mine = names(pivots[article_id])
    with closing(index.connect(db_path)) as db:  # pairs the reader already said are different news
        apart = {row[0] if row[1] == article_id else row[1]
                 for row in db.execute("SELECT a, b FROM story_exclusions WHERE a = ? OR b = ?", (article_id, article_id))}
    found = []
    for row in rows:
        if row["source"] == me["source"] or row["id"] == article_id or row["id"] in apart \
                or (me["story_key"] and row["story_key"] == me["story_key"]):
            continue
        score = corpus.raw(article_id, row["id"])
        # a shared name (a person, place, organisation) as well: shared common words are not enough
        if score >= SIMILAR_MIN and mine & names(row["pivot"]):
            found.append({"id": row["id"], "source": row["source"], "title": row["title"], "published": row["published"],
                          "score": round(score, 2)})
    found.sort(key=lambda item: -item["score"])
    return found[:limit]


#: このニュースの流れ (D-74): groups before and after this one about the same matter (a developing
#: story is several groups: the attack, the second attack, the response), within ARC_DAYS of it.
ARC_DAYS = 10
#: Two articles in one language whose opening paragraphs share this much (tf-idf cosine) are about
#: the same matter (measured: follow-ups 0.17-0.25, unrelated news at most 0.13).
ARC_BODY_MIN = 0.16
#: Across languages only headlines compare: this similar (core.Corpus.raw) with two shared names,
#: or ARC_ONE_NAME with one.
ARC_HEADLINE_MIN = 0.22
ARC_ONE_NAME = 0.4
ARC_LIMIT = 8
ARC_PARAGRAPHS = 8
#: D-75: same-language articles below this text similarity are not grouped (measured on the live
#: library: different news 0.014-0.045, the same news from 0.07; on the labelled set from 0.10)...
BODY_APART = 0.045
#: ...when both have enough text to judge (distinct terms in the opening paragraphs).
BODY_APART_TERMS = 40
_body_cache: dict[tuple[int, str], tuple[str, dict[str, float]]] = {}
#: (database, group) -> (state of all groups, result): a refresh, correction or join changes the state.
_arc_cache: dict[tuple[str, str], tuple[tuple, list[dict]]] = {}


def _body_terms(db_path: Path, article: dict) -> tuple[str, dict[str, float]]:
    """(language, term -> 1 + log count) of the opening paragraphs."""
    key = (article["id"], article["published"] or "")
    if key not in _body_cache:
        if len(_body_cache) > 4000:
            _body_cache.clear()
        content = index.get_content(db_path, article["id"]) or {"paragraphs": []}
        lang = core.detect_lang(article["title"])
        counts = core.body_terms("\n".join(content["paragraphs"][:ARC_PARAGRAPHS]), lang)
        _body_cache[key] = (lang, {t: 1 + math.log(c) for t, c in counts.items()})
    return _body_cache[key]


def _body_vectors(db_path: Path, articles: list[dict]) -> dict[int, tuple[str, dict[str, float]]]:
    """Opening paragraphs as unit tf-idf vectors, idf over the given articles in the same language
    ("했다", "表示" are in every Korean / Chinese article, rare among all)."""
    terms = {a["id"]: _body_terms(db_path, a) for a in articles}
    df: dict[str, Counter] = {}
    docs: Counter = Counter()
    for lang, tf in terms.values():
        docs[lang] += 1
        df.setdefault(lang, Counter()).update(tf.keys())
    idf = {lang: {t: math.log((docs[lang] + 1) / (count + 0.5)) for t, count in counts.items()}
           for lang, counts in df.items()}
    bodies = {}
    for article_id, (lang, tf) in terms.items():
        weights = {t: w * idf[lang][t] for t, w in tf.items()}
        norm = math.sqrt(sum(w * w for w in weights.values())) or 1.0
        bodies[article_id] = (lang, {t: w / norm for t, w in weights.items()})
    return bodies


def _body_cosine(bodies: dict, a: int, b: int) -> float | None:
    """None when the two are in different languages (or one has no text)."""
    (lang_a, va), (lang_b, vb) = bodies[a], bodies[b]
    if lang_a != lang_b or not va or not vb:
        return None
    small, big = (va, vb) if len(va) <= len(vb) else (vb, va)
    return sum(w * big.get(t, 0.0) for t, w in small.items())


def warm_arcs(db_path: Path, hours: int = 24) -> int:
    """Work out the 続報 of the last ``hours``' multi-outlet groups ahead of time (今日のまとめ asks
    for all of them at once; after a collection the first visitor would otherwise wait)."""
    from datetime import timedelta, timezone
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    keys = [g["key"] for g in list_stories(db_path, scope="multi", per_page=60)["stories"] if (g["newest"] or "") >= since]
    for key in keys:
        arc(db_path, key)
    return len(keys)


def arc(db_path: Path, key: str, limit: int = ARC_LIMIT) -> list[dict]:
    """Groups of the same developing matter as group ``key``, oldest first, this group included
    (``"current": True``); [] when there is none. Each: {"key", "title", "lead_id", "lead_source",
    "count", "current", "score", "outlet_counts", "first", "oldest", "newest_time"}."""
    with closing(index.connect(db_path)) as db:
        state = tuple(db.execute("SELECT COUNT(*), COALESCE(SUM(rowid), 0), COALESCE(SUM(LENGTH(story_key)), 0) "
                                 "FROM story_members").fetchone())
    cached = _arc_cache.get((str(db_path), key))
    if cached and cached[0] == state:
        return cached[1]
    if len(_arc_cache) > 2000:
        _arc_cache.clear()
    result = _arc(db_path, key, limit)
    _arc_cache[(str(db_path), key)] = (state, result)
    return result


def _arc(db_path: Path, key: str, limit: int) -> list[dict]:
    from datetime import timedelta
    with closing(index.connect(db_path)) as db:
        mine = _members(db, [key]).get(key, [])
        dated = [d for d in (core.parse_utc(m["published"]) for m in mine if m.get("published")) if d]
        if not dated:
            return []
        low = (min(dated) - timedelta(days=ARC_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        high = (max(dated) + timedelta(days=ARC_DAYS)).strftime("%Y-%m-%dT%H:%M:%SZ")
        keys = [row[0] for row in db.execute(
            """SELECT DISTINCT m.story_key FROM story_members m JOIN articles a ON a.id = m.article_id
               WHERE a.error = '' AND a.published BETWEEN ? AND ?""", (low, high))]
        groups = _members(db, keys)
    groups[key] = mine
    everyone = [m for g in groups.values() for m in g]
    bodies = _body_vectors(db_path, everyone)
    # headlines (English pivots) for pairs in different languages
    pivoted = {m["id"]: m["pivot"] for m in everyone if m.get("pivot")}
    items = {i: (f, dated[0]) for i, f in ((i, core.features(p)) for i, p in pivoted.items()) if f}
    corpus = core.Corpus(items) if items else None
    names = core.name_tokens({i: core.shapes(p) for i, p in pivoted.items()})

    def pair(a: int, b: int) -> float:
        """How strongly two articles tie the groups: 1.0 = just enough."""
        body = _body_cosine(bodies, a, b)
        if body is not None:
            return body / ARC_BODY_MIN
        if corpus is None or a not in items or b not in items:
            return 0.0
        common = sum(1 for t in corpus.vectors[a] if t in corpus.vectors[b] and t in names)
        score = corpus.raw(a, b)
        return score / ARC_HEADLINE_MIN if common >= 2 or (common and score >= ARC_ONE_NAME) else 0.0

    # whatever ties two groups, their headlines must share a name (a place, person, organisation):
    # "military base", "killed" alone tie a fire in Canada to a missile strike in Yemen
    # (a name here: capitalised in the window's headlines and never written in lower case: not "Military Base")
    seen: dict[str, set[str]] = {}
    for pivot in pivoted.values():
        for word, found_shapes in core.shapes(pivot).items():
            seen.setdefault(word, set()).update(found_shapes)
    proper = {w for w, f in seen.items() if "lower" not in f and f & {"name", "quoted", "initial"} and w not in core._GENERIC}

    def group_names(members: list[dict]) -> set[str] | None:
        if not any(m["id"] in items for m in members):
            return None  # headlines not translated yet: nothing to require
        return {t for m in members if m["id"] in items for t in corpus.vectors[m["id"]] if t in proper}

    my_names = group_names(mine)
    found = []
    for other, members in groups.items():
        if other == key:
            continue
        their_names = group_names(members)
        if my_names is not None and their_names is not None and not my_names & their_names:
            continue
        best = max((pair(a["id"], b["id"]) for a in mine for b in members), default=0.0)
        if best >= 1.0:
            found.append((best, other))
    found.sort(reverse=True)
    if not found:
        return []
    scores = {other: score for score, other in found}
    result = []
    for other in [o for _, o in found[: max(1, limit - 1)]] + [key]:
        members = groups[other]
        central = max(members, key=lambda m: (m["score"], m["published"]))
        result.append({"key": other, "title": central["title"], "lead_id": central["id"],
                       "lead_source": central["source"], "count": len(members), "current": other == key,
                       "score": round(scores.get(other, 0.0), 2), **_summary(members)})
    result.sort(key=lambda g: (g["oldest"], g["key"]))
    return result
