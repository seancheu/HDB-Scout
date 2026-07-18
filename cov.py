"""Potential COV (Cash Over Valuation) estimation.

Since 2014 HDB only values a flat AFTER the price is agreed, so buyers gauge
COV risk by comparing the asking price against recent official resale
transactions of similar flats nearby. This module pulls those transactions from
data.gov.sg (HDB resale registrations, updated monthly) and estimates:

    potential COV ≈ asking price − estimated valuation

The pipeline mirrors how professional valuers work (per HDB guidance and
industry write-ups — Stacked Homes, CheckHowMuch, PropertyGuru guides):

  comparables : same flat type/size, same block first then same street,
                last 12 months (widened to 24 if thin), outliers trimmed
  adjustments : • time     — street-level price drift (OLS, clipped)
                • storey   — local $/storey premium fitted from the street's
                             own transactions (clipped to 0–1.2%/storey)
                • lease    — Bala's leasehold-relativity curve
                             value ∝ 1 − 1.035^(−years_remaining)
                • recency  — 6-month half-life weights (valuers lean on the
                             trailing ~6 months of deals)
  renovation  : ignored on purpose — valuations are comp-driven, so a
                renovated unit's premium tends to surface as COV, not value.

COV is payable in CASH only.
"""

import json
import math
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


def _subject_storey(about):
    """Best guess of the listing's storey: an exact '#12-345' beats the
    high/mid/low wording (HDB bands are 3 floors, so mid-points suffice)."""
    m = re.search(r"#\s?(\d{1,2})\s?-", about or "")
    if m and 1 <= int(m.group(1)) <= 50:
        return float(m.group(1)), "exact"
    band = _floor_band(about)
    if band:
        return {"high": 11.0, "mid": 6.0, "low": 3.0}[band], band
    return None, None


def _remaining_lease_years(rec):
    """Comp's remaining lease in years, from '61 years 04 months' or the
    lease-commencement year (99-year leases)."""
    s = str(rec.get("remaining_lease") or "")
    m = re.match(r"(\d+)\s*years?(?:\s*(\d+)\s*months?)?", s)
    if m:
        return int(m.group(1)) + int(m.group(2) or 0) / 12
    try:
        start = int(rec.get("lease_commence_date"))
        if 1960 <= start <= date.today().year:
            return 99 - (date.today().year - start)
    except (TypeError, ValueError):
        pass
    return None


def _bala(t):
    """Bala's leasehold-relativity curve: value fraction of freehold at
    t years remaining. Slope ≈0.2%/yr in the 80s, ≈0.4%/yr in the 60s."""
    t = max(5.0, min(99.0, t))
    return 1 - (1 / 1.035) ** t


def _flat_type_for(sqm):
    """Rough sqm → HDB flat type (used as a soft preference, never a hard
    filter — official bands overlap across build eras)."""
    if sqm is None:
        return None
    if sqm < 40:
        return "1 ROOM"
    if sqm < 53:
        return "2 ROOM"
    if sqm < 80:
        return "3 ROOM"
    if sqm < 108:
        return "4 ROOM"
    if sqm < 127:
        return "5 ROOM"
    return "EXECUTIVE"


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


def _storey_premium(recs, sqm_ok, drift, now_idx):
    """Fractional price premium per storey, fitted from this street's own
    transactions (time-adjusted log-price vs storey mid-point). Industry
    range is ~$3k–7k per floor; clip to 0–1.2%/storey. None if too thin."""
    pts = []
    for r in recs:
        if r["month"] < _month_ago(24) or not sqm_ok(r):
            continue
        s = _storey_mid(r["storey_range"])
        if s is None:
            continue
        p = int(r["resale_price"])
        if drift is not None:
            p = p * (1 + drift) ** (now_idx - _month_idx(r["month"]))
        pts.append((s, math.log(p)))
    if len(pts) < 10:
        return None
    n = len(pts)
    mx = sum(p[0] for p in pts) / n
    my = sum(p[1] for p in pts) / n
    denom = sum((x - mx) ** 2 for x, _ in pts)
    if not denom:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / denom   # d(ln price)/storey
    return max(0.0, min(0.012, slope))


def _weighted_median(pairs):
    """Median of (value, weight) pairs — the 50% point of cumulative weight."""
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2:
            return v
    return pairs[-1][0]


def _weighted_quantile(pairs, q):
    pairs = sorted(pairs)
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot * q:
            return v
    return pairs[-1][0]


def estimate(block_street, size_sqft, asking_price, about=None, top_year=None):
    """Return a COV-estimate dict, or None when no data is available.

    Valuer-style pipeline: tight size band → same-block preference → per-comp
    adjustments (time drift, storey premium, Bala lease curve) → recency-
    weighted, outlier-trimmed median.
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

    def loose(r):
        return near(r, max(5.0, (sqm or 50) * 0.10))

    def sized(pool_, months_):
        cut = _month_ago(months_)
        tight = [r for r in pool_ if r["month"] >= cut and near(r, 3.0)]
        if len(tight) >= 5:
            return tight, True
        return [r for r in pool_ if r["month"] >= cut and loose(r)], False

    months = 12
    pool, exact_size = sized(recs, 12)
    if len(pool) < 3:
        pool, exact_size = sized(recs, 24)
        months = 24
    if len(pool) < 3:
        return {"count": len(pool), "street": street}
    if exact_size and sqm is not None:
        adjustments.append("same flat size")

    # 1b) Same flat type as a soft preference (bands overlap across eras).
    ftype = _flat_type_for(sqm)
    if ftype:
        typed = [r for r in pool if r.get("flat_type") == ftype]
        if len(typed) >= 5 and len(typed) < len(pool):
            pool = typed
            adjustments.append(f"{ftype.title().replace('Room', 'room')} comps")

    # 2) Same block when it still leaves enough evidence.
    scope = "street"
    if block:
        blk = [r for r in pool if r["block"].upper() == block]
        if len(blk) >= 3:
            pool, scope = blk, "block"

    # 3) Time drift + local storey premium, both fitted street-wide.
    drift = _street_drift(recs, loose)
    now_idx = _month_idx(date.today().strftime("%Y-%m"))
    storey_slope = _storey_premium(recs, loose, drift, now_idx)
    subj_storey, storey_src = _subject_storey(about)

    # Subject's remaining lease (99-year HDB lease from the TOP year).
    subj_lease = None
    try:
        ty = int(top_year)
        if 1960 <= ty <= date.today().year:
            subj_lease = 99 - (date.today().year - ty)
    except (TypeError, ValueError):
        pass

    used_storey = used_lease = False

    def adj_price(r):
        nonlocal used_storey, used_lease
        p = float(int(r["resale_price"]))
        # (a) bring the deal to today's market
        if drift is not None:
            p *= (1 + drift) ** (now_idx - _month_idx(r["month"]))
        # (b) storey: move the comp to the subject's floor
        if storey_slope and subj_storey is not None:
            cs = _storey_mid(r["storey_range"])
            if cs is not None:
                delta = max(-8.0, min(8.0, subj_storey - cs))
                if abs(delta) >= 1:
                    p *= (1 + storey_slope) ** delta
                    used_storey = True
        # (c) lease: Bala's curve ratio, capped ±12%
        if subj_lease is not None:
            cl = _remaining_lease_years(r)
            if cl is not None and abs(subj_lease - cl) >= 2:
                ratio = _bala(subj_lease) / _bala(cl)
                p *= max(0.88, min(1.12, ratio))
                used_lease = True
        return int(p)

    if drift is not None and abs(drift) >= 0.001:
        adjustments.append(f"trend-adjusted ({drift * 100:+.1f}%/mo)")

    # Recency-weighted comps: 6-month half-life, like a valuer leaning on
    # the trailing half-year of deals.
    weighted = [(adj_price(r), 0.5 ** ((now_idx - _month_idx(r["month"])) / 6.0))
                for r in pool]
    if used_storey:
        adjustments.append(
            f"storey-adjusted ({storey_slope * 100:.1f}%/floor to "
            f"{'#%d' % subj_storey if storey_src == 'exact' else storey_src + ' floor'})")
    if used_lease:
        adjustments.append(f"lease-adjusted (Bala curve, {subj_lease:.0f} yrs left)")

    # 4) Trim outliers (1.5×IQR) so one odd deal doesn't skew the median.
    prices = sorted(p for p, _ in weighted)
    if len(prices) >= 5:
        q1 = prices[len(prices) // 4]
        q3 = prices[(3 * len(prices)) // 4]
        iqr = q3 - q1
        kept = [(p, w) for p, w in weighted if q1 - 1.5 * iqr <= p <= q3 + 1.5 * iqr]
        if len(kept) >= 3 and len(kept) < len(weighted):
            adjustments.append(f"{len(weighted) - len(kept)} outlier(s) trimmed")
            weighted = kept

    adjustments.append("recency-weighted (6-mo half-life)")
    median = int(_weighted_median(weighted))
    band_low = int(_weighted_quantile(weighted, 0.25))
    band_high = int(_weighted_quantile(weighted, 0.75))
    lo = min(p for p, _ in weighted)
    hi = max(p for p, _ in weighted)

    ask = None
    m = re.search(r"[\d,]+", str(asking_price or "").replace(" ", ""))
    if m:
        ask = int(m.group(0).replace(",", ""))

    # Street price trend: monthly medians of similar-size deals (last 18 mo).
    trend = {}
    for r in recs:
        if r["month"] >= _month_ago(18) and loose(r):
            trend.setdefault(r["month"], []).append(int(r["resale_price"]))
    series = [[mth, int(statistics.median(v))] for mth, v in sorted(trend.items())]

    latest = sorted(pool, key=lambda r: r["month"], reverse=True)[:3]
    return {
        "series": series,
        "drift_pct_mo": round(drift * 100, 2) if drift is not None else None,
        "count": len(weighted),
        "scope": scope,                      # block | street
        "months": months,
        "street": street,
        "median": median,
        "low": lo,
        "high": hi,
        "band_low": band_low,                # likely-valuation band (p25–p75)
        "band_high": band_high,
        "cov": (ask - median) if ask else None,
        "cov_low": (ask - band_high) if ask else None,
        "cov_high": (ask - band_low) if ask else None,
        "subj_storey": subj_storey,
        "subj_lease": subj_lease,
        "adjustments": adjustments,
        "latest": [{
            "month": r["month"],
            "block": r["block"],
            "storey": r["storey_range"],
            "sqm": float(r["floor_area_sqm"]),
            "price": int(r["resale_price"]),
        } for r in latest],
    }
