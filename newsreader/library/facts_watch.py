"""読み比べの数字を見張る (D-59): for recent news groups reported by several outlets, compare the
figures once in the background and keep a summary, so the home page can point at the news whose
outlets disagree on a count ("聯合 20人 → 53人 · BBC 50人超").

The same summary is written when someone opens 「数字を比べる」 on a group page. Translation is
injected (``translate_en``: texts -> English texts, cached by the caller) and capped per day.
"""
from __future__ import annotations

import json
import time
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from newsreader.library import compare, index, stories

TranslateEn = Callable[[list[str]], list[str]]

#: An article's figures come from its headline and first paragraphs (later ones often report
#: side events: another attack, last year's toll).
FACT_PARAGRAPHS = 3
FACT_CHARS = 1_000
MAX_MEMBERS = 12
#: Background budget for translating openings to English, per local day (the group page's own
#: button is not counted: the reader asked for it).
DAILY_CHARS = 15_000
RECENT_HOURS = 48
#: Only groups whose every member is this close to the rest (stories_core mean similarity) are
#: put on the home page: a loosely chained group can mix two events and show a false disagreement.
COHESIVE_SCORE = 0.25
MAX_GROUPS_PER_RUN = 30


def opening(content: dict | None) -> str:
    text = ""
    for paragraph in [p.strip() for p in (content or {}).get("paragraphs", []) if p.strip()][:FACT_PARAGRAPHS]:
        if text and len(text) + len(paragraph) > FACT_CHARS:
            break
        text = f"{text}\n{paragraph}" if text else paragraph
    return text[:FACT_CHARS]


def newest_members(story: dict) -> list[dict]:
    return sorted(story["members"], key=lambda m: m["published"] or "", reverse=True)[:MAX_MEMBERS]


def english_of(db_path: Path, members: list[dict], translate_en: TranslateEn) -> tuple[dict[int, str], dict[int, tuple[str, str]]]:
    """({id: English headline + opening}, {id: (own opening, its English)} for non-English ones).
    The body comes first so that a figure in both keeps the fuller sentence."""
    english: dict[int, str] = {}
    foreign: list[tuple[dict, str]] = []
    for member in members:
        text = opening(index.get_content(db_path, member["id"]))
        if member.get("lang") == "en":
            english[member["id"]] = f"{text}\n{member['title']}"
        elif member.get("pivot"):
            foreign.append((member, text))
    translated = translate_en([text for _, text in foreign]) if foreign else []
    pairs = {}
    for (member, text), out in zip(foreign, translated):
        english[member["id"]] = f"{out}\n{member['pivot']}"
        pairs[member["id"]] = (text, out)
    return english, pairs


def summary(categories: list[dict]) -> list[dict]:
    """The compared counts only: [{"kind", "label", "differs", "trail": [{"id", "source", "published", "text"}]}]."""
    out = []
    for category in categories:
        if not category["compared"]:
            continue
        trail = []
        for row in category["rows"]:
            top = max(row["facts"], key=lambda f: -1 if f["value"] is None else f["value"])
            trail.append({"id": row["id"], "source": row["source"], "published": row["published"], "text": top["text"]})
        out.append({"kind": category["kind"], "label": category["label"], "differs": category["differs"], "trail": trail})
    return out


def _signature(members: list[dict]) -> str:
    return ",".join(str(i) for i in sorted(m["id"] for m in members))


def store(db_path: Path, key: str, members: list[dict], categories: list[dict]) -> None:
    found = summary(categories)
    newest = max((m["published"] or "" for m in members), default="")
    cohesive = all(float(m.get("score") or 0) >= COHESIVE_SCORE for m in members)
    with closing(index.connect(db_path)) as db:
        index._write(db, [("INSERT OR REPLACE INTO story_facts(story_key, members, newest, differs, cohesive, summary, "
                           "computed) VALUES(?,?,?,?,?,?,?)",
                           (key, _signature(members), newest, int(any(c["differs"] for c in found)), int(cohesive),
                            json.dumps(found, ensure_ascii=False), datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")))])


def _today() -> str:
    return time.strftime("%Y-%m-%d")


def refresh(db_path: Path, translate_en: TranslateEn | None, *, daily_chars: int = DAILY_CHARS,
            now: datetime | None = None) -> dict:
    """Compare the figures of recent multi-outlet groups not compared yet (or whose members
    changed). Without ``translate_en`` only groups whose articles are all English are done."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(hours=RECENT_HOURS)).strftime("%Y-%m-%dT%H:%M:%SZ")
    groups = stories.list_stories(db_path, scope="multi", per_page=MAX_GROUPS_PER_RUN)["stories"]
    with closing(index.connect(db_path)) as db:
        done = {row[0]: row[1] for row in db.execute("SELECT story_key, members FROM story_facts")}
        used = (db.execute("SELECT chars FROM facts_usage WHERE day = ?", (_today(),)).fetchone() or (0,))[0]
    result = {"compared": 0, "differs": 0, "skipped_budget": 0, "chars": 0}
    for group in groups:
        if (group["newest"] or "") < since:
            continue
        members = newest_members(group)
        if done.get(group["key"]) == _signature(members):
            continue
        foreign_chars = sum(len(opening(index.get_content(db_path, m["id"])))
                            for m in members if m.get("lang") != "en" and m.get("pivot"))
        if foreign_chars and (translate_en is None or used + foreign_chars > daily_chars):
            result["skipped_budget"] += 1
            continue
        english, _ = english_of(db_path, members, translate_en or (lambda texts: []))
        categories = compare.compare([dict(m, english=english[m["id"]]) for m in members if m["id"] in english])
        store(db_path, group["key"], members, categories)
        if foreign_chars:
            used += foreign_chars
            result["chars"] += foreign_chars
            with closing(index.connect(db_path)) as db:
                index._write(db, [("INSERT INTO facts_usage(day, chars) VALUES(?, ?) "
                                   "ON CONFLICT(day) DO UPDATE SET chars = excluded.chars", (_today(), used))])
        result["compared"] += 1
        result["differs"] += int(any(c["differs"] for c in categories if c["compared"]))
    return result


def differing(db_path: Path, *, days: int = 3, limit: int = 4, now: datetime | None = None) -> list[dict]:
    """Recent groups whose outlets disagree on a count, newest first, for the home page:
    [{"key", "title", "lead_source", "kinds": [{"label", "trail": [...]}]}]."""
    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with closing(index.connect(db_path)) as db:
        rows = db.execute("SELECT story_key, members, summary FROM story_facts WHERE differs = 1 AND cohesive = 1 AND newest >= ? "
                          "ORDER BY newest DESC LIMIT ?", (since, limit * 3)).fetchall()
    out = []
    for key, members, raw in rows:
        story = stories.get_story(db_path, key)
        if story is None or _signature(newest_members(story)) != members:
            continue  # regrouped since: the summary is stale
        try:
            kinds = [k for k in json.loads(raw) if k.get("differs")]
        except ValueError:
            continue
        out.append({"key": key, "title": story["lead_title"], "lead_id": story["lead_id"],
                    "lead_source": story["lead_source"], "kinds": kinds})
        if len(out) >= limit:
            break
    return out
