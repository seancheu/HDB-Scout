"""Mini web UI for the PropertyGuru listing extractor.

Layout: a URL input bar across the top, a results panel below. Each successful
extraction is also appended to results.csv.

Run:  python app.py   ->  http://127.0.0.1:5000
"""

import csv
import hashlib
import json
import os
import re
import threading
from collections import Counter
from datetime import datetime
from urllib.parse import parse_qs, urlparse

# The Flask server is threaded; serialise CSV writes so concurrent requests
# (e.g. a note edit while a batch import runs) can't interleave/clobber.
_CSV_LOCK = threading.Lock()

from flask import Flask, Response, jsonify, render_template, request

from extractor import (DETAIL_KEYS, FIELDS, BlockedError, extract, extract_many,
                       extract_search_all, is_search_url, parse, skim_flags)

MAX_IMPORT_PAGES = 20  # protects against a broad, unfiltered search
from mapview import (_haversine_m, build_compare_map_html, build_map_html,
                     build_overview_map_html, geocode_address,
                     nearest_schools, nearest_stations, route_minutes)

SUPPORTED_SITES = ("propertyguru", "srx.com.sg")


def _supported(url):
    return any(s in (url or "") for s in SUPPORTED_SITES)


def _enrich(result):
    """Fill coordinates + nearby stations for sites that don't embed them
    (SRX). Geocodes via OneMap (postal code when available), then finds the
    nearest MRT/LRT from the station dataset."""
    if not (result.get("block_lat") and result.get("block_lon")):
        query = result.get("postal_code") or (
            f"{result['block_street']} Singapore" if result.get("block_street") else None
        )
        if query:
            lat, lon = geocode_address(query)
            result["block_lat"], result["block_lon"] = lat, lon
    elif result.get("block_street"):
        # Sanity-check the listing's embedded pin against OneMap — agents
        # sometimes mis-pin the block. If they disagree by >2 km, trust OneMap
        # and recompute everything derived from the point.
        glat, glon = geocode_address(f"{result['block_street']} Singapore")
        if glat and _haversine_m(result["block_lat"], result["block_lon"],
                                 glat, glon) > 2000:
            result["block_lat"], result["block_lon"] = glat, glon
            result["stations"] = []
            result["schools"] = []
            result["nearest_mrt"] = None
            result["mrt_distance"] = None
    if result.get("block_lat") and not result.get("stations"):
        stations = nearest_stations(result["block_lat"], result["block_lon"])
        result["stations"] = stations
        if stations and not result.get("mrt_distance"):
            s0 = next((s for s in stations if s["type"] == "MRT"), stations[0])
            result["nearest_mrt"] = s0["name"]
            result["mrt_distance"] = f"~{s0['distance_m']} m · ~{s0['walk_mins']} min walk"
    if result.get("block_lat") and not result.get("schools"):
        result["schools"] = nearest_schools(result["block_lat"], result["block_lon"])
    return result

app = Flask(__name__)
# Pick up template edits on refresh without restarting the server.
app.config["TEMPLATES_AUTO_RELOAD"] = True

CSV_PATH = os.path.join(os.path.dirname(__file__), "results.csv")
# Map columns are persisted so any saved row can be re-plotted later.
MAP_COLUMNS = ["block_lat", "block_lon", "stations_json", "schools_json"]
# Basic details (available from a search card) + a "deep" flag marking whether
# the full listing page has been fetched.
DETAIL_COLUMNS = DETAIL_KEYS + ["images_json", "agent_json", "deep"]
# User-editable tracking columns (notes / shortlist / status / price history).
USER_COLUMNS = ["note", "starred", "status", "price_history_json"]
CSV_COLUMNS = (["timestamp", "url"] + FIELDS + MAP_COLUMNS + DETAIL_COLUMNS
               + USER_COLUMNS)


def _write_all(rows):
    """Rewrite the whole CSV with the current schema (atomically, locked)."""
    with _CSV_LOCK:
        tmp = CSV_PATH + ".tmp"
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(rows)
        os.replace(tmp, CSV_PATH)


def _ensure_schema():
    """Migrate an existing results.csv to the current column set if needed."""
    if not os.path.exists(CSV_PATH):
        return
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f), [])
    if header == CSV_COLUMNS:
        return
    rows = _csv_rows()  # reads with old header
    # Existing rows predate the light/deep split — they were fully extracted,
    # so mark them deep unless clearly a light import.
    if "deep" not in header:
        for r in rows:
            if r.get("top_year") or r.get("about") or r.get("block_lat"):
                r["deep"] = "1"
    _write_all(rows)


def _csv_rows():
    """Raw rows in file order (no reversing, no _id)."""
    if not os.path.exists(CSV_PATH):
        return []
    with open(CSV_PATH, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _price_num(s):
    """Parse a price string like 'S$ 725,000' to an int, or None."""
    m = re.search(r"[\d,]+", (s or "").replace(" ", ""))
    return int(m.group(0).replace(",", "")) if m else None


def _row_from_result(url, result, now, deep):
    """Build a full CSV row dict from an extraction result."""
    row = {"timestamp": now, "url": url}
    row.update({k: (result.get(k) or "") for k in FIELDS})
    row["block_lat"] = result.get("block_lat") or ""
    row["block_lon"] = result.get("block_lon") or ""
    row["stations_json"] = json.dumps(result.get("stations") or [])
    row["schools_json"] = json.dumps(result.get("schools") or [])
    for k in DETAIL_KEYS:
        row[k] = result.get(k) or ""
    row["images_json"] = json.dumps(result.get("images") or [])
    row["agent_json"] = json.dumps(result.get("agent") or {})
    row["deep"] = "1" if deep else ""
    row["note"] = ""
    row["starred"] = ""
    row["status"] = ""
    pn = _price_num(result.get("price"))
    row["price_history_json"] = json.dumps([{"t": now, "p": pn}] if pn else [])
    return row


def append_csv(url, result, deep=True):
    """Append one extraction to results.csv; returns the new row's file index."""
    now = datetime.now().isoformat(timespec="seconds")
    with _CSV_LOCK:
        new_file = not os.path.exists(CSV_PATH)
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS, extrasaction="ignore")
            if new_file:
                writer.writeheader()
            writer.writerow(_row_from_result(url, result, now, deep))
    return len(_csv_rows()) - 1


def _listing_id(url):
    """The numeric listing id at the end of a PropertyGuru URL — the identity
    of a listing. Falls back to the whole URL if no id is found."""
    nums = re.findall(r"(\d{6,})", url or "")
    return nums[-1] if nums else (url or "").strip().lower()


def _content_key(row):
    """Secondary duplicate signal: same block + price + size (a re-list of the
    same unit under a different URL)."""
    return (
        (row.get("block_street") or "").strip().lower(),
        (row.get("price") or "").strip(),
        (row.get("size_sqft") or "").strip(),
    )


def _annotate_dups(rows):
    """Tag each row with _dup / _dup_reason when another saved row matches it."""
    id_counts = Counter(_listing_id(r.get("listing_url") or r.get("url")) for r in rows)
    content_counts = Counter(_content_key(r) for r in rows)
    for r in rows:
        same_id = id_counts[_listing_id(r.get("listing_url") or r.get("url"))] > 1
        same_content = (
            any(_content_key(r)) and content_counts[_content_key(r)] > 1
        )
        r["_dup"] = same_id or same_content
        r["_dup_reason"] = (
            "Same listing saved more than once" if same_id
            else "Same block, price & size as another saved entry" if same_content
            else ""
        )


def read_rows():
    """Return all saved rows (newest first), each tagged with its file index."""
    rows = _csv_rows()
    for i, r in enumerate(rows):
        r["_id"] = i
        # Whether this row has enough data to draw a map.
        r["_mappable"] = bool(
            (r.get("block_lat") and r.get("block_lon")) or r.get("stations_json", "[]") != "[]"
        )
        # Keyword flags skimmed from the description (computed live, so they
        # also appear on rows saved before this feature existed).
        r["_flags"] = skim_flags(r.get("about"))
        # Which site the row came from.
        src = (r.get("listing_url") or r.get("url") or "").lower()
        r["_source"] = "SRX" if "srx.com" in src else "PG"
        # Whether the full listing page has been fetched (vs a light import).
        r["_deep"] = r.get("deep") == "1"
        # Tracking fields.
        r["_starred"] = r.get("starred") == "1"
        r["_status"] = r.get("status") or ""
        r["_note"] = r.get("note") or ""
        # Price change since first saved (from price history).
        try:
            hist = json.loads(r.get("price_history_json") or "[]")
        except Exception:
            hist = []
        r["_price_change"] = None
        prices = [h.get("p") for h in hist if h.get("p")]
        if len(prices) >= 2 and prices[-1] != prices[0]:
            delta = prices[-1] - prices[0]
            r["_price_change"] = {
                "delta": delta,
                "pct": round(delta / prices[0] * 100, 1),
                "from": prices[0],
                "checks": len(prices),
            }
    _annotate_dups(rows)
    return list(reversed(rows))


def delete_row(row_id):
    """Delete the row at file index `row_id`. Returns True if removed."""
    rows = _csv_rows()
    if row_id < 0 or row_id >= len(rows):
        return False
    del rows[row_id]
    _write_all(rows)
    return True


def update_row(row_id, fields):
    """Patch note/starred/status on a row. Returns True if updated."""
    rows = _csv_rows()
    if row_id < 0 or row_id >= len(rows):
        return False
    for k in ("note", "starred", "status"):
        if k in fields:
            rows[row_id][k] = fields[k]
    _write_all(rows)
    return True


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/extract", methods=["POST"])
def do_extract():
    url = (request.json or {}).get("url", "").strip()
    if not url or not _supported(url):
        return jsonify({"error": "Please enter a valid PropertyGuru or SRX listing URL."}), 400
    try:
        result = _enrich(extract(url))
    except BlockedError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001 — surface any fetch/parse failure to the UI
        return jsonify({"error": f"Could not extract this listing: {e}"}), 500

    if not any(result.get(k) for k in FIELDS):
        return jsonify({"error": "Loaded the page but found no details."}), 422

    # Duplicate check against what is already saved (before appending).
    existing = _csv_rows()
    lid = _listing_id(result.get("listing_url") or url)
    duplicate = any(
        _listing_id(r.get("listing_url") or r.get("url")) == lid
        or (any(_content_key(r)) and _content_key(r) == _content_key(result))
        for r in existing
    )
    row_id = append_csv(url, result)
    return jsonify({"result": result, "id": row_id, "duplicate": duplicate})


def _import_search(url, max_pages):
    """Walk a search and append new listings (skipping ones already saved by
    listing-id). Returns a summary dict."""
    r = extract_search_all(url, max_pages=max_pages)
    seen = {_listing_id(row.get("listing_url") or row.get("url")) for row in _csv_rows()}
    imported = skipped = 0
    for res in r["listings"]:
        lid = _listing_id(res.get("listing_url"))
        if lid in seen:
            skipped += 1
            continue
        seen.add(lid)
        append_csv(res.get("listing_url") or url, res, deep=False)
        imported += 1
    return {
        "imported": imported, "skipped": skipped, "found": len(r["listings"]),
        "total_pages": r["total_pages"], "fetched_pages": r["fetched_pages"],
        "capped": r["capped"],
    }


def _dedup_rows():
    """Collapse rows that share a listing id, keeping the richest (deep) copy.
    Returns how many rows were removed."""
    rows = _csv_rows()
    idx = {}          # listing-id -> position in `keep`
    keep = []
    removed = 0
    for row in rows:
        lid = _listing_id(row.get("listing_url") or row.get("url"))
        if lid in idx:
            removed += 1
            kept = keep[idx[lid]]
            # Prefer the deep copy; otherwise keep the existing one.
            if row.get("deep") == "1" and kept.get("deep") != "1":
                keep[idx[lid]] = row
            continue
        idx[lid] = len(keep)
        keep.append(row)
    if removed:
        _write_all(keep)
    return removed


@app.route("/import", methods=["POST"])
def do_import():
    """Bulk-import every listing on a PropertyGuru search-results page as a
    light row (one fetch, no per-listing pop-ups)."""
    payload = request.json or {}
    url = (payload.get("url") or "").strip()
    max_pages = int(payload.get("max_pages") or MAX_IMPORT_PAGES)
    if not is_search_url(url):
        return jsonify({"error": "That doesn't look like a PropertyGuru search URL."}), 400
    try:
        return jsonify(_import_search(url, max_pages))
    except BlockedError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not read that search page: {e}"}), 500


# --------------------------------------------------------------------------- #
# Saved searches
# --------------------------------------------------------------------------- #

SEARCHES_PATH = os.path.join(os.path.dirname(__file__), "saved_searches.json")


def _load_searches():
    if not os.path.exists(SEARCHES_PATH):
        return []
    try:
        with open(SEARCHES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save_searches(items):
    with open(SEARCHES_PATH, "w", encoding="utf-8") as f:
        json.dump(items, f, indent=1)


def _search_name(url):
    """Friendly label from the key filters, e.g. 'Pasir Ris · ≤900k · ≥1200sqft'."""
    q = parse_qs(urlparse(url).query)
    parts = []
    town = q.get("_freetextDisplay", [None])[0]
    if town:
        parts.append(town.replace("+", " "))
    if q.get("propertyTypeGroup", [None])[0] == "H":
        parts.append("HDB")
    if q.get("maxPrice"):
        try:
            parts.append(f"≤{int(q['maxPrice'][0]) // 1000}k")
        except ValueError:
            pass
    if q.get("minSize"):
        parts.append(f"≥{q['minSize'][0]}sqft")
    return " · ".join(parts) or "Saved search"


@app.route("/searches")
def searches():
    return jsonify({"searches": _load_searches()})


@app.route("/searches/add", methods=["POST"])
def searches_add():
    url = ((request.json or {}).get("url") or "").strip()
    if not is_search_url(url):
        return jsonify({"error": "That doesn't look like a PropertyGuru search URL."}), 400
    items = _load_searches()
    sid = hashlib.md5(url.encode()).hexdigest()[:8]
    if not any(s["id"] == sid for s in items):
        items.append({"id": sid, "name": _search_name(url), "url": url})
        _save_searches(items)
    return jsonify({"id": sid, "searches": items})


@app.route("/searches/delete", methods=["POST"])
def searches_delete():
    sid = (request.json or {}).get("id")
    items = [s for s in _load_searches() if s["id"] != sid]
    _save_searches(items)
    return jsonify({"searches": items})


@app.route("/searches/refresh", methods=["POST"])
def searches_refresh():
    """Re-pull a saved search: add new listings, then remove any duplicates."""
    sid = (request.json or {}).get("id")
    item = next((s for s in _load_searches() if s["id"] == sid), None)
    if not item:
        return jsonify({"error": "Saved search not found."}), 404
    try:
        summary = _import_search(item["url"], MAX_IMPORT_PAGES)
    except BlockedError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not refresh: {e}"}), 500
    summary["removed"] = _dedup_rows()
    return jsonify(summary)


def _apply_deep(row, result):
    """Fill a row in place from a full extraction result and mark it deep."""
    for k in FIELDS + DETAIL_KEYS:
        if result.get(k):
            row[k] = result[k]
    if result.get("images"):
        row["images_json"] = json.dumps(result["images"])
    if result.get("agent"):
        row["agent_json"] = json.dumps(result["agent"])
    if result.get("block_lat"):
        row["block_lat"] = result["block_lat"]
        row["block_lon"] = result["block_lon"]
        row["stations_json"] = json.dumps(result.get("stations") or [])
        row["schools_json"] = json.dumps(result.get("schools") or [])
    try:
        hist = json.loads(row.get("price_history_json") or "[]")
    except Exception:
        hist = []
    if not hist:
        pn = _price_num(result.get("price") or row.get("price"))
        if pn:
            hist = [{"t": row.get("timestamp"), "p": pn}]
    row["price_history_json"] = json.dumps(hist)
    row["deep"] = "1"


@app.route("/deepen", methods=["POST"])
def deepen():
    """Fetch the full listing page for a light row and fill in the rest
    (TOP/lease, description, coordinates, stations, schools)."""
    row_id = (request.json or {}).get("id")
    rows = _csv_rows()
    if not isinstance(row_id, int) or not (0 <= row_id < len(rows)):
        return jsonify({"error": "Row not found."}), 404
    row = rows[row_id]
    if row.get("deep") == "1":
        return jsonify({"ok": True, "already": True})
    target = row.get("listing_url") or row.get("url")
    try:
        result = _enrich(extract(target))
    except BlockedError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not load the listing: {e}"}), 500

    _apply_deep(row, result)
    _write_all(rows)
    return jsonify({"ok": True})


@app.route("/deepen-all", methods=["POST"])
def deepen_all():
    """Deepen up to `limit` light rows in ONE browser session; returns how many
    were done and how many remain, so the UI can loop with progress."""
    limit = int((request.json or {}).get("limit", 10))
    rows = _csv_rows()
    light = [r for r in rows
             if r.get("deep") != "1" and (r.get("listing_url") or r.get("url"))]
    if not light:
        return jsonify({"deepened": 0, "remaining": 0})
    batch = light[:limit]
    urls = [r.get("listing_url") or r.get("url") for r in batch]
    items = extract_many(urls)
    by_url = {it["url"]: it for it in items}
    deepened = 0
    for r in batch:
        target = r.get("listing_url") or r.get("url")
        it = by_url.get(target)
        if it and it.get("ok") and any(it["result"].get(k) for k in FIELDS):
            _apply_deep(r, _enrich(it["result"]))
            deepened += 1
        else:
            r["deep"] = "1"  # give up on this one so the loop can finish
    _write_all(rows)
    remaining = sum(1 for r in _csv_rows() if r.get("deep") != "1"
                    and (r.get("listing_url") or r.get("url")))
    return jsonify({"deepened": deepened, "remaining": remaining})


@app.route("/extract-batch", methods=["POST"])
def do_extract_batch():
    payload = request.json or {}
    urls = payload.get("urls") or []
    # Accept either a list or a newline/space separated string.
    if isinstance(urls, str):
        urls = urls.split()
    urls = [u.strip() for u in urls if u and _supported(u)]
    if not urls:
        return jsonify({"error": "No valid PropertyGuru or SRX URLs found."}), 400

    items = extract_many(urls)
    saved = 0
    seen_ids = {_listing_id(r.get("listing_url") or r.get("url")) for r in _csv_rows()}
    for item in items:
        if item.get("ok") and any(item["result"].get(k) for k in FIELDS):
            _enrich(item["result"])
            lid = _listing_id(item["result"].get("listing_url") or item["url"])
            item["duplicate"] = lid in seen_ids
            seen_ids.add(lid)
            append_csv(item["url"], item["result"])
            saved += 1
        else:
            item["ok"] = False
            item.setdefault("error", "Loaded the page but found no details.")
    return jsonify({"items": items, "saved": saved, "total": len(items)})


@app.route("/results")
def results():
    return jsonify({"columns": CSV_COLUMNS, "rows": read_rows()})


@app.route("/update", methods=["POST"])
def update():
    """Set note / starred / status on a saved row."""
    payload = request.json or {}
    row_id = payload.get("id")
    if not isinstance(row_id, int):
        return jsonify({"error": "Bad id."}), 400
    fields = {}
    if "note" in payload:
        fields["note"] = str(payload["note"])
    if "starred" in payload:
        fields["starred"] = "1" if payload["starred"] else ""
    if "status" in payload:
        fields["status"] = str(payload["status"])
    if not update_row(row_id, fields):
        return jsonify({"error": "Row not found."}), 404
    return jsonify({"ok": True})


@app.route("/recheck", methods=["POST"])
def recheck():
    """Re-extract a saved row's URL and record any price change."""
    row_id = (request.json or {}).get("id")
    rows = _csv_rows()
    if not isinstance(row_id, int) or not (0 <= row_id < len(rows)):
        return jsonify({"error": "Row not found."}), 404
    row = rows[row_id]
    url = row.get("url")
    try:
        result = _enrich(extract(url))
    except BlockedError as e:
        return jsonify({"error": str(e)}), 502
    except Exception as e:  # noqa: BLE001
        return jsonify({"error": f"Could not re-check: {e}"}), 500

    new_price = result.get("price") or row.get("price")
    old_num = _price_num(row.get("price"))
    new_num = _price_num(new_price)
    try:
        hist = json.loads(row.get("price_history_json") or "[]")
    except Exception:
        hist = []
    now = datetime.now().isoformat(timespec="seconds")
    changed = new_num is not None and old_num is not None and new_num != old_num
    # Seed a baseline for rows saved before price history existed.
    if not hist:
        base = old_num if old_num is not None else new_num
        if base is not None:
            hist.append({"t": row.get("timestamp") or now, "p": base})
    if changed:
        hist.append({"t": now, "p": new_num})

    # Refresh the row's current values (price, size, about, map data).
    row["timestamp"] = now
    for k in FIELDS:
        if result.get(k):
            row[k] = result[k]
    if result.get("block_lat"):
        row["block_lat"] = result["block_lat"]
        row["block_lon"] = result["block_lon"]
        row["stations_json"] = json.dumps(result.get("stations") or [])
        row["schools_json"] = json.dumps(result.get("schools") or [])
    row["price_history_json"] = json.dumps(hist)
    _write_all(rows)

    return jsonify({
        "ok": True,
        "changed": changed,
        "old_price": old_num,
        "new_price": new_num,
    })


def _row_map_data(row, row_id=None):
    """Coerce a CSV row into the shape the map builder expects."""
    def _load(key):
        try:
            return json.loads(row.get(key) or "[]")
        except Exception:
            return []
    return {
        "id": row_id,
        "url": row.get("listing_url") or row.get("url"),
        "block_street": row.get("block_street"),
        "block_lat": float(row["block_lat"]) if row.get("block_lat") else None,
        "block_lon": float(row["block_lon"]) if row.get("block_lon") else None,
        "stations": _load("stations_json"),
        "schools": _load("schools_json"),
    }


@app.route("/map/<int:row_id>")
def map_view(row_id):
    """Return a standalone Folium map (block + nearby MRT/LRT) for one saved row."""
    theme = request.args.get("theme", "light")
    rows = _csv_rows()
    if row_id < 0 or row_id >= len(rows):
        return Response("<p style='font-family:sans-serif'>Map not found.</p>",
                        mimetype="text/html", status=404)
    d = _row_map_data(rows[row_id], row_id)
    html = build_map_html(d["block_street"], d["block_lat"], d["block_lon"],
                          d["stations"], d["schools"], theme=theme,
                          url=d["url"])
    return Response(html, mimetype="text/html")


@app.route("/map-overview")
def map_overview():
    """One pin per saved row (block only, no POIs) — an area overview."""
    theme = request.args.get("theme", "light")
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()]
    rows = _csv_rows()
    items = []
    for i in ids:
        if 0 <= i < len(rows) and rows[i].get("block_lat"):
            r = rows[i]
            items.append({
                "id": i,
                "url": r.get("listing_url") or r.get("url"),
                "block_street": r.get("block_street"),
                "price": r.get("price"),
                "town": r.get("town"),
                "block_lat": float(r["block_lat"]),
                "block_lon": float(r["block_lon"]),
            })
    if not items:
        return Response("<p style='font-family:sans-serif'>No mappable listings.</p>",
                        mimetype="text/html", status=404)
    return Response(build_overview_map_html(items, theme=theme), mimetype="text/html")


@app.route("/map-compare")
def map_compare():
    """Return one Folium map plotting several saved rows, colour-coded."""
    theme = request.args.get("theme", "light")
    ids = [int(x) for x in request.args.get("ids", "").split(",") if x.strip().isdigit()]
    rows = _csv_rows()
    items = [_row_map_data(rows[i], i) for i in ids if 0 <= i < len(rows)]
    if not items:
        return Response("<p style='font-family:sans-serif'>Nothing to compare.</p>",
                        mimetype="text/html", status=404)
    return Response(build_compare_map_html(items, theme=theme), mimetype="text/html")


@app.route("/commute", methods=["POST"])
def commute():
    """Door-to-door transit + drive time from each listing to one destination."""
    payload = request.json or {}
    dest = (payload.get("dest") or "").strip()
    ids = payload.get("ids") or []
    if not dest:
        return jsonify({"error": "Enter a destination address."}), 400
    dlat, dlon = geocode_address(dest)
    if not (dlat and dlon):
        return jsonify({"error": f"Could not find '{dest}'."}), 422
    rows = _csv_rows()
    # Route all listings in parallel (2 OneMap calls each is slow serially).
    from concurrent.futures import ThreadPoolExecutor

    def _route(i):
        la, lo = float(rows[i]["block_lat"]), float(rows[i]["block_lon"])
        key = rows[i].get("listing_url") or rows[i].get("url")
        return key, {
            "transit": route_minutes(la, lo, dlat, dlon, "pt"),
            "drive": route_minutes(la, lo, dlat, dlon, "drive"),
        }

    valid = [i for i in ids
             if isinstance(i, int) and 0 <= i < len(rows) and rows[i].get("block_lat")]
    times = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        for key, t in ex.map(_route, valid):
            times[key] = t
    return jsonify({"dest": dest, "times": times})


@app.route("/cov/<int:row_id>")
def cov_estimate(row_id):
    """Potential COV estimate vs recent transactions near this listing."""
    import cov as cov_mod
    rows = _csv_rows()
    if not (0 <= row_id < len(rows)):
        return jsonify({"error": "Row not found."}), 404
    r = rows[row_id]
    est = cov_mod.estimate(r.get("block_street"), r.get("size_sqft"),
                           r.get("price"), about=r.get("about"))
    if est is None:
        return jsonify({"error": "Transaction data unavailable right now."}), 502
    return jsonify(est)


@app.route("/block/<int:row_id>")
def block_profile(row_id):
    """Official HDB block data for this listing's block."""
    import hdbinfo
    rows = _csv_rows()
    if not (0 <= row_id < len(rows)):
        return jsonify({"error": "Row not found."}), 404
    info = hdbinfo.block_info(rows[row_id].get("block_street"))
    if not info:
        return jsonify({"error": "No official block record found."}), 404
    return jsonify(info)


@app.route("/p1/<int:row_id>")
def p1_check(row_id):
    """P1 registration outlook for the nearest primary schools to this block."""
    import p1data
    rows = _csv_rows()
    if not (0 <= row_id < len(rows)):
        return jsonify({"error": "Row not found."}), 404
    try:
        schools = json.loads(rows[row_id].get("schools_json") or "[]")
    except Exception:
        schools = []
    if not schools:
        return jsonify({"schools": [], "note": "No nearby-school data for this listing."})
    if p1data.is_stale():
        try:
            p1data.refresh()
        except Exception:
            pass
    row = rows[row_id]
    blat = float(row["block_lat"]) if row.get("block_lat") else None
    blon = float(row["block_lon"]) if row.get("block_lon") else None
    out = []
    for s in schools[:4]:                     # nearest few
        # MOE's 1 km / 2 km bands are measured HOME-TO-SCHOOL (radial), not by
        # walking route — so judge the band on straight-line distance.
        radial = None
        if blat and s.get("lat"):
            radial = int(_haversine_m(blat, blon, s["lat"], s["lon"]))
        judge = radial if radial is not None else s.get("distance_m")
        rec = p1data.summarise(p1data.lookup(s.get("name")))
        if not rec:
            out.append({"name": s.get("name"), "distance_m": s.get("distance_m"),
                        "radial_m": radial, "unmatched": True})
            continue
        rec["distance_m"] = s.get("distance_m")
        rec["radial_m"] = radial
        rec["within_1km"] = (judge or 9999) <= 1000
        rec["within_2km"] = (judge or 9999) <= 2000
        out.append(rec)
    return jsonify({"schools": out})


@app.route("/recheck-batch", methods=["POST"])
def recheck_batch():
    """Re-check prices for all shortlisted (starred) rows in ONE browser
    session. Updates price history and current fields; reports changes."""
    rows = _csv_rows()
    targets = [(i, r) for i, r in enumerate(rows)
               if r.get("starred") == "1" and (r.get("listing_url") or r.get("url"))]
    if not targets:
        return jsonify({"error": "Star (★) some listings first — this re-checks your shortlist."}), 400
    targets = targets[:12]      # keep one run bounded
    urls = [r.get("listing_url") or r.get("url") for _, r in targets]
    try:
        items = extract_many(urls)
    except BlockedError as e:
        return jsonify({"error": str(e)}), 502
    by_url = {it["url"]: it for it in items}
    now = datetime.now().isoformat(timespec="seconds")
    changed, checked = [], 0
    for i, r in targets:
        it = by_url.get(r.get("listing_url") or r.get("url"))
        if not (it and it.get("ok")):
            continue
        checked += 1
        res = it["result"]
        old = _price_num(r.get("price"))
        new = _price_num(res.get("price"))
        try:
            hist = json.loads(r.get("price_history_json") or "[]")
        except Exception:
            hist = []
        if not hist and old:
            hist = [{"t": r.get("timestamp") or now, "p": old}]
        if new and old and new != old:
            hist.append({"t": now, "p": new})
            changed.append({"street": r.get("block_street"), "old": old, "new": new})
        for k in FIELDS:
            if res.get(k):
                r[k] = res[k]
        r["price_history_json"] = json.dumps(hist)
        r["timestamp"] = now
    _write_all(rows)
    return jsonify({"checked": checked, "changed": changed})


@app.route("/download")
def download():
    """Download the raw results.csv."""
    if not os.path.exists(CSV_PATH):
        return Response("No results yet.", status=404)
    with open(CSV_PATH, encoding="utf-8") as f:
        data = f.read()
    return Response(
        data, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=results.csv"},
    )


@app.route("/delete", methods=["POST"])
def delete():
    row_id = (request.json or {}).get("id")
    if not isinstance(row_id, int) or not delete_row(row_id):
        return jsonify({"error": "Could not delete that entry."}), 400
    return jsonify({"ok": True})


@app.route("/delete-all", methods=["POST"])
def delete_all_rows():
    """Remove every saved listing."""
    if os.path.exists(CSV_PATH):
        os.remove(CSV_PATH)
    return jsonify({"ok": True})


# Upgrade any pre-existing results.csv to the current column set on startup.
_ensure_schema()


def _lan_ip():
    """This Mac's IP on the local network, for phone access."""
    import socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


if __name__ == "__main__":
    port = 5001  # 5000 is often taken by macOS AirPlay Receiver / ControlCentre.
    ip = _lan_ip()
    print("\n" + "=" * 56)
    print("  PropertyGuru Extractor is running")
    print(f"  On this Mac:              http://127.0.0.1:{port}")
    print(f"  On your phone (same Wi-Fi): http://{ip}:{port}")
    print("  (Keep this window open. Ctrl+C to stop.)")
    print("=" * 56 + "\n")
    # host=0.0.0.0 so other devices on your Wi-Fi (your phone) can reach it.
    # threaded=True so the blocking Playwright call doesn't freeze the server.
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)
