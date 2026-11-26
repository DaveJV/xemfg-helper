"""Resolve a fuzzy game name, Steam appid or path to Game objects."""
from __future__ import annotations

import re
from pathlib import Path

from rapidfuzz import fuzz, process, utils

from ..models import Game

ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10,
         "xi": 11, "xii": 12, "xiii": 13, "xiv": 14, "xv": 15, "xvi": 16, "xvii": 17, "xviii": 18,
         "xix": 19, "xx": 20}
STOP = {"the", "of", "a", "an", "and"}


def norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _words(title: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", title.lower()) if w]


def aliases(title: str) -> set[str]:
    """'FINAL FANTASY XVI' -> {'finalfantasyxvi', 'finalfantasy16', 'ffxvi', 'ff16', ...}."""
    words = _words(title)
    out = {norm(title)}
    arabic = [str(ROMAN[w]) if w in ROMAN else w for w in words]
    out.add("".join(arabic))
    for ws in (words, arabic):
        body = [w for w in ws if w not in STOP] or ws
        # initials, keeping trailing numerals whole: ff16 / ffxvi
        ini = "".join(w if (w.isdigit() or w in ROMAN) else w[0] for w in body)
        out.add(ini)
        ini_all = "".join(w[0] for w in body)
        if len(ini_all) >= 2:
            out.add(ini_all)
    return {a for a in out if a}


def resolve(query: str, games: list[Game]) -> list[Game]:
    """Return matching games, best first. One result = unambiguous."""
    q = query.strip()
    if not q:
        return []
    if q.isdigit():
        hit = [g for g in games if g.appid == q]
        if hit:
            return hit
    hit = [g for g in games if g.key == q or (g.gameid and g.gameid == q and g.key.startswith("faugus:"))]
    if hit:
        return hit
    nq = norm(q)
    exact = [g for g in games if norm(g.title) == nq]
    if exact:
        return exact
    alias_hits = [g for g in games if nq in aliases(g.title)]
    if alias_hits:
        return alias_hits
    sub = [g for g in games if nq and nq in norm(g.title)]
    if len(sub) == 1:
        return sub
    choices = {i: g.title for i, g in enumerate(games)}
    scored = process.extract(q, choices, scorer=fuzz.WRatio, processor=utils.default_process, limit=8, score_cutoff=60)
    if not scored:
        return sub
    best = scored[0][1]
    if len(scored) == 1 or best - scored[1][1] >= 12:
        return [games[scored[0][2]]]
    ranked = [games[k] for _, s, k in scored if s >= best - 12]
    # keep substring hits on top when present
    return sorted(ranked, key=lambda g: (g not in sub,))


def looks_like_path(q: str) -> bool:
    return "/" in q or q.startswith("~") or (Path(q).exists() and Path(q).is_absolute())
