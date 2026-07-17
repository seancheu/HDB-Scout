"""Official per-block HDB data (data.gov.sg "HDB Property Information").

Gives the exact block's year completed, floors, dwelling units, and whether the
block itself has a multi-storey carpark or market/hawker centre.
"""

import json
import time

import requests

from cov import split_block_street

_RESOURCE = "d_17f5382f26140b1fdae0ba2ef6239d2f"
_API = "https://data.gov.sg/api/action/datastore_search"
_cache = {}
_TTL = 7 * 24 * 3600


def block_info(block_street):
    """Profile dict for '261B Sengkang East Way', or None."""
    block, street = split_block_street(block_street)
    if not (block and street):
        return None
    key = f"{block}|{street}"
    hit = _cache.get(key)
    if hit and time.time() - hit[0] < _TTL:
        return hit[1]
    try:
        r = requests.get(_API, params={
            "resource_id": _RESOURCE,
            "filters": json.dumps({"blk_no": block, "street": street}),
            "limit": 1,
        }, timeout=20)
        recs = r.json()["result"]["records"]
    except Exception:
        return None
    if not recs:
        _cache[key] = (time.time(), None)
        return None
    rec = recs[0]
    out = {
        "block": block,
        "street": street,
        "year_completed": rec.get("year_completed"),
        "max_floor": rec.get("max_floor_lvl"),
        "total_units": rec.get("total_dwelling_units"),
        "mscp": rec.get("multistorey_carpark") == "Y",
        "market_hawker": rec.get("market_hawker") == "Y",
        "precinct_pavilion": rec.get("precinct_pavilion") == "Y",
    }
    _cache[key] = (time.time(), out)
    return out
