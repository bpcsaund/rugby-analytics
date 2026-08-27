"""
Fetch historical weather (temperature, precipitation, wind) at kickoff time
for every match in match_venues.csv, via Open-Meteo's free historical
archive API (no key required). One request per (venue, year) -- not per
match -- with raw responses cached to data/raw/weather_cache/ so re-running
this only fetches newly-added venues/years, same pattern as wr_api_cache.

Only resolves matches whose venue was successfully geocoded (see
geocode_venues.py) and whose date is in the past (the archive API has no
forecast data -- see predict_upcoming_match.py's --forecast option for
upcoming fixtures).

Output: data/raw/sheets_export/match_weather.csv, one row per
team-perspective per match: date, team, opposition, temp_c, precip_mm, wind_kmh.
"""

import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pandas as pd

VENUES_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "sheets_export" / "match_venues.csv"
GEOCODE_CACHE_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "venue_geocode_cache.json"
WEATHER_CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "raw" / "weather_cache"
OUT_PATH = Path(__file__).resolve().parent.parent / "data" / "raw" / "sheets_export" / "match_weather.csv"
ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def safe_filename(name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", name).strip("_").lower()


def fetch_year(lat: float, lon: float, year: int, cache_key: str) -> dict | None:
    cache_path = WEATHER_CACHE_DIR / f"{cache_key}_{year}.json"
    if cache_path.exists():
        return json.load(open(cache_path))
    end_date = min(f"{year}-12-31", datetime.now(timezone.utc).strftime("%Y-%m-%d"))
    try:
        resp = httpx.get(ARCHIVE_URL, params={
            "latitude": lat, "longitude": lon,
            "start_date": f"{year}-01-01", "end_date": end_date,
            "hourly": "temperature_2m,precipitation,wind_speed_10m",
            "timezone": "auto",
        }, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"  fetch failed for {cache_key} {year}: {e}")
        return None
    WEATHER_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(data))
    return data


def local_kickoff_hour(kickoff_utc: str, gmt_offset: float) -> datetime | None:
    if not kickoff_utc or pd.isna(gmt_offset):
        return None
    try:
        dt_utc = datetime.fromisoformat(kickoff_utc)
    except ValueError:
        return None
    local = dt_utc + timedelta(hours=float(gmt_offset))
    return local.replace(minute=0, second=0, microsecond=0)


def main():
    venues_df = pd.read_csv(VENUES_PATH, dtype=str)
    geocode_cache = json.load(open(GEOCODE_CACHE_PATH))

    rows = []
    n_no_coords, n_no_weather_data, n_future, n_ok = 0, 0, 0, 0
    year_fetch_cache: dict[tuple, dict] = {}

    now = datetime.now(timezone.utc)
    for _, row in venues_df.iterrows():
        coords = geocode_cache.get(row["venue"])
        if not coords:
            n_no_coords += 1
            continue

        local_dt = local_kickoff_hour(row["kickoff_utc"], row["gmt_offset"])
        if local_dt is None:
            continue
        if datetime.fromisoformat(row["kickoff_utc"]) > now:
            n_future += 1
            continue

        cache_key = safe_filename(row["venue"])
        year = local_dt.year
        fetch_key = (cache_key, year)
        if fetch_key not in year_fetch_cache:
            year_fetch_cache[fetch_key] = fetch_year(coords["lat"], coords["lon"], year, cache_key)
            time.sleep(2.0)
        year_data = year_fetch_cache[fetch_key]
        if not year_data:
            n_no_weather_data += 1
            continue

        target_iso = local_dt.strftime("%Y-%m-%dT%H:00")
        times = year_data.get("hourly", {}).get("time", [])
        try:
            idx = times.index(target_iso)
        except ValueError:
            n_no_weather_data += 1
            continue

        hourly = year_data["hourly"]
        rows.append(dict(
            date=row["date"], team=row["team"], opposition=row["opposition"],
            temp_c=hourly["temperature_2m"][idx],
            precip_mm=hourly["precipitation"][idx],
            wind_kmh=hourly["wind_speed_10m"][idx],
        ))
        n_ok += 1

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(OUT_PATH, index=False)
    print(f"Resolved weather for {n_ok} team-perspective rows -> {OUT_PATH}")
    print(f"Skipped: {n_no_coords} no geocode, {n_future} future dates (no archive data), "
          f"{n_no_weather_data} no matching weather hour")
    print(f"Fetched/cached {len(year_fetch_cache)} (venue, year) archive requests")


if __name__ == "__main__":
    main()
