"""One-off extension: fetch World Rugby bulk match list for 2015-01-01 to
2018-12-31, so build_squad_ages.py picks up Eddie Jones's full England
tenure (he took over Feb 2016; the original 6-team/ages build only covers
2019-2026). Cached pages use a distinct prefix (page_pre2019_N.json) so they
don't collide with the existing 2019-2026 page_N.json cache files that
matches_for_team() in build_squad_ages.py globs over.
"""
import json
import os
import time

import httpx

BASE = "https://api.wr-rims-prod.pulselive.com/rugby/v3"
ROOT = os.path.expanduser("~/rugby_analytics")
BULK_CACHE = os.path.join(ROOT, "data/raw/wr_api_cache/bulk")
os.makedirs(BULK_CACHE, exist_ok=True)

CLIENT = httpx.Client(timeout=30.0)
SLEEP = 0.25


def _get(url, params=None):
    for attempt in range(5):
        try:
            r = CLIENT.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            print(f"  WARN status={r.status_code} url={url} params={params}")
            time.sleep(1.0)
        except httpx.HTTPError as e:
            print(f"  WARN exception {e} url={url}")
            time.sleep(1.0)
    return None


def main():
    page = 0
    page_size = 100
    total_calls = 0
    while True:
        cache_path = os.path.join(BULK_CACHE, f"page_pre2019_{page}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                d = json.load(f)
        else:
            d = _get(f"{BASE}/match", params={
                "states": "U,UP,L,CC,C",
                "sort": "asc",
                "page": page,
                "pageSize": page_size,
                "startDate": "2015-01-01",
                "endDate": "2018-12-31",
                "sport": "mru",
            })
            total_calls += 1
            time.sleep(SLEEP)
            if d is None:
                print(f"FAILED page {page}, stopping")
                break
            with open(cache_path, "w") as f:
                json.dump(d, f)
        num_pages = d["pageInfo"]["numPages"]
        print(f"page {page}/{num_pages} entries={len(d['content'])}")
        page += 1
        if page >= num_pages:
            break
    print(f"bulk fetch done, api calls={total_calls}")


if __name__ == "__main__":
    main()
