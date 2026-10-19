"""媒体くらべ (D-62): how the outlets compare over the last days — from the library alone, no
translation. Per outlet: articles, how many of them other outlets also reported, scoops (news
no other outlet reported), how often it reported a shared news first and how late otherwise,
and the figure disagreements it was part of. Plus which pairs of outlets cover the same news.
"""
from __future__ import annotations

import json
import statistics
from contextlib import closing
from datetime import datetime, timedelta, timezone
from itertools import combinations
from pathlib import Path

from newsreader.library import index

_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _parse(stamp: str) -> datetime | None:
    try:
        return datetime.strptime(stamp, _FORMAT)
    except (TypeError, ValueError):
        return None


def stats(db_path: Path, *, days: int = 7, now: datetime | None = None) -> dict:
    now = (now or datetime.now(timezone.utc)).replace(tzinfo=None)
    since = (now - timedelta(days=days)).strftime(_FORMAT)
    with closing(index.connect(db_path)) as db:
        articles = db.execute("SELECT a.id, a.source, a.published, m.story_key FROM articles a "
                              "LEFT JOIN story_members m ON m.article_id = a.id "
                              "WHERE a.error = '' AND a.published >= ? AND a.source != ''", (since,)).fetchall()
        facts = db.execute("SELECT summary FROM story_facts WHERE differs = 1 AND cohesive = 1 AND newest >= ?",
                           (since,)).fetchall()
    outlets: dict[str, dict] = {}
    groups: dict[str, list[tuple[str, datetime | None]]] = {}
    for article_id, source, published, key in articles:
        entry = outlets.setdefault(source, {"source": source, "articles": 0, "shared": 0, "scoops": 0, "first": 0,
                                            "later": [], "disputes": 0})
        entry["articles"] += 1
        if key:
            groups.setdefault(key, []).append((source, _parse(published)))
    shared_groups = {key: members for key, members in groups.items() if len({s for s, _ in members}) > 1}
    for members in shared_groups.values():
        for source, _ in members:
            outlets[source]["shared"] += 1
    for entry in outlets.values():
        entry["scoops"] = entry["articles"] - entry["shared"]  # no other outlet reported that news
    pairs: dict[tuple[str, str], int] = {}
    for members in shared_groups.values():
        firsts: dict[str, datetime] = {}
        for source, moment in members:
            if moment and (source not in firsts or moment < firsts[source]):
                firsts[source] = moment
        if len(firsts) > 1:
            earliest = min(firsts.values())
            for source, moment in firsts.items():
                if moment == earliest:
                    outlets[source]["first"] += 1
                else:
                    outlets[source]["later"].append((moment - earliest).total_seconds() / 3600)
        for a, b in combinations(sorted({s for s, _ in members}), 2):
            pairs[(a, b)] = pairs.get((a, b), 0) + 1
    for (raw,) in facts:
        try:
            kinds = json.loads(raw)
        except ValueError:
            continue
        for source in {step["source"] for kind in kinds if kind.get("differs") for step in kind.get("trail", [])}:
            if source in outlets:
                outlets[source]["disputes"] += 1
    rows = []
    for entry in sorted(outlets.values(), key=lambda e: (-e["articles"], e["source"].casefold())):
        later = entry.pop("later")
        entry["median_lag_hours"] = round(statistics.median(later), 1) if later else None
        entry["shared_ratio"] = round(100 * entry["shared"] / entry["articles"]) if entry["articles"] else 0
        entry["races"] = entry["first"] + len(later)
        rows.append(entry)
    return {"days": days, "since": since, "outlets": rows, "shared_news": len(shared_groups),
            "pairs": [{"a": a, "b": b, "news": n} for (a, b), n in sorted(pairs.items(), key=lambda kv: -kv[1])]}
