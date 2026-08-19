"""One-off backfill of eng irb / opp irb columns in england_rugby.csv for
rows that have a real date but blank ranking (2015-2020 legacy rows).
Ireland was checked and has zero rows in this category (its blank-irb rows
all also lack dates), so it's not touched here.
"""
import csv
import json
import time
import urllib.request
from datetime import date, timedelta
from pathlib import Path

ROOT = Path.home() / "rugby_analytics"
CSV_PATH = ROOT / "data/raw/sheets_export/england_rugby.csv"
CACHE_DIR = ROOT / "data/raw/wr_api_cache/rankings"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

OPP_NAME = {
    "wales": "Wales", "italy": "Italy", "ireland": "Ireland", "scotland": "Scotland",
    "france": "France", "fiji": "Fiji", "australia": "Australia", "uruguay": "Uruguay",
    "sa": "South Africa", "argentina": "Argentina", "nz": "New Zealand", "japan": "Japan",
    "tonga": "Tonga", "usa": "USA", "georgia": "Georgia", "samoa": "Samoa",
}


def rankings_for_date(d: date) -> dict:
    key = d.isoformat()
    cache_file = CACHE_DIR / f"{key}.json"
    if cache_file.exists():
        data = json.loads(cache_file.read_text())
    else:
        url = f"https://api.wr-rims-prod.pulselive.com/rugby/v3/rankings/mru?language=en&date={key}"
        with urllib.request.urlopen(url) as resp:
            data = json.loads(resp.read())
        cache_file.write_text(json.dumps(data))
        time.sleep(0.25)
    return {e["team"]["name"]: e["team"]["pos"] for e in data["entries"] if "pos" in e["team"]} \
        if "pos" in data["entries"][0]["team"] else {e["team"]["name"]: e["pos"] for e in data["entries"]}


rows = list(csv.reader(open(CSV_PATH, newline="")))
header, body = rows[0], rows[1:]

filled = 0
skipped_no_opp_match = []
for row in body:
    date_str, _, opp = row[0], row[1], row[2]
    if not date_str or row[4] != "":
        continue  # no date, or already populated
    d = date.fromisoformat("-".join(reversed(date_str.split("/"))))
    lookup_day = d - timedelta(days=1)
    ranks = rankings_for_date(lookup_day)
    eng_pos = ranks.get("England")
    opp_full = OPP_NAME.get(opp.strip().lower())
    opp_pos = ranks.get(opp_full) if opp_full else None
    if eng_pos is None or opp_pos is None:
        skipped_no_opp_match.append((date_str, opp))
        continue
    row[4] = str(eng_pos)
    row[5] = str(opp_pos)
    filled += 1

with open(CSV_PATH, "w", newline="") as f:
    w = csv.writer(f)
    w.writerow(header)
    w.writerows(body)

print(f"Filled {filled} rows")
print(f"Skipped (no name match): {skipped_no_opp_match}")
