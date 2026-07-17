"""P1 registration vacancy / balloting data (MOE).

Scrapes MOE's "past vacancies and balloting data" page once (18 paginated
pages, ~179 schools) and caches it locally as JSON. Each school's card gives,
per phase, the vacancies / applicants / whether balloting happened.

Why Phase 1 and 2C matter to a home buyer:
  * Phase 1  — child has a sibling in the school. Home address irrelevant.
  * Phase 2C — open to everyone else; this is where HOME DISTANCE decides.
               Within 1 km gets first priority in the ballot, then 1–2 km.
So "will we get in?" for a normal family ≈ how contested Phase 2C was.
"""

import json
import os
import re
import time

_CACHE = os.path.join(os.path.dirname(__file__), "p1_vacancies.json")
_URL = ("https://www.moe.gov.sg/primary/p1-registration/"
        "past-vacancies-and-balloting-data")
_TTL = 30 * 24 * 3600           # refresh monthly; MOE publishes yearly
_mem = None


def _norm(name):
    """Loose key for matching MOE names to PropertyGuru's school names."""
    n = (name or "").lower()
    n = re.sub(r"\(.*?\)", " ", n)
    n = re.sub(r"\b(primary|school|pri)\b", " ", n)
    n = re.sub(r"[^a-z0-9]+", "", n)
    return n


def _num(text):
    m = re.search(r"-?\d[\d,]*", text or "")
    return int(m.group(0).replace(",", "")) if m else None


def _cutoff_from(note):
    """Distance cut-off named in a balloting note, if any."""
    if re.search(r"between 1\s*km and 2\s*km", note, re.I):
        return "1-2km"
    if re.search(r"within 1\s*km", note, re.I):
        return "1km"
    if re.search(r"more than 2\s*km|beyond 2\s*km|outside 2\s*km", note, re.I):
        return ">2km"
    if re.search(r"within 2\s*km", note, re.I):
        return "2km"
    return None


def _parse_card(card):
    """Pull each phase's figures out of one school card.

    Parses each `__phase` DOM block separately, so prose that merely mentions a
    phase name ("…filled by Phase 2C") can never corrupt another section.
    """
    name_el = card.select_one(".moe-vacancies-ballot-card__school")
    if not name_el:
        return None
    name = name_el.get_text(" ", strip=True)
    year_el = card.select_one("[class*=moe-vacancies-ballot-card__year]")
    year_m = re.search(r"20\d{2}", year_el.get_text() if year_el else "")

    phases = {}
    for ph in card.select(".moe-vacancies-ballot-card__phase"):
        seg = ph.get_text(" | ", strip=True)
        h = re.match(r"Phase (1|2A|2B|2C Supplementary|2C)\b", seg)
        if not h or h.group(1) in phases:
            continue
        key = h.group(1)
        if re.search(r"All eligible applicants were offered a place", seg, re.I):
            phases[key] = {"balloted": False, "all_offered": True}
            continue
        vac = _num((re.search(r"Vacancies \| ([^|]+)", seg) or [None, ""])[1])
        app = _num((re.search(r"Applicants \| ([^|]+)", seg) or [None, ""])[1])
        ballot_m = re.search(r"Balloting: \| (Yes|No)", seg)
        entry = {
            "vacancies": vac,
            "applicants": app,
            "balloted": (ballot_m.group(1) == "Yes") if ballot_m else None,
        }
        bv = re.search(r"Vacancies for ballot \| ([^|]+)", seg)
        ba = re.search(r"Balloting applicants \| ([^|]+)", seg)
        if bv:
            entry["ballot_vacancies"] = _num(bv.group(1))
        if ba:
            entry["ballot_applicants"] = _num(ba.group(1))
        # The sentence after "Balloting: Yes/No" carries the DISTANCE CUT-OFF —
        # without it, "Balloting: No" is ambiguous (everyone got in, OR distance
        # alone excluded the rest).
        # The note may span several text nodes — take everything up to the
        # ballot figures (or the end of the block) and re-join it.
        note_m = re.search(
            r"Balloting: \| (?:Yes|No) \| (.+?)(?:\| Vacancies for ballot|\| See school|$)",
            seg)
        if note_m:
            note = re.sub(r"\s*\|\s*", " ", note_m.group(1)).strip()
            entry["note"] = note
            cut = _cutoff_from(note)
            if cut:
                entry["cutoff"] = cut
            if re.search(r"offered to all Singapore Citizen children", note, re.I):
                entry["all_sc_offered"] = True
        phases[key] = entry
    if not phases:
        return None
    return {
        "name": name,
        "year": year_m.group(0) if year_m else None,
        "phases": phases,
    }


def refresh(headless=False):
    """Walk every page of the MOE table and cache the result. Returns the dict."""
    from bs4 import BeautifulSoup
    from playwright.sync_api import sync_playwright
    from extractor import _BROWSER_LOCK, _new_browser

    schools = {}
    with _BROWSER_LOCK, sync_playwright() as p:
        browser, ctx = _new_browser(p, headless)
        try:
            page = ctx.new_page()
            page.goto(_URL, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(3000)
            def has_2c(rec):
                p = (rec or {}).get("phases", {}).get("2C") or {}
                return p.get("vacancies") is not None or p.get("all_offered")

            seen_pages = 0
            while seen_pages < 25:                       # 18 pages + slack
                page.wait_for_timeout(700)               # let cards finish rendering
                soup = BeautifulSoup(page.content(), "html.parser")
                for card in soup.select("[class*=moe-vacancies-ballot-card]"):
                    if "moe-vacancies-ballot-card" not in (card.get("class") or []):
                        continue
                    rec = _parse_card(card)
                    if not rec:
                        continue
                    key = _norm(rec["name"])
                    # Never let a half-rendered card clobber a complete one.
                    if key in schools and has_2c(schools[key]) and not has_2c(rec):
                        continue
                    schools[key] = rec
                seen_pages += 1
                nxt = page.query_selector(".btn-pag-next")
                if not nxt or nxt.is_disabled():
                    break
                before = page.inner_text(".moe-vacancies-ballot-card__school")
                nxt.click()
                # Wait for the first card to change (page turned).
                for _ in range(20):
                    page.wait_for_timeout(300)
                    if page.inner_text(".moe-vacancies-ballot-card__school") != before:
                        break
                else:
                    break
        finally:
            browser.close()

    data = {"fetched": time.time(), "schools": schools}
    with open(_CACHE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    global _mem
    _mem = data
    return data


def _load():
    global _mem
    if _mem:
        return _mem
    if os.path.exists(_CACHE):
        try:
            with open(_CACHE, encoding="utf-8") as f:
                _mem = json.load(f)
                return _mem
        except Exception:
            pass
    return None


def is_stale():
    d = _load()
    return (not d) or (time.time() - d.get("fetched", 0) > _TTL)


def lookup(school_name):
    """P1 data for one school name (fuzzy-matched), or None."""
    d = _load()
    if not d:
        return None
    key = _norm(school_name)
    if key in d["schools"]:
        return d["schools"][key]
    for k, v in d["schools"].items():          # containment fallback
        if key and (key in k or k in key):
            return v
    return None


def summarise(rec):
    """Turn one school's phases into a buyer-facing verdict for Phase 2C."""
    if not rec:
        return None
    p1 = rec["phases"].get("1") or {}
    p2c = rec["phases"].get("2C") or {}
    vac, app = p2c.get("vacancies"), p2c.get("applicants")
    ratio = (app / vac) if (vac and app) else None
    balloted, cut = p2c.get("balloted"), p2c.get("cutoff")
    # "Balloting: No" alone is NOT "everyone got in" — check the distance cut-off.
    if p2c.get("all_offered"):
        verdict, tone = "All eligible got in", "good"
    elif balloted and cut == "1km":
        verdict, tone = "Balloted even within 1 km — hardest", "warn"
    elif balloted and cut == "1-2km":
        verdict, tone = "Within 1 km safe · balloted at 1–2 km", "warn"
    elif balloted and cut == ">2km":
        verdict, tone = "Within 2 km safe · balloted beyond 2 km", "info"
    elif balloted:
        verdict, tone = "Balloted", "warn"
    elif cut == "1km":
        verdict, tone = "No ballot, but only within 1 km got places", "warn"
    elif cut in ("1-2km", "2km"):
        verdict, tone = "No ballot, but only within 2 km got places", "info"
    elif balloted is False and (p2c.get("all_sc_offered")
                                or (app is not None and vac is not None and app <= vac)):
        verdict, tone = "No ballot — all got in", "good"
    elif balloted is False:
        verdict, tone = "No ballot", "info"
    else:
        verdict, tone = "No Phase 2C data", "info"
    return {
        "name": rec["name"],
        "year": rec.get("year"),
        "p1_vacancies": p1.get("vacancies"),
        "p1_applicants": p1.get("applicants"),
        "p1_all_offered": p1.get("all_offered", False),
        "p2c_vacancies": vac,
        "p2c_applicants": app,
        "p2c_balloted": p2c.get("balloted"),
        "p2c_ballot_vacancies": p2c.get("ballot_vacancies"),
        "p2c_ballot_applicants": p2c.get("ballot_applicants"),
        "p2c_cutoff": cut,
        "p2c_note": p2c.get("note"),
        "ratio": round(ratio, 2) if ratio else None,
        "verdict": verdict,
        "tone": tone,
    }
