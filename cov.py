"""Potential COV (Cash Over Valuation) estimation.

Since 2014 HDB only values a flat AFTER the price is agreed, so buyers gauge
COV risk by comparing the asking price against recent official resale
transactions of similar flats nearby. This module pulls those transactions from
data.gov.sg (HDB resale registrations, updated monthly) and estimates:

    potential COV ≈ asking price − median comparable transacted price

Comparables: same block if ≥3 in the window, else same street; similar floor
area (±10%); last 12 months (widened to 24 if thin).  COV is payable in CASH.
"""

import json
import re
import statistics
import time
from datetime import date

import requests

_RESOURCE = "d_8b84c4ee58e3cfc0ece0d773c8ca6abc"   # HDB resale from Jan-2017
_API = "https://data.gov.sg/api/action/datastore_search"
_TTL = 12 * 3600
_cache = {}          # STREET NAME -> (fetched_at, records)

# The dataset abbreviates street words ("ANCHORVALE RD", "ANG MO KIO AVE 10").
_ABBREV = {
    "ROAD": "RD", "STREET": "ST", "AVENUE": "AVE", "DRIVE": "DR",
    "CRESCENT": "CRES", "CLOSE": "CL", "CENTRAL": "CTRL", "PLACE": "PL",
    "GARDENS": "GDNS", "HEIGHTS": "HTS", "TERRACE": "TER", "NORTH": "NTH",
    "SOUTH": "STH", "BUKIT": "BT", "UPPER": "UPP", "LORONG": "LOR",
    "JALAN": "JLN", "COMMONWEALTH": "C'WEALTH", "MARKET": "MKT",
    "TANJONG": "TG", "KAMPONG": "KG",
}


def _normalize_street(street):
    words = (street or "").upper().split()
    return " ".join(_ABBREV.get(w, w) for w in words)


def split_block_street(block_street):
    """'261B Sengkang East Way' -> ('261B', 'SENGKANG EAST WAY')."""
    m = re.match(r"\s*(\d+[A-Z]?)\s+(.+)", block_street or "")
    if not m:
        return None, _normalize_street(block_street)
    return m.group(1).upper(), _normalize_street(m.group(2))


def _street_records(street):
    now = time.time()
    hit = _cache.get(street)
    if hit and now - hit[0] < _TTL:
        return hit[1]
    try:
        r = requests.get(_API, params={
            "resource_id": _RESOURCE,
            "filters": json.dumps({"street_name": street}),
            "limit": 4000,
        }, timeout=25)
        recs = r.json()["result"]["records"]
        _cache[street] = (now, recs)
        return recs
    except Exception:
        return None


def _month_ago(months):
    d = date.today()
    y, m = d.year, d.month - months
    while m <= 0:
        y, m = y - 1, m + 12
    return f"{y:04d}-{m:02d}"


def _month_idx(ym):
    y, m = ym.split("-")
    return int(y) * 12 + int(m)


def _storey_mid(storey_range):
    """'07 TO 09' -> 8."""
    nums = re.findall(r"\d+", storey_range or "")
    return (int(nums[0]) + int(nums[-1])) / 2 if nums else None


def _floor_band(about_text):
    """Detect the listing's floor level from its description."""
    t = (about_text or "").lower()
    if re.search(r"high(?:er)?[\s-]*(?:floor|level|storey)|penthouse|top floor", t):
        return "high"
    if re.search(r"low(?:er)?[\s-]*(?:floor|level|storey)|ground floor", t):
        return "low"
    if re.search(r"mid(?:dle)?[\s-]*(?:floor|level|storey)", t):
        return "mid"
    return None


def _street_drift(recs, sqm_ok):
    """%/month price drift on this street (simple OLS over 24 months of
    similar-size transactions). Clipped to ±1.5%/mo; None if too thin."""
    pool = [r for r in recs if r["month"] >= _month_ago(24) and sqm_ok(r)]
    if len(pool) < 8:
        return None
    pts = [(_month_idx(r["month"]), int(r["resale_price"])) for r in pool]
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((x - mx) ** 2 for x, _ in pts)
    if not denom:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / denom   # $/month
    drift = slope / my
    return max(-0.015, min(0.015, drift))


def estimate(block_street, size_sqft, asking_price, about=None):
    """Return a COV-estimate dict, or None when no data is available.

    Valuer-style pipeline: tight size band → same-block preference →
    floor-level match → trend-adjust comps to today → trim outliers → median.
    """
    block, street = split_block_street(block_street)
    if not street:
        return None
    recs = _street_records(street)
    if recs is None:
        return None
    if not recs:
        return {"count": 0, "street": street}

    sqm = None
    m = re.search(r"[\d,.]+", str(size_sqft or ""))
    if m:
        try:
            sqm = float(m.group(0).replace(",", "")) / 10.7639
        except ValueError:
            pass

    adjustments = []

    # 1) Size: same flat type (±3 sqm) preferred, else ±10%.
    def near(r, tol):
        return sqm is None or abs(float(r["floor_area_sqm"]) - sqm) <= tol

    def sized(pool_, months_):
        cut = _month_ago(months_)
        tight = [r for r in pool_ if r["month"] >= cut and near(r, 3.0)]
        if len(tight) >= 5:
            return tight, True
        return [r for r in pool_ if r["month"] >= cut
                and near(r, max(5.0, (sqm or 50) * 0.10))], False

    months = 12
    pool, exact_size = sized(recs, 12)
    if len(pool) < 3:
        pool, exact_size = sized(recs, 24)
        months = 24
    if len(pool) < 3:
        return {"count": len(pool), "street": street}
    if exact_size and sqm is not None:
        adjustments.append("same flat size")

    # 2) Same block when it still leaves enough evidence.
    scope = "street"
    if block:
        blk = [r for r in pool if r["block"].upper() == block]
        if len(blk) >= 3:
            pool, scope = blk, "block"

    # 3) Floor-level match (high/low/mid from the listing description).
    band = _floor_band(about)
    if band:
        rng = {"high": (7, 99), "mid": (4, 9), "low": (1, 6)}[band]
        lvl = [r for r in pool
               if (s := _storey_mid(r["storey_range"])) is not None
               and rng[0] <= s <= rng[1]]
        if len(lvl) >= 3:
            pool = lvl
            adjustments.append(f"{band}-floor comps")

    # 4) Trend-adjust each comp to the current month.
    drift = _street_drift(recs, lambda r: near(r, max(5.0, (sqm or 50) * 0.10)))
    now_idx = _month_idx(date.today().strftime("%Y-%m"))
    def adj_price(r):
        p = int(r["resale_price"])
        if drift is None:
            return p
        age = now_idx - _month_idx(r["month"])
        return int(p * (1 + drift) ** age)
    if drift is not None and abs(drift) >= 0.001:
        adjustments.append(f"trend-adjusted ({drift * 100:+.1f}%/mo)")

    adjusted = sorted(adj_price(r) for r in pool)

    # 5) Trim outliers (1.5×IQR) so one odd deal doesn't skew the median.
    if len(adjusted) >= 5:
        q1 = adjusted[len(adjusted) // 4]
        q3 = adjusted[(3 * len(adjusted)) // 4]
        iqr = q3 - q1
        kept = [p for p in adjusted if q1 - 1.5 * iqr <= p <= q3 + 1.5 * iqr]
        if len(kept) >= 3 and len(kept) < len(adjusted):
            adjustments.append(f"{len(adjusted) - len(kept)} outlier(s) trimmed")
            adjusted = kept

    median = int(statistics.median(adjusted))

    ask = None
    m = re.search(r"[\d,]+", str(asking_price or "").replace(" ", ""))
    if m:
        ask = int(m.group(0).replace(",", ""))

    # Street price trend: monthly medians of similar-size deals (last 18 mo).
    trend = {}
    for r in recs:
        if r["month"] >= _month_ago(18) and near(r, max(5.0, (sqm or 50) * 0.10)):
            trend.setdefault(r["month"], []).append(int(r["resale_price"]))
    series = [[m, int(statistics.median(v))] for m, v in sorted(trend.items())]

    latest = sorted(pool, key=lambda r: r["month"], reverse=True)[:3]
    return {
        "series": series,
        "drift_pct_mo": round(drift * 100, 2) if drift is not None else None,
        "count": len(adjusted),
        "scope": scope,                      # block | street
        "months": months,
        "street": street,
        "median": median,
        "low": adjusted[0],
        "high": adjusted[-1],
        "cov": (ask - median) if ask else None,
        "adjustments": adjustments,
        "latest": [{
            "month": r["month"],
            "block": r["block"],
            "storey": r["storey_range"],
            "sqm": float(r["floor_area_sqm"]),
            "price": int(r["resale_price"]),
        } for r in latest],
    }
