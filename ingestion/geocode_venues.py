"""
Geocode the unique venues in match_venues.csv via Open-Meteo's free geocoding
API (no key required), caching results to disk so this only ever needs to run
once per new venue -- not on every dataset rebuild or in CI.

Output: data/raw/venue_geocode_cache.json, {venue_name: {lat, lon, resolved_as}}
or {venue_name: null} for a venue that couldn't be resolved (logged for
manual follow-up rather than silently dropped).
"""

import json
import time
from pathlib import Path

import httpx
import pandas as pd

VENUES_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "sheets_export" / "match_venues.csv"
CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "venue_geocode_cache.json"
GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# World Rugby's venue.country is often a constituent country (England, Wales,
# Scotland) rather than the sovereign state Open-Meteo returns (United Kingdom).
UK_CONSTITUENTS = {"england", "wales", "scotland", "northern ireland"}


def country_matches(result: dict, expected_country: str) -> bool:
    if not expected_country:
        return True
    expected = expected_country.lower()
    result_country = (result.get("country") or "").lower()
    result_admin1 = (result.get("admin1") or "").lower()
    if result_country == expected:
        return True
    if expected in UK_CONSTITUENTS and result_country == "united kingdom":
        return True
    if result_admin1 == expected:
        return True
    return False


def city_candidates(city: str) -> list[str]:
    """'Kumamoto Prefecture, Kumamoto City' -> ['Kumamoto Prefecture, Kumamoto City', 'Kumamoto City', 'Kumamoto', ...]"""
    candidates = [city]
    parts = [p.strip() for p in city.split(",")]
    candidates.extend(reversed(parts))
    cleaned = set()
    for c in list(candidates):
        stripped = c.replace(" Prefecture", "").replace(" City", "").replace(" and Hove", "").strip()
        if stripped:
            cleaned.add(stripped)
    candidates.extend(cleaned)
    seen, out = set(), []
    for c in candidates:
        if c and c not in seen:
            seen.add(c)
            out.append(c)
    return out


def geocode(name: str, city: str, country: str) -> dict | None:
    queries = [name] + city_candidates(city)
    for query in queries:
        if not query:
            continue
        try:
            resp = httpx.get(GEOCODE_URL, params={"name": query, "count": 10}, timeout=15)
            resp.raise_for_status()
            results = resp.json().get("results") or []
        except (httpx.HTTPError, ValueError):
            continue
        for r in results:
            if country_matches(r, country):
                return {"lat": r["latitude"], "lon": r["longitude"],
                        "resolved_as": f"{r.get('name')}, {r.get('country')}", "query_used": query}
    return None


def main():
    df = pd.read_csv(VENUES_PATH, dtype=str)
    venues = df[["venue", "city", "country"]].drop_duplicates()

    cache = json.load(open(CACHE_PATH)) if CACHE_PATH.exists() else {}
    n_fetched, n_failed = 0, 0

    for _, row in venues.iterrows():
        key = row["venue"]
        if key in cache and cache[key] is not None:
            continue
        result = geocode(row["venue"], row["city"], row["country"])
        cache[key] = result
        if result is None:
            n_failed += 1
            print(f"FAILED to geocode: {row['venue']} ({row['city']}, {row['country']})")
        else:
            n_fetched += 1
        time.sleep(0.2)   # polite rate limit

    CACHE_PATH.write_text(json.dumps(cache, indent=2, sort_keys=True))
    print(f"\nGeocoded {n_fetched} new venue(s), {n_failed} failed, {len(cache)} total cached -> {CACHE_PATH}")


if __name__ == "__main__":
    main()
