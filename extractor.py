"""Core logic: fetch a PropertyGuru listing page with Playwright and parse
the fields of interest (block/street, size in sqft, nearest MRT + distance,
TOP year).

PropertyGuru sits behind Cloudflare's "Just a moment..." JS challenge, which
blocks plain HTTP requests (403) *and* headless Chromium. We therefore render
the page in a visible, real Chrome window (`channel="chrome"`), which passes the
challenge automatically.

The listing page is a Next.js app that embeds all its data in a
`<script id="__NEXT_DATA__">` blob, so parsing is done against that structured
JSON (with a text-regex fallback) rather than brittle CSS classes.
"""

import json
import os
import re
from bs4 import BeautifulSoup

FIELDS = [
    "block_street",
    "price",
    "size_sqft",
    "nearest_mrt",
    "mrt_distance",
    "top_year",
    "about",
    "listing_url",
]


class BlockedError(Exception):
    """Raised when the page could not get past the anti-bot challenge."""


# --------------------------------------------------------------------------- #
# Description skimming — flag decision-relevant keywords in the "about" text
# --------------------------------------------------------------------------- #

_NUM_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}


def skim_flags(text):
    """Scan a listing description for deal-relevant keywords.

    Returns a list of {"label", "tone"} dicts, tone one of:
      "good" (green), "warn" (amber), "info" (neutral).
    Empty list when nothing is mentioned.
    """
    if not text:
        return []
    t = re.sub(r"\s+", " ", text).lower()
    flags = []

    # --- Extension of stay (checked negative-first so both can't fire) ------
    if re.search(r"no\s+(?:need\s+(?:for\s+)?)?extension|without\s+extension"
                 r"|no\s+ext\b", t):
        flags.append({"label": "✓ No extension", "tone": "good"})
    else:
        m = re.search(
            r"(\d+|one|two|three|four|five|six)[\s-]*(?:months?|mths?)"
            r"[\s\w]{0,12}?extension"
            r"|extension\s+(?:of\s+stay\s+)?(?:of|for)\s+"
            r"(\d+|one|two|three|four|five|six)[\s-]*(?:months?|mths?)", t)
        if m:
            raw = next(g for g in m.groups() if g)
            n = _NUM_WORDS.get(raw, raw)
            flags.append({"label": f"🕐 {n} mth extension", "tone": "warn"})
        elif re.search(r"extension\s+(?:of\s+stay|required|needed)"
                       r"|require[sd]?\s+(?:an?\s+)?extension"
                       r"|seller[\s\w]{0,20}extension", t):
            flags.append({"label": "🕐 Extension required", "tone": "warn"})

    # --- Tenancy / vacancy ---------------------------------------------------
    m = re.search(r"tenanted\s+(?:un)?till?\s+([a-z]+\s+\d{4}|\d{4})", t)
    if m:
        flags.append({"label": f"👤 Tenanted until {m.group(1).title()}", "tone": "warn"})
    elif re.search(r"tenanted|with\s+tenanc?y|tenancy\s+in\s+place", t):
        flags.append({"label": "👤 Tenanted", "tone": "warn"})
    elif re.search(r"vacant\s+possession|vacant\s+unit|immediate\s+occupation", t):
        flags.append({"label": "✓ Vacant possession", "tone": "good"})

    # --- Renovation state (year-stamped when the text gives one) --------------
    if re.search(r"original\s+condition|un-?renovated", t):
        flags.append({"label": "🔨 Original condition", "tone": "info"})
    else:
        reno_yr = re.search(
            r"renovat\w*\s+(?:in\s+|done\s+(?:in\s+)?)?(20\d{2})"
            r"|(20\d{2})\s+renovat", t)
        if reno_yr:
            yr = reno_yr.group(1) or reno_yr.group(2)
            flags.append({"label": f"✨ Renovated {yr}", "tone": "good"})
        elif re.search(r"renovated|renovation\s+done|move[\s-]?in\s+condition", t):
            flags.append({"label": "✨ Renovated", "tone": "good"})

    # --- Facing & sun (big livability factor in SG) ----------------------------
    if re.search(r"north[\s/-]*south\s+facing|n[-/]s\s+facing", t):
        flags.append({"label": "🧭 N-S facing", "tone": "good"})
    elif re.search(r"no\s+(?:west|afternoon|noon)\s+sun", t):
        flags.append({"label": "🧭 No west sun", "tone": "good"})
    if re.search(r"unblock(?:ed)?\s+(?:\w+\s+)?view", t):
        flags.append({"label": "🌅 Unblocked view", "tone": "good"})
    else:
        vm = re.search(r"(greenery|park|waterway|river|sea|reservoir|pool)\s+view", t)
        if vm:
            flags.append({"label": f"🌳 {vm.group(1).title()} view", "tone": "info"})
    if re.search(r"quiet\s+(?:environment|facing|unit)"
                 r"|not\s+facing\s+(?:any\s+)?(?:main\s+)?road", t):
        flags.append({"label": "🤫 Quiet facing", "tone": "info"})

    # --- Unit traits ------------------------------------------------------------
    if re.search(r"corner\s+(?:unit|flat)", t):
        flags.append({"label": "📐 Corner unit", "tone": "info"})
    lvl = re.search(r"(?:above|level|lvl|storey)\s*#\s*0?(\d{1,2})\b", t)
    if lvl:
        flags.append({"label": f"🏢 Level {lvl.group(1)}+", "tone": "info"})
    elif re.search(r"high\s+floor", t):
        flags.append({"label": "🏢 High floor", "tone": "info"})
    elif re.search(r"low\s+floor|ground\s+floor", t):
        flags.append({"label": "🏢 Low floor", "tone": "info"})
    if re.search(r"lift\s+(?:level|landing)", t):
        flags.append({"label": "🛗 Lift level", "tone": "info"})
    fm = re.search(r"\b(maisonette|jumbo|penthouse|dbss|loft|adjoined)\b", t)
    if fm:
        flags.append({"label": f"🏠 {fm.group(1).upper() if fm.group(1) == 'dbss' else fm.group(1).title()}",
                      "tone": "info"})

    # --- Sale urgency ------------------------------------------------------------
    if re.search(r"urgent\s+sale|must\s+sell|priced?\s+to\s+sell|below\s+valuation", t):
        flags.append({"label": "🔥 Urgent sale", "tone": "info"})

    return flags


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #

import random
import threading
import time as _time

# Persistent Chrome profile: keeps cookies (incl. the Cloudflare clearance)
# across runs, so once a challenge is passed we stop being re-challenged.
_PROFILE_DIR = os.path.join(os.path.dirname(__file__), ".browser_profile")
# One browser session at a time (a persistent profile can't be shared).
_BROWSER_LOCK = threading.Lock()


def _clean_profile_locks():
    """Remove stale Chrome singleton locks left by a crashed/killed run.
    Safe because _BROWSER_LOCK serialises our own launches."""
    import glob
    for f in glob.glob(os.path.join(_PROFILE_DIR, "Singleton*")):
        try:
            os.remove(f)
        except OSError:
            pass


def _new_browser(p, headless):
    """Launch a persistent-profile browser context, preferring real Chrome.

    Returns (closable, context) — closing the context closes the browser.
    """
    _clean_profile_locks()
    kwargs = {
        "headless": headless,
        "args": ["--disable-blink-features=AutomationControlled"],
        "viewport": {"width": 1440, "height": 900},
        "locale": "en-SG",
    }
    try:
        ctx = p.chromium.launch_persistent_context(
            _PROFILE_DIR, channel="chrome", **kwargs)
    except Exception:
        ctx = p.chromium.launch_persistent_context(_PROFILE_DIR, **kwargs)
    ctx.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined})"
    )
    return ctx, ctx


def _pace():
    """Human-ish gap between page loads in a batch."""
    _time.sleep(random.uniform(1.2, 2.8))


def _warm_up(context, origin="https://www.propertyguru.com.sg/"):
    """Visit the site homepage once to acquire Cloudflare clearance before
    hitting a deep/filtered URL (which is challenged far more aggressively).
    Any challenge is then handled once, on the plain homepage."""
    try:
        _load_html(context, origin)
    except Exception:
        pass


def _load_html(context, url, timeout_ms=45000):
    """Navigate one page, letting a Cloudflare challenge clear if it appears.

    Everything we parse (PG __NEXT_DATA__, SRX microdata) is server-rendered in
    the initial HTML. With the persistent profile a challenge should only appear
    on the first visit; we give it ample time and retry once before failing.
    """
    page = context.new_page()
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        waited = 0
        challenged = "moment" in page.title().lower()
        if challenged:
            # Interactive challenge: surface the window so the user can click
            # the verification once; the persistent profile remembers it.
            try:
                page.bring_to_front()
            except Exception:
                pass
        while "moment" in page.title().lower():
            if waited >= 120000:
                raise BlockedError(
                    "Cloudflare verification not completed. A Chrome window "
                    "shows a verification checkbox — click it once, then retry."
                )
            page.wait_for_timeout(2000)
            waited += 2000
        # Small human-like settle + scroll before reading the page.
        page.wait_for_timeout(random.randint(800, 1600))
        page.mouse.wheel(0, random.randint(800, 2400))
        page.wait_for_timeout(random.randint(300, 700))
        return page.content()
    finally:
        page.close()


def fetch_html(url, headless=False, timeout_ms=45000):
    """Render a single listing page and return its HTML.

    Uses a real Chrome window by default (headless=False) because Cloudflare
    blocks headless Chromium. Playwright is imported lazily so the module can be
    imported without the browser installed.
    """
    from playwright.sync_api import sync_playwright

    with _BROWSER_LOCK, sync_playwright() as p:
        browser, context = _new_browser(p, headless)
        try:
            return _load_html(context, url, timeout_ms)
        finally:
            browser.close()


# --------------------------------------------------------------------------- #
# Parsing
# --------------------------------------------------------------------------- #

def _next_data(soup):
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag and tag.string:
        try:
            return json.loads(tag.string)
        except Exception:
            return None
    return None


def _clean(t):
    return re.sub(r"\s+", " ", t).strip() if isinstance(t, str) else t


def _dig(obj, *keys):
    """Safely walk nested dict keys, returning None on any miss."""
    cur = obj
    for k in keys:
        if isinstance(cur, dict) and k in cur:
            cur = cur[k]
        else:
            return None
    return cur


# Extra (non-display) keys carried alongside FIELDS for the map.
MAP_KEYS = ["block_lat", "block_lon", "stations", "schools"]
# Basic detail keys available even from a search-results card (no page visit).
DETAIL_KEYS = ["beds", "baths", "psf", "thumbnail", "listed_date", "town"]


def _blank_output():
    out = dict.fromkeys(FIELDS)
    out["block_lat"] = None
    out["block_lon"] = None
    out["stations"] = []
    out["schools"] = []
    out["images"] = []
    out["agent"] = {}
    for k in DETAIL_KEYS:
        out[k] = None
    return out


def _poi_point(s):
    """Normalise one point-of-interest entry to {name, lat, lon, distance_m,
    walk_mins}, or None if it lacks coordinates."""
    pt = s.get("point") or {}
    if not (pt.get("lat") and pt.get("lon")):
        return None
    km = s.get("walkingDistanceKm") or s.get("distanceKm")
    return {
        "name": _clean(s.get("name")) or "",
        "lat": pt["lat"],
        "lon": pt["lon"],
        "distance_m": int(round(km * 1000)) if km else None,
        "walk_mins": s.get("walkingDurationMins"),
    }


def _parse_from_next(nxt):
    """Extract fields from the __NEXT_DATA__ blob. Missing fields stay None."""
    data = _dig(nxt, "props", "pageProps", "pageData", "data") or {}
    out = _blank_output()

    # Block coordinates (exact, straight from the listing).
    point = _dig(data, "listingDetail", "location", "point") or {}
    if point.get("lat") and point.get("lon"):
        out["block_lat"] = point["lat"]
        out["block_lon"] = point["lon"]

    # Block + street name.
    addr = _dig(data, "listingDetail", "location", "address") or {}
    if addr.get("formatted"):
        out["block_street"] = _clean(addr["formatted"])
    else:
        num = addr.get("streetNumber") or ""
        street = _dig(data, "listingDetail", "location", "streetName") or ""
        combined = f"{num} {street}".strip()
        out["block_street"] = _clean(combined) or None

    # Price (formatted).
    out["price"] = (
        _dig(data, "listingData", "pricePretty")
        or _dig(data, "propertyOverviewData", "propertyInfo", "price", "amount")
        or None
    )

    # Size in sqft.
    out["size_sqft"] = _dig(data, "listingData", "floorAreaText") or None

    # Beds / baths / PSF / thumbnail (from the property-overview block).
    out["psf"] = _dig(data, "propertyOverviewData", "propertyInfo", "price", "perSqft")
    for a in (_dig(data, "propertyOverviewData", "propertyInfo", "amenities") or []):
        unit = str(a.get("unit", "")).lower()
        if "bed" in unit:
            out["beds"] = str(a.get("value"))
        elif "bath" in unit:
            out["baths"] = str(a.get("value"))

    # Full photo gallery (urlTemplate carries a ${viewType} size placeholder).
    gallery = []
    for im in (_dig(data, "listingDetail", "media", "listingImages") or []):
        t = im.get("urlTemplate") or ""
        if t:
            gallery.append({"url": t.replace("${viewType}", "V550"),
                            "caption": _clean(im.get("caption")) or ""})
    out["images"] = gallery
    if gallery and not out.get("thumbnail"):
        out["thumbnail"] = gallery[0]["url"]

    # When the listing was first posted.
    fp = _dig(data, "listingDetail", "dates", "firstPosted", "date")
    if fp:
        out["listed_date"] = str(fp)[:10]

    # Town (HDB estate), for grouping by area.
    out["town"] = _dig(data, "listingDetail", "location", "hdbEstate", "text")

    # Listing (seller's) agent contact.
    ag = _dig(data, "listingDetail", "lister", "metaByType", "agent") or {}
    if ag.get("name"):
        phone = None
        for c in (ag.get("contacts") or []):
            if c.get("type") == "mobile":
                phone = c.get("pretty") or c.get("value")
                break
        if not phone and ag.get("contacts"):
            phone = ag["contacts"][0].get("pretty") or ag["contacts"][0].get("value")
        out["agent"] = {
            "name": _clean(ag.get("name")),
            "license": ag.get("license"),
            "phone": phone,
            "title": _clean(ag.get("jobTitle")),
        }

    # "About this property" description (subtitle headline + body, HTML stripped).
    desc = _dig(data, "descriptionBlockData") or {}
    subtitle = _clean(desc.get("subtitle"))
    body = desc.get("description")
    body_text = None
    if body:
        # Turn <br> into spaces before stripping the rest of the markup.
        body_text = _clean(
            BeautifulSoup(re.sub(r"<br\s*/?>", " ", body), "html.parser").get_text(" ")
        )
    about = " — ".join([p for p in (subtitle, body_text) if p]) or None
    out["about"] = about

    # TOP year — search all project meta variants for a completion year.
    meta = _dig(data, "listingDetail", "project", "metaByType")
    if isinstance(meta, dict):
        for variant in meta.values():
            year = variant.get("completionYear") if isinstance(variant, dict) else None
            if year:
                out["top_year"] = str(year)
                break

    # Nearest MRT station + distance. The list is sorted nearest-first;
    # prefer an actual "MRT" station, else fall back to the nearest transit stop.
    mrt_list = _dig(data, "listingDetail", "pointOfInterest", "mrt")
    if isinstance(mrt_list, list) and mrt_list:
        station = next(
            (s for s in mrt_list if "mrt" in str(s.get("name", "")).lower()),
            mrt_list[0],
        )
        out["nearest_mrt"] = _clean(station.get("name"))
        km = station.get("walkingDistanceKm") or station.get("distanceKm")
        mins = station.get("walkingDurationMins")
        parts = []
        if km is not None:
            parts.append(f"{int(round(km * 1000))} m")
        if mins:
            parts.append(f"~{mins} min walk")
        out["mrt_distance"] = " · ".join(parts) or None

    # Full list of nearby MRT/LRT stations with coordinates (for the map).
    if isinstance(mrt_list, list):
        stations = []
        for s in mrt_list:
            p = _poi_point(s)
            if p:
                p["type"] = "MRT" if "mrt" in p["name"].lower() else "LRT"
                stations.append(p)
        out["stations"] = stations

    # Nearby government primary schools (within ~1 km; else nearest 3).
    school_list = _dig(data, "listingDetail", "pointOfInterest", "schools")
    if isinstance(school_list, list):
        primary = [s for s in school_list
                   if "primary" in str(s.get("subcategory", "")).lower()]
        within = [s for s in primary if (s.get("distanceKm") or 99) <= 1.0]
        chosen = within or primary[:3]
        out["schools"] = [p for p in (_poi_point(s) for s in chosen) if p]

    return out


def _parse_fallback(soup, out):
    """Fill any still-missing fields from visible page text (regex)."""
    text = soup.get_text(" ", strip=True)
    if not out.get("block_street"):
        h1 = soup.find("h1")
        if h1:
            out["block_street"] = _clean(h1.get_text())
    if not out.get("size_sqft"):
        m = re.search(r"([\d,]{2,})\s*(?:sq\s*ft|sqft)", text, re.I)
        if m:
            out["size_sqft"] = f"{m.group(1)} sqft"
    if not out.get("top_year"):
        m = re.search(r"TOP\s*(?:\(.*?\))?\s*[:\-]?\s*((?:19|20)\d{2})", text, re.I)
        if m:
            out["top_year"] = m.group(1)
    if not out.get("nearest_mrt"):
        # Require the full "X MRT Station" form so widget/nav text like
        # "Your Location MRT" can't slip in.
        m = re.search(r"([A-Z][A-Za-z' ]+?\s+MRT\s+Station)", text)
        if m:
            out["nearest_mrt"] = _clean(m.group(1))
    if not out.get("price"):
        m = re.search(r"S\$\s?[\d,]+", text)
        if m:
            out["price"] = _clean(m.group(0))
    if not out.get("listing_url"):
        canon = soup.find("link", rel="canonical")
        og = soup.find("meta", property="og:url")
        out["listing_url"] = (
            (canon and canon.get("href"))
            or (og and og.get("content"))
            or None
        )
    if not out.get("thumbnail"):
        ogimg = soup.find("meta", property="og:image")
        if ogimg and ogimg.get("content"):
            out["thumbnail"] = ogimg["content"]
    return out


SQM_TO_SQFT = 10.7639


def _parse_srx(soup):
    """Extract fields from an SRX listing page (schema.org microdata)."""
    out = _blank_output()

    props = {}
    for ap in soup.select("[itemprop=additionalProperty]"):
        n = ap.select_one("[itemprop=name]")
        v = ap.select_one("[itemprop=value]")
        if n and v:
            props[n.get_text(strip=True)] = _clean(v.get_text(" ", strip=True))

    # Address comes as "451A Sengkang West Way (791451)" — postal code included.
    addr = props.get("Address")
    if addr:
        out["block_street"] = _clean(re.sub(r"\s*\(\d{6}\)\s*$", "", addr))
        m = re.search(r"\((\d{6})\)", addr)
        if m:
            out["postal_code"] = m.group(1)  # extra key, used for geocoding

    price = soup.select_one("[itemprop=price]")
    if price and (price.get("content") or "").isdigit():
        out["price"] = f"S$ {int(price['content']):,}"

    size = props.get("Size") or ""
    m = re.search(r"([\d.]+)\s*sqm", size)
    if m:
        out["size_sqft"] = f"{round(float(m.group(1)) * SQM_TO_SQFT):,} sqft"
    elif "sqft" in size:
        out["size_sqft"] = size

    by = props.get("Built Year") or ""
    m = re.search(r"(19|20)\d{2}", by)
    if m:
        out["top_year"] = m.group(0)

    out["beds"] = props.get("Bedrooms")
    out["baths"] = props.get("Bathrooms")
    out["psf"] = props.get("PSF")

    # Listed date, e.g. "08-Apr-2026".
    out["listed_date"] = props.get("Date Listed")
    out["town"] = props.get("HDB Town")

    # Listing agent (SRX masks the phone, e.g. "9691 XXXX").
    text = soup.get_text(" ", strip=True)
    am = re.search(r"Hi I'?m ([A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+)+)", text)
    ceam = re.search(r"CEA:\s*([A-Z0-9]+(?:\s*\|\s*[A-Z0-9]+)?)", text)
    if am or ceam:
        name = am.group(1) if am else None
        ph = None
        if name:
            pm = re.search(re.escape(name) + r"\s*([0-9]{4}\s?[0-9X]{3,4})", text)
            ph = _clean(pm.group(1)) if pm else None
        out["agent"] = {
            "name": name,
            "license": ceam.group(1).strip() if ceam else None,
            "phone": ph,
            "title": None,
        }

    # Photo gallery: <img> tags under "Listing Photos/<listing-id>/...".
    og = soup.find("meta", property="og:url")
    lid_m = re.search(r"/listings/(\d+)", (og.get("content") if og else "") or "")
    lid = lid_m.group(1) if lid_m else None
    seen, gallery = set(), []
    for img in soup.find_all("img", src=True):
        src = img["src"]
        if "Listing Photos" not in src:
            continue
        if lid and f"/{lid}/" not in src:
            continue
        pid = re.search(r"/(\d+)(?:_mobile)?\.jpe?g", src)
        key = pid.group(1) if pid else src
        if key in seen:
            continue
        seen.add(key)
        gallery.append({"url": src.replace("/S/", "/L/").replace("_mobile", ""),
                        "caption": ""})
    out["images"] = gallery
    if gallery and not out.get("thumbnail"):
        out["thumbnail"] = gallery[0]["url"]

    desc = soup.select_one(".listing-description")
    if desc:
        out["about"] = _clean(desc.get_text(" ", strip=True))

    og = soup.find("meta", property="og:url")
    if og and og.get("content"):
        out["listing_url"] = og["content"]

    return out


def parse(html):
    """Parse rendered HTML into the field dict, auto-detecting the site.
    Missing fields are None."""
    soup = BeautifulSoup(html, "html.parser")
    nxt = _next_data(soup)
    if nxt:                                   # PropertyGuru (Next.js)
        out = _parse_from_next(nxt)
    elif soup.select_one("[itemprop=additionalProperty], .listing-description"):
        out = _parse_srx(soup)                # SRX (microdata)
    else:
        out = _blank_output()
    return _parse_fallback(soup, out)


def is_search_url(url):
    """True for a PropertyGuru search-results page (not a single listing)."""
    u = (url or "").lower()
    return "propertyguru" in u and "/listing/" not in u and (
        "property-for-sale" in u or "property-for-rent" in u
        or "/search" in u or "freetext=" in u or "listing-type=" in u
    )


def _light_from_card(ld):
    """Build a light result dict from one search-card listingData object."""
    out = _blank_output()
    out["listing_url"] = ld.get("url")
    out["block_street"] = _clean(
        ld.get("fullAddress") or ld.get("shortAddress") or ld.get("localizedTitle")
    ) or None
    out["price"] = _dig(ld, "price", "pretty") or None
    out["size_sqft"] = _dig(ld, "area", "localeStringValue") or (
        f"{ld['floorArea']:,} sqft" if ld.get("floorArea") else None)
    out["beds"] = str(ld["bedrooms"]) if ld.get("bedrooms") is not None else None
    out["baths"] = str(ld["bathrooms"]) if ld.get("bathrooms") is not None else None
    out["psf"] = _clean(ld.get("psfText")) or None
    out["thumbnail"] = ld.get("thumbnail") or None
    posted = ld.get("postedOn") or {}
    if posted.get("unix"):
        from datetime import datetime
        out["listed_date"] = datetime.fromtimestamp(posted["unix"]).strftime("%Y-%m-%d")
    mrt = ld.get("mrt")
    if isinstance(mrt, dict):
        out["nearest_mrt"] = _clean(mrt.get("name")) or None
    return out


def parse_search(html):
    """Parse a PropertyGuru search-results page into a list of light result
    dicts (one per listing card), using the embedded __NEXT_DATA__."""
    soup = BeautifulSoup(html, "html.parser")
    nxt = _next_data(soup)
    cards = _dig(nxt, "props", "pageProps", "pageData", "data", "listingsData") or []
    out = []
    for c in cards:
        ld = c.get("listingData") if isinstance(c, dict) else None
        if isinstance(ld, dict) and ld.get("url"):
            out.append(_light_from_card(ld))
    return out


def extract_search(url, headless=False):
    """Fetch a search-results page and return light result dicts."""
    return parse_search(fetch_html(url, headless=headless))


def _set_page(url, n):
    """Return `url` with its `page` query param set to n (added if absent).

    Preserves repeated params (e.g. floorLevel=HIGH&floorLevel=PENT) and their
    order — only the `page` value is replaced.
    """
    from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse
    p = urlparse(url)
    pairs = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if k != "page"]
    pairs.append(("page", str(n)))
    return urlunparse(p._replace(query=urlencode(pairs)))


def _search_total_pages(html):
    nxt = _next_data(BeautifulSoup(html, "html.parser"))
    pg = _dig(nxt, "props", "pageProps", "pageData", "data", "paginationData") or {}
    try:
        return int(pg.get("totalPages") or 1)
    except Exception:
        return 1


def extract_search_all(url, max_pages=20, headless=False, on_page=None):
    """Walk every page of a search (up to max_pages), reusing ONE browser, and
    return all unique light listings.

    Returns {"listings", "total_pages", "fetched_pages", "capped"}.
    `on_page(page, pages_to_fetch, count_so_far)` fires after each page.
    """
    from playwright.sync_api import sync_playwright

    listings, seen, total_pages = [], set(), 1
    with _BROWSER_LOCK, sync_playwright() as p:
        browser, context = _new_browser(p, headless)
        try:
            _warm_up(context)          # clear Cloudflare on the homepage first
            page_n = 1
            while page_n <= max_pages:
                html = _load_html(context, _set_page(url, page_n))
                if page_n == 1:
                    total_pages = _search_total_pages(html)
                for r in parse_search(html):
                    key = _listing_key(r.get("listing_url"))
                    if key in seen:
                        continue
                    seen.add(key)
                    listings.append(r)
                if on_page:
                    on_page(page_n, min(total_pages, max_pages), len(listings))
                if page_n >= total_pages:
                    break
                page_n += 1
                _pace()
        finally:
            browser.close()
    return {
        "listings": listings,
        "total_pages": total_pages,
        "fetched_pages": min(total_pages, max_pages),
        "capped": total_pages > max_pages,
    }


def _listing_key(url):
    m = re.findall(r"(\d{6,})", url or "")
    return m[-1] if m else (url or "")


def extract(url, headless=False):
    """Fetch + parse a listing URL. Returns the field dict."""
    return parse(fetch_html(url, headless=headless))


def extract_many(urls, headless=False, on_result=None):
    """Extract several listings, reusing ONE browser for the whole batch.

    Returns a list of dicts: {"url", "ok", "result"} on success or
    {"url", "ok": False, "error"} on failure. `on_result`, if given, is called
    with each item as it completes (useful for progress/streaming).
    """
    from playwright.sync_api import sync_playwright

    items = []
    with _BROWSER_LOCK, sync_playwright() as p:
        browser, context = _new_browser(p, headless)
        try:
            for i, url in enumerate(urls):
                url = (url or "").strip()
                if not url:
                    continue
                if i:
                    _pace()
                try:
                    result = parse(_load_html(context, url))
                    item = {"url": url, "ok": True, "result": result}
                except Exception as e:  # noqa: BLE001 — record per-URL failure
                    item = {"url": url, "ok": False, "error": str(e)}
                items.append(item)
                if on_result:
                    on_result(item)
        finally:
            browser.close()
    return items


if __name__ == "__main__":
    import sys

    target = sys.argv[1] if len(sys.argv) > 1 else (
        "https://www.propertyguru.com.sg/listing/"
        "hdb-for-sale-261b-sengkang-east-way-500060706"
    )
    print(json.dumps(extract(target), indent=2))
