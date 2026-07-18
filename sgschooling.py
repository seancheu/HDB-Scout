"""Multi-year P1 ballot history from sgschooling.com.

MOE only publishes the LATEST year's vacancies/balloting; sgschooling.com
keeps ~17 years of history per school. This module fetches a school's page
(e.g. https://sgschooling.com/school/angsana), parses the Phase 2C column of
its ballot-history table, and caches everything locally so each school is
fetched at most once a month.

Per year we extract:  taken / applied / vacancies, whether it went to ballot,
and the ballot note (e.g. "SC<1 (164/59)" = SC within 1 km balloted, 164
applicants for 59 places).  robots.txt allows this; we fetch one small page
per school, on demand, with a long cache.
"""

import json
import os
import re
import time

import requests

_BASE = "https://sgschooling.com"
_CACHE = os.path.join(os.path.dirname(__file__), "sgschooling_cache.json")
_TTL = 30 * 24 * 3600          # ballot data changes once a year
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126 Safari/537.36")
_mem = None


def _load():
    global _mem
    if _mem is None:
        try:
            with open(_CACHE, encoding="utf-8") as f:
                _mem = json.load(f)
        except Exception:
            _mem = {}
        _mem.setdefault("index", {})
        _mem.setdefault("schools", {})
    return _mem


def _save():
    try:
        with open(_CACHE, "w", encoding="utf-8") as f:
            json.dump(_mem, f, indent=1)
    except OSError:
        pass


def _get(url):
    r = requests.get(url, headers={"User-Agent": _UA}, timeout=20,
                     allow_redirects=True)
    r.raise_for_status()
    return r.text


def _norm(name):
    """Loose key for matching school names across sources."""
    n = (name or "").lower()
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"\b(primary|school|pri)\b", " ", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def _index():
    """name-key -> {slug, name} for all ~186 primary schools (cached)."""
    d = _load()
    idx = d["index"]
    if idx.get("fetched", 0) > time.time() - _TTL and idx.get("map"):
        return idx["map"]
    try:
        html = _get(_BASE + "/school/")
    except Exception:
        return idx.get("map") or {}
    links = re.findall(r'href="/school/([a-z0-9-]+)"[^>]*>(.*?)</a>', html, re.S)
    mapping = {}
    for slug, raw in links:
        name = re.sub(r"<[^>]+>", " ", raw)
        name = re.sub(r"\s+", " ", name).strip()
        key = _norm(name)
        if key and key not in mapping:
            mapping[key] = {"slug": slug, "name": name}
    if mapping:
        d["index"] = {"fetched": time.time(), "map": mapping}
        _save()
    return mapping


def slug_for(school_name):
    """sgschooling slug for a school name (fuzzy), or None."""
    mapping = _index()
    key = _norm(school_name)
    if not key:
        return None
    hit = mapping.get(key)
    if hit:
        return hit["slug"]
    for k, v in mapping.items():           # containment fallback
        if key in k or k in key:
            return v["slug"]
    return None


def _cell(td_html):
    """One phase cell -> {taken, applied, vacancies, balloted, note}."""
    def num(pattern):
        m = re.search(pattern, td_html, re.S)
        if not m:
            return None
        t = re.sub(r"<[^>]+>", "", m.group(1))
        t = t.replace("&mdash;", "").replace("&middot;", "").strip()
        return int(t) if t.isdigit() else None

    taken = num(r'class="sc-taken"[^>]*>(.*?)</div>')
    sub = re.search(r'class="sc-sub"[^>]*>(.*?)</div>', td_html, re.S)
    applied = vac = None
    if sub:
        parts = re.sub(r"<[^>]+>", "|", sub.group(1)).split("|")
        nums = [p.strip() for p in parts if p.strip().isdigit()]
        if len(nums) >= 2:
            applied, vac = int(nums[0]), int(nums[1])
    note_m = re.search(r'class="sc-ballot"[^>]*>(.*?)</div>', td_html, re.S)
    note = None
    if note_m:
        note = re.sub(r"<[^>]+>", "", note_m.group(1))
        note = note.replace("&lt;", "<").replace("&gt;", ">").strip() or None
    balloted = bool(note) or "color:#b23b22" in td_html
    if taken is None and applied is None and vac is None:
        return None
    return {"taken": taken, "applied": applied, "vacancies": vac,
            "balloted": balloted, "note": note}


def history(school_name, years=8):
    """Phase-2C ballot history for a school, newest first (cached).

    Returns {"url", "years": [{year, taken, applied, vacancies, balloted,
    note, ratio}]} or None when the school can't be matched/fetched.
    """
    slug = slug_for(school_name)
    if not slug:
        return None
    d = _load()
    hit = d["schools"].get(slug)
    if hit and hit.get("fetched", 0) > time.time() - _TTL:
        data = hit.get("years")
    else:
        data = _fetch_history(slug)
        if data is not None:
            d["schools"][slug] = {"fetched": time.time(), "years": data}
            _save()
        elif hit:                       # fetch failed — serve stale cache
            data = hit.get("years")
    if not data:
        return None
    out = []
    for y in data[:years]:
        r = dict(y)
        a, v = r.get("applied"), r.get("vacancies")
        r["ratio"] = round(a / v, 2) if a and v else None
        out.append(r)
    return {"url": f"{_BASE}/school/{slug}", "years": out}


def _fetch_history(slug):
    """Parse the ballot-history table of one school page. None on failure."""
    try:
        html = _get(f"{_BASE}/school/{slug}")
    except Exception:
        return None
    # The history table is the one whose header starts "Year | Phase 1 | ...".
    for table in re.findall(r"<table[^>]*>.*?</table>", html, re.S):
        rows = re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.S)
        if not rows:
            continue
        header = [re.sub(r"<[^>]+>", "", c).strip()
                  for c in re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", rows[0], re.S)]
        if not header or header[0] != "Year" or "2C" not in header:
            continue
        col_2c = header.index("2C")
        years = []
        for row in rows[1:]:
            cells = re.findall(r"<t[hd][^>]*>(.*?)</t[hd]>", row, re.S)
            if len(cells) <= col_2c:
                continue
            ym = re.match(r"\s*(20\d\d)", re.sub(r"<[^>]+>", "", cells[0]))
            if not ym:
                continue
            parsed = _cell(cells[col_2c])
            if parsed:
                parsed["year"] = int(ym.group(1))
                years.append(parsed)
        years.sort(key=lambda r: -r["year"])
        return years
    return None
