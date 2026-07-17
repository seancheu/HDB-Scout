"""Folium map rendering for listings: an HDB block + all nearby MRT/LRT stations,
either for a single property or several compared on one map.

Coordinates normally come straight from the PropertyGuru listing data (both the
block and every station carry lat/lon). If a block's coordinates are ever
missing (e.g. an older saved row), we fall back to geocoding the address via
Singapore's OneMap API — the same method used by the HDB_Mapper project.
"""

import json
import os

import folium
import requests

# OneMap credentials (free account at onemap.gov.sg) unlock geocoding,
# routing/commute times, and the hawker-centre dataset. Sourced from, in order:
#   1. env vars ONEMAP_EMAIL / ONEMAP_PASSWORD
#   2. a local secrets.json next to this file: {"ONEMAP_EMAIL": ..., "ONEMAP_PASSWORD": ...}
#   3. (legacy) a sibling HDB_Mapper project's .streamlit/secrets.toml
# Everything else degrades gracefully without them.
_SECRETS_JSON = os.path.join(os.path.dirname(__file__), "secrets.json")
_LEGACY_TOML = os.path.join(os.path.dirname(__file__), "..", "HDB_Mapper",
                            ".streamlit", "secrets.toml")
SG_CENTER = [1.3521, 103.8198]

# Distinct marker colours for the compare view (Folium's supported set).
PALETTE = ["red", "blue", "green", "purple", "orange", "darkred",
           "cadetblue", "darkpurple", "darkgreen", "black"]


def _tiles(theme):
    """Clean, minimalist basemaps: light or dark."""
    return "cartodbdark_matter" if theme == "dark" else "cartodbpositron"


# --------------------------------------------------------------------------- #
# Geocoding fallback (HDB_Mapper's OneMap method)
# --------------------------------------------------------------------------- #

def _load_onemap_creds():
    email = os.environ.get("ONEMAP_EMAIL")
    password = os.environ.get("ONEMAP_PASSWORD")
    if email and password:
        return email, password
    try:
        with open(_SECRETS_JSON, encoding="utf-8") as f:
            data = json.load(f)
        if data.get("ONEMAP_EMAIL") and data.get("ONEMAP_PASSWORD"):
            return data["ONEMAP_EMAIL"], data["ONEMAP_PASSWORD"]
    except Exception:
        pass
    try:  # legacy sibling-project fallback
        import tomllib
        with open(_LEGACY_TOML, "rb") as f:
            data = tomllib.load(f)
        return data.get("ONEMAP_EMAIL"), data.get("ONEMAP_PASSWORD")
    except Exception:
        return None, None


_token_cache = {}


def _onemap_token():
    if "token" in _token_cache:
        return _token_cache["token"]
    email, password = _load_onemap_creds()
    if not email or not password:
        return None
    try:
        r = requests.post(
            "https://www.onemap.gov.sg/api/auth/post/getToken",
            json={"email": email, "password": password},
            timeout=12,
        )
        token = r.json().get("access_token") if r.status_code == 200 else None
    except Exception:
        token = None
    _token_cache["token"] = token
    return token


def _next_weekday_peak():
    """Next weekday date + a morning-peak time, for transit routing."""
    from datetime import datetime, timedelta
    d = datetime.now()
    while d.weekday() >= 5:            # skip Sat/Sun
        d += timedelta(days=1)
    return d.strftime("%m-%d-%Y"), "08:30:00"


def route_minutes(o_lat, o_lon, d_lat, d_lon, mode="pt"):
    """Travel time in minutes via OneMap routing. mode: 'pt' (transit) or 'drive'.
    Returns None on failure."""
    token = _onemap_token()
    if not token:
        return None
    params = {"start": f"{o_lat},{o_lon}", "end": f"{d_lat},{d_lon}",
              "routeType": mode}
    if mode == "pt":
        date, time = _next_weekday_peak()
        params.update({"mode": "TRANSIT", "date": date, "time": time,
                       "numItineraries": 1})
    try:
        r = requests.get("https://www.onemap.gov.sg/api/public/routingsvc/route",
                         params=params, headers={"Authorization": token}, timeout=15)
        j = r.json()
        if mode == "pt":
            its = j.get("plan", {}).get("itineraries", [])
            if its:
                return round(its[0]["duration"] / 60)
        else:
            rs = j.get("route_summary", {})
            if "total_time" in rs:
                return round(rs["total_time"] / 60)
    except Exception:
        pass
    return None


def geocode_address(query):
    """Geocode a Singapore address to (lat, lon) via OneMap. HDB_Mapper's method."""
    token = _onemap_token()
    if not token or not query:
        return None, None
    try:
        r = requests.get(
            "https://www.onemap.gov.sg/api/common/elastic/search",
            params={"searchVal": query, "returnGeom": "Y",
                    "getAddrDetails": "N", "pageNum": 1},
            headers={"Authorization": token},
            timeout=10,
        )
        results = r.json().get("results", []) if r.status_code == 200 else []
        if results:
            return float(results[0]["LATITUDE"]), float(results[0]["LONGITUDE"])
    except Exception:
        pass
    return None, None


# --------------------------------------------------------------------------- #
# Nearest MRT/LRT lookup (for sites that don't embed station data, e.g. SRX)
# Reuses HDB_Mapper's approach: the mrtsg.csv station dataset + haversine.
# --------------------------------------------------------------------------- #

_MRT_CSV_URL = ("https://raw.githubusercontent.com/hxchua/datadoubleconfirm/"
                "master/datasets/mrtsg.csv")
_MRT_CSV_PATH = os.path.join(os.path.dirname(__file__), "mrt_stations.csv")
_stations_cache = []


def _haversine_m(lat1, lon1, lat2, lon2):
    import math
    r = 6371000
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _load_mrt_stations():
    if _stations_cache:
        return _stations_cache
    import csv
    if not os.path.exists(_MRT_CSV_PATH):
        try:
            r = requests.get(_MRT_CSV_URL, timeout=15)
            r.raise_for_status()
            with open(_MRT_CSV_PATH, "w", encoding="utf-8") as f:
                f.write(r.text)
        except Exception:
            return []
    with open(_MRT_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                name = row["STN_NAME"].title().replace("Mrt", "MRT").replace("Lrt", "LRT")
                _stations_cache.append({
                    "name": f"{row['STN_NO']} {name}",
                    "lat": float(row["Latitude"]),
                    "lon": float(row["Longitude"]),
                    "type": "LRT" if "LRT" in row["STN_NAME"].upper() else "MRT",
                })
            except Exception:
                continue
    return _stations_cache


def nearest_stations(lat, lon, n=4, max_km=2.0):
    """Nearest MRT/LRT stations to a point, PG-station-dict shaped.
    Distances are straight-line; walking minutes estimated at ~80 m/min."""
    out = []
    for s in _load_mrt_stations():
        d = _haversine_m(lat, lon, s["lat"], s["lon"])
        out.append({**s, "distance_m": int(round(d)),
                    "walk_mins": max(1, round(d / 80))})
    out.sort(key=lambda s: s["distance_m"])
    within = [s for s in out if s["distance_m"] <= max_km * 1000]
    return (within or out[:2])[:n]


# --------------------------------------------------------------------------- #
# Nearest primary-school lookup (for sites that don't embed school data)
# Reuses HDB_Mapper's geocoded school list; rebuilds it via OneMap if missing.
# --------------------------------------------------------------------------- #

_SCHOOLS_CSV_PATH = os.path.join(os.path.dirname(__file__), "primary_schools.csv")
_HDB_MAPPER_SCHOOLS = os.path.join(os.path.dirname(__file__), "..", "HDB_Mapper",
                                   "Data", "primary_schools_geocoded.csv")
_SCHOOLS_SRC_URL = ("https://raw.githubusercontent.com/chuabern/"
                    "DBA3702-Shiny-App-Project/master/"
                    "general-information-of-schools.csv")
_schools_cache = []


def _ensure_schools_csv():
    """Make sure primary_schools.csv exists locally: copy HDB_Mapper's cache,
    else rebuild from the schools directory + OneMap geocoding (its method)."""
    if os.path.exists(_SCHOOLS_CSV_PATH):
        return True
    if os.path.exists(_HDB_MAPPER_SCHOOLS):
        import shutil
        shutil.copy(_HDB_MAPPER_SCHOOLS, _SCHOOLS_CSV_PATH)
        return True
    import csv as _csv
    import io
    try:
        r = requests.get(_SCHOOLS_SRC_URL, timeout=20)
        r.raise_for_status()
        rows = list(_csv.DictReader(io.StringIO(r.text)))
    except Exception:
        return False
    records = []
    for row in rows:
        if "PRIMARY" not in str(row.get("mainlevel_code", "")).upper():
            continue
        lat, lon = geocode_address(str(row.get("postal_code", "")).zfill(6))
        if lat and lon:
            records.append({"NAME": row["school_name"], "Latitude": lat,
                            "Longitude": lon})
    if not records:
        return False
    with open(_SCHOOLS_CSV_PATH, "w", newline="", encoding="utf-8") as f:
        w = _csv.DictWriter(f, fieldnames=["NAME", "Latitude", "Longitude"])
        w.writeheader()
        w.writerows(records)
    return True


def _load_schools():
    if _schools_cache:
        return _schools_cache
    import csv
    if not _ensure_schools_csv():
        return []
    with open(_SCHOOLS_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                _schools_cache.append({
                    "name": row["NAME"].title(),
                    "lat": float(row["Latitude"]),
                    "lon": float(row["Longitude"]),
                })
            except Exception:
                continue
    return _schools_cache


def nearest_schools(lat, lon, n=5, max_km=1.0):
    """Nearest government primary schools to a point, PG-school-dict shaped.
    Straight-line distances, ~80 m/min walking estimate."""
    out = []
    for s in _load_schools():
        d = _haversine_m(lat, lon, s["lat"], s["lon"])
        out.append({**s, "distance_m": int(round(d)),
                    "walk_mins": max(1, round(d / 80))})
    out.sort(key=lambda s: s["distance_m"])
    within = [s for s in out if s["distance_m"] <= max_km * 1000]
    return (within or out[:3])[:n]


# --------------------------------------------------------------------------- #
# Nearby malls & hawker centres (SG-wide, cached locally)
# Malls: community-scraped coordinate list. Hawkers: OneMap NEA theme.
# --------------------------------------------------------------------------- #

_MALLS_CSV_PATH = os.path.join(os.path.dirname(__file__), "malls.csv")
_MALLS_URL = ("https://raw.githubusercontent.com/ValaryLim/"
              "Mall-Coordinates-Web-Scraper/master/mall_coordinates_updated.csv")
_HAWKER_CSV_PATH = os.path.join(os.path.dirname(__file__), "hawker_centres.csv")
_malls_cache = []
_hawker_cache = []


def _load_malls():
    if _malls_cache:
        return _malls_cache
    import csv
    if not os.path.exists(_MALLS_CSV_PATH):
        try:
            r = requests.get(_MALLS_URL, timeout=20)
            r.raise_for_status()
            with open(_MALLS_CSV_PATH, "w", encoding="utf-8") as f:
                f.write(r.text)
        except Exception:
            return []
    with open(_MALLS_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            try:
                _malls_cache.append({"name": row["name"],
                                     "lat": float(row["latitude"]),
                                     "lon": float(row["longitude"])})
            except Exception:
                continue
    return _malls_cache


def _load_hawkers():
    if _hawker_cache:
        return _hawker_cache
    import csv
    if not os.path.exists(_HAWKER_CSV_PATH):
        token = _onemap_token()
        if not token:
            return []
        try:
            r = requests.get(
                "https://www.onemap.gov.sg/api/public/themesvc/retrieveTheme",
                params={"queryName": "ssot_hawkercentres"},
                headers={"Authorization": token}, timeout=20)
            results = r.json().get("SrchResults", [])
        except Exception:
            return []
        rows = []
        for e in results:
            latlng = e.get("LatLng")
            if not latlng or "," not in latlng:
                continue
            lat, lon = latlng.split(",")[:2]
            rows.append({"name": e.get("NAME", "Hawker Centre"),
                         "lat": lat.strip(), "lon": lon.strip()})
        if rows:
            with open(_HAWKER_CSV_PATH, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["name", "lat", "lon"])
                w.writeheader()
                w.writerows(rows)
    if os.path.exists(_HAWKER_CSV_PATH):
        with open(_HAWKER_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    _hawker_cache.append({"name": row["name"],
                                          "lat": float(row["lat"]),
                                          "lon": float(row["lon"])})
                except Exception:
                    continue
    return _hawker_cache


def _nearest(items, lat, lon, n, max_km):
    out = []
    for s in items:
        d = _haversine_m(lat, lon, s["lat"], s["lon"])
        out.append({**s, "distance_m": int(round(d)),
                    "walk_mins": max(1, round(d / 80))})
    out.sort(key=lambda s: s["distance_m"])
    picked = []
    for s in out:
        if s["distance_m"] > max_km * 1000:
            break
        # Skip near-duplicates (same place listed twice, e.g. "The X" / "X").
        if any(_haversine_m(s["lat"], s["lon"], p["lat"], p["lon"]) < 80 for p in picked):
            continue
        picked.append(s)
        if len(picked) >= n:
            break
    return picked


def nearest_malls(lat, lon, n=4, max_km=1.2):
    return _nearest(_load_malls(), lat, lon, n, max_km)


def nearest_hawkers(lat, lon, n=4, max_km=1.2):
    return _nearest(_load_hawkers(), lat, lon, n, max_km)


# --------------------------------------------------------------------------- #
# Preschools (ECDA geojson, reused from HDB_Mapper) & CHAS clinics (data.gov.sg)
# --------------------------------------------------------------------------- #

_PRESCHOOL_GEOJSON = os.path.join(os.path.dirname(__file__), "preschools.geojson")
_PRESCHOOL_POLL = ("https://api-open.data.gov.sg/v1/public/api/datasets/"
                   "d_61eefab99958fd70e6aab17320a71f1c/poll-download")
_LEGACY_PRESCHOOL = os.path.join(os.path.dirname(__file__), "..", "HDB_Mapper",
                                 "Data", "PreSchoolsLocation.geojson")
_CHAS_CSV_PATH = os.path.join(os.path.dirname(__file__), "chas_clinics.csv")
_CHAS_POLL = ("https://api-open.data.gov.sg/v1/public/api/datasets/"
              "d_548c33ea2d99e29ec63a7cc9edcccedc/poll-download")
_preschool_cache = []
_chas_cache = []


def _load_preschools():
    if _preschool_cache:
        return _preschool_cache
    import re as _re
    import shutil
    if not os.path.exists(_PRESCHOOL_GEOJSON):
        if os.path.exists(_LEGACY_PRESCHOOL):
            shutil.copy(_LEGACY_PRESCHOOL, _PRESCHOOL_GEOJSON)
        else:
            try:  # official ECDA dataset on data.gov.sg
                poll = requests.get(_PRESCHOOL_POLL, timeout=20).json()
                url = (poll.get("data") or {}).get("url")
                gj = requests.get(url, timeout=60).json()
                with open(_PRESCHOOL_GEOJSON, "w", encoding="utf-8") as f:
                    json.dump(gj, f)
            except Exception:
                return []
    try:
        with open(_PRESCHOOL_GEOJSON, encoding="utf-8") as f:
            gj = json.load(f)
    except Exception:
        return []
    name_re = _re.compile(r"CENTRE_NAME</th>\s*<td>(.*?)</td>", _re.I)
    for feat in gj.get("features", []):
        try:
            coords = feat["geometry"]["coordinates"]
            m = name_re.search(feat["properties"].get("Description", ""))
            _preschool_cache.append({
                "name": (m.group(1).title() if m else "Preschool"),
                "lat": float(coords[1]), "lon": float(coords[0]),
            })
        except Exception:
            continue
    return _preschool_cache


def _load_chas():
    if _chas_cache:
        return _chas_cache
    import csv
    import re as _re
    if not os.path.exists(_CHAS_CSV_PATH):
        try:
            poll = requests.get(_CHAS_POLL, timeout=20).json()
            url = (poll.get("data") or {}).get("url")
            gj = requests.get(url, timeout=60).json()
        except Exception:
            return []
        name_re = _re.compile(r"HCI_NAME</th>\s*<td>(.*?)</td>", _re.I)
        rows = []
        for feat in gj.get("features", []):
            try:
                coords = feat["geometry"]["coordinates"]
                m = name_re.search(feat["properties"].get("Description", ""))
                rows.append({"name": (m.group(1).title() if m else "CHAS Clinic"),
                             "lat": coords[1], "lon": coords[0]})
            except Exception:
                continue
        if rows:
            with open(_CHAS_CSV_PATH, "w", newline="", encoding="utf-8") as f:
                w = csv.DictWriter(f, fieldnames=["name", "lat", "lon"])
                w.writeheader()
                w.writerows(rows)
    if os.path.exists(_CHAS_CSV_PATH):
        with open(_CHAS_CSV_PATH, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                try:
                    _chas_cache.append({"name": row["name"],
                                        "lat": float(row["lat"]),
                                        "lon": float(row["lon"])})
                except Exception:
                    continue
    return _chas_cache


def nearest_preschools(lat, lon, n=4, max_km=0.8):
    return _nearest(_load_preschools(), lat, lon, n, max_km)


def nearest_clinics(lat, lon, n=3, max_km=0.8):
    return _nearest(_load_chas(), lat, lon, n, max_km)


# --------------------------------------------------------------------------- #
# Map building
# --------------------------------------------------------------------------- #

def _station_label(s):
    bits = [s.get("name") or "Station"]
    if s.get("distance_m"):
        bits.append(f"{s['distance_m']} m")
    if s.get("walk_mins"):
        bits.append(f"~{s['walk_mins']} min walk")
    return " · ".join(bits)


def _add_property(m, block_street, block_lat, block_lon, stations, schools=None,
                  malls=None, hawkers=None, extras=None, row_id=None, url=None,
                  block_color="red", station_color=None, school_color="purple",
                  line_color="gray", draw_school_lines=True):
    """Add one property's block + stations + schools (+ malls/hawkers) to map `m`.

    Returns the list of [lat, lon] points added (for fit_bounds).
    """
    stations = stations or []
    schools = schools or []
    malls = malls or []
    hawkers = hawkers or []
    pts = []

    if block_lat and block_lon:
        folium.Marker(
            [block_lat, block_lon],
            tooltip=f"🏠 {block_street or 'Block'}",
            popup=_listing_popup(block_street, None, row_id, url),
            icon=folium.Icon(color=block_color, icon="home", prefix="fa"),
        ).add_to(m)
        pts.append([block_lat, block_lon])

    def _place(poi, color, icon, connect):
        label = _station_label(poi)
        folium.Marker(
            [poi["lat"], poi["lon"]],
            tooltip=label, popup=label,
            icon=folium.Icon(color=color, icon=icon, prefix="fa"),
        ).add_to(m)
        pts.append([poi["lat"], poi["lon"]])
        if connect and block_lat and block_lon:
            folium.PolyLine(
                [[block_lat, block_lon], [poi["lat"], poi["lon"]]],
                color=line_color, weight=1.4, opacity=0.6, dash_array="4, 4",
                tooltip=label,
            ).add_to(m)

    for s in stations:
        # In single view, colour by station type; in compare, use the shared colour.
        color = station_color or ("blue" if s.get("type") == "MRT" else "green")
        _place(s, color, "train", connect=True)

    for sc in schools:
        _place(sc, station_color or school_color, "graduation-cap",
               connect=draw_school_lines)
    for ml in malls:
        _place(ml, station_color or "orange", "shopping-cart", connect=False)
    for hk in hawkers:
        _place(hk, station_color or "cadetblue", "cutlery", connect=False)
    for ps in (extras or {}).get("preschools", []):
        _place(ps, station_color or "pink", "child", connect=False)
    for cl in (extras or {}).get("clinics", []):
        _place(cl, station_color or "darkblue", "plus", connect=False)
    return pts


def _finish(m, pts):
    if len(pts) > 1:
        m.fit_bounds(pts, padding=(30, 30))
    return m.get_root().render()


def build_map_html(block_street, block_lat, block_lon, stations, schools=None,
                   theme="light", url=None):
    """Standalone map for ONE property (block + nearby MRT/LRT + primary schools)."""
    stations = stations or []
    schools = schools or []
    if not (block_lat and block_lon) and block_street:
        block_lat, block_lon = geocode_address(f"{block_street} Singapore")

    if block_lat and block_lon:
        center = [block_lat, block_lon]
    elif stations:
        center = [stations[0]["lat"], stations[0]["lon"]]
    else:
        center = SG_CENTER

    has_pt = bool(block_lat and block_lon)
    malls = nearest_malls(block_lat, block_lon) if has_pt else []
    hawkers = nearest_hawkers(block_lat, block_lon) if has_pt else []
    extras = {
        "preschools": nearest_preschools(block_lat, block_lon) if has_pt else [],
        "clinics": nearest_clinics(block_lat, block_lon) if has_pt else [],
    }

    m = folium.Map(location=center, zoom_start=16, tiles=_tiles(theme),
                   control_scale=True)
    pts = _add_property(m, block_street, block_lat, block_lon, stations, schools,
                        malls, hawkers, extras, url=url, block_color="red")
    # Legend so the marker colours are self-explanatory.
    legend = [("🏠 Block", _SWATCH["red"]), ("🚇 MRT", _SWATCH["blue"]),
              ("🚈 LRT", _SWATCH["green"])]
    if schools:
        legend.append(("🎓 Primary school", _SWATCH["purple"]))
    if malls:
        legend.append(("🛒 Mall", _SWATCH["orange"]))
    if hawkers:
        legend.append(("🍜 Hawker centre", _SWATCH["cadetblue"]))
    if extras["preschools"]:
        legend.append(("👶 Preschool", _SWATCH["pink"]))
    if extras["clinics"]:
        legend.append(("🏥 CHAS clinic", _SWATCH["darkblue"]))
    m.get_root().html.add_child(_legend(legend, title="Legend"))
    return _finish(m, pts)


def _listing_popup(name, price=None, row_id=None, url=None):
    """Marker popup with actions: focus the row in the app (via postMessage to
    the parent page — the map is same-origin) and open the original listing."""
    import html as _html
    lines = [f"<b>{_html.escape(name or 'Listing')}</b>"]
    if price:
        lines.append(_html.escape(str(price)))
    links = []
    if row_id is not None:
        links.append(
            f"<a href=\"#\" style=\"color:#ef2d56;font-weight:700;text-decoration:none\" "
            f"onclick=\"parent.postMessage({{type:'focus-listing',id:{int(row_id)}}},'*');"
            f"return false;\">Open in app</a>")
    if url:
        links.append(
            f"<a href=\"{_html.escape(str(url), quote=True)}\" target=\"_blank\" "
            f"rel=\"noopener\" style=\"color:#ef2d56;font-weight:700;"
            f"text-decoration:none\">View listing ↗</a>")
    if links:
        lines.append(" · ".join(links))
    body = "<br>".join(lines)
    return folium.Popup(
        f'<div style="font:13px/1.6 -apple-system,BlinkMacSystemFont,sans-serif;'
        f'min-width:170px">{body}</div>', max_width=300)


def build_overview_map_html(items, theme="light"):
    """Area overview: ONE pin per property (block only, no POIs).

    `items` carry block_street / price / block_lat / block_lon / town. When the
    set spans several towns, each town gets its own pin colour + a legend.
    """
    towns = sorted({it.get("town") for it in items if it.get("town")})
    multi = len(towns) > 1
    town_color = {t: PALETTE[i % len(PALETTE)] for i, t in enumerate(towns)}

    m = folium.Map(location=SG_CENTER, zoom_start=12, tiles=_tiles(theme),
                   control_scale=True)
    pts = []
    for it in items:
        lat, lon = it.get("block_lat"), it.get("block_lon")
        if not (lat and lon):
            continue
        label = " · ".join(x for x in (it.get("block_street"), it.get("price")) if x)
        color = town_color.get(it.get("town"), "red") if multi else "red"
        folium.Marker(
            [lat, lon], tooltip=label,
            popup=_listing_popup(it.get("block_street"), it.get("price"),
                                 it.get("id"), it.get("url")),
            icon=folium.Icon(color=color, icon="home", prefix="fa"),
        ).add_to(m)
        pts.append([lat, lon])
    if multi:
        m.get_root().html.add_child(_legend(
            [(t, _SWATCH.get(town_color[t], "#333")) for t in towns],
            title="Towns"))
    return _finish(m, pts)


def _legend(items_colors, title="Properties"):
    """Floating legend box mapping labels to their colour."""
    rows = "".join(
        f'<div style="display:flex;align-items:center;gap:6px;margin:2px 0">'
        f'<span style="width:11px;height:11px;border-radius:50%;background:{c};'
        f'display:inline-block;border:1px solid #0003"></span>'
        f'<span>{name}</span></div>'
        for name, c in items_colors
    )
    return folium.Element(
        '<div style="position:fixed;bottom:18px;left:18px;z-index:9999;'
        'background:rgba(255,255,255,.92);padding:10px 12px;border-radius:8px;'
        'font:12px/1.3 -apple-system,sans-serif;color:#1a2027;'
        'box-shadow:0 1px 6px #0003">'
        f'<b>{title}</b>{rows}</div>'
    )


# Approximate CSS colour for each Folium palette name (for the legend swatch).
_SWATCH = {
    "red": "#d33", "blue": "#38a", "green": "#3a3", "purple": "#93c",
    "orange": "#e80", "darkred": "#900", "cadetblue": "#579",
    "darkpurple": "#639", "darkgreen": "#183", "black": "#333",
    "pink": "#e6a", "darkblue": "#236",
}


def build_compare_map_html(items, theme="light"):
    """Standalone map comparing SEVERAL properties, each in its own colour.

    `items` is a list of dicts: block_street, block_lat, block_lon, stations.
    """
    m = folium.Map(location=SG_CENTER, zoom_start=14, tiles=_tiles(theme),
                   control_scale=True)
    all_pts = []
    legend = []
    for i, it in enumerate(items):
        color = PALETTE[i % len(PALETTE)]
        lat, lon = it.get("block_lat"), it.get("block_lon")
        if not (lat and lon) and it.get("block_street"):
            lat, lon = geocode_address(f"{it['block_street']} Singapore")
        pts = _add_property(
            m, it.get("block_street"), lat, lon, it.get("stations"),
            it.get("schools"), row_id=it.get("id"), url=it.get("url"),
            block_color=color, station_color=color, line_color=color,
            draw_school_lines=False,  # markers only in compare, to reduce clutter
        )
        all_pts += pts
        legend.append((it.get("block_street") or "Property", _SWATCH.get(color, "#333")))
    if legend:
        m.get_root().html.add_child(_legend(legend))
    return _finish(m, all_pts)
