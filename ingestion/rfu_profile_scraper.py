#!/usr/bin/env python3
"""
Scrape England player profile pages from englandrugby.com.

Strategy (WAF-safe):
  1. Fetch the squad page to get ~36 known-good profile slugs (1 request).
  2. Build England player slugs from rfu_lineups.csv names.
  3. Attempt the UNION: squad-page slugs first, then any extra slugs from the
     lineups that weren't already covered, skipping pure-surname entries that
     never resolve.
  4. 3-second polite delay between every request.

Extracts per player: caps, height, weight, position, career mins,
avg mins/game, England wins, draws, losses.

Outputs: data/raw/england_player_profiles.csv

Usage:
  python3 ingestion/rfu_profile_scraper.py
"""

import csv
import re
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR     = Path(__file__).parent.parent
LINEUPS_CSV  = BASE_DIR / "data" / "raw" / "rfu_lineups.csv"
OUT_CSV      = BASE_DIR / "data" / "raw" / "england_player_profiles.csv"

SQUAD_URL = "https://www.englandrugby.com/follow/england-men/senior-men"
PROFILE_URL = "https://www.englandrugby.com/follow/england-men/senior-men/{slug}"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

FIELDS = [
    "player_name", "slug", "url",
    "position", "caps",
    "age", "height_cm", "weight_kg",
    "career_mins", "avg_mins_per_game",
    "england_wins", "england_draws", "england_losses",
    "status",
]

DELAY_SECONDS = 3.0   # between every request
RETRY_DELAY   = 15.0  # extra pause before retrying a 403/202


# ---------------------------------------------------------------------------
# Slug helpers
# ---------------------------------------------------------------------------
def to_slug(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = re.sub(r"['\"`]", "", name)
    name = re.sub(r"[^a-z0-9\s\-]", "", name)
    name = re.sub(r"[\s_]+", "-", name.strip())
    return re.sub(r"-+", "-", name)


def is_multi_word(name: str) -> bool:
    return len(name.split()) > 1


# ---------------------------------------------------------------------------
# Step 1 — fetch squad page for known-good slugs (1 HTTP request)
# ---------------------------------------------------------------------------
def get_squad_slugs() -> list[str]:
    """Return slugs extracted from onclick handlers on the squad page."""
    print(f"Fetching squad page: {SQUAD_URL}")
    req = urllib.request.Request(SQUAD_URL, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            html = r.read().decode("utf-8", errors="ignore")
        slugs = re.findall(
            r"window\.location\.href\s*=\s*['\"]/follow/england-men/senior-men/([^'\"]+)['\"]",
            html,
        )
        print(f"  Found {len(set(slugs))} squad-page slugs")
        return list(dict.fromkeys(slugs))  # deduplicated, order preserved
    except Exception as e:
        print(f"  WARNING: could not fetch squad page ({e}) — continuing without it")
        return []


# ---------------------------------------------------------------------------
# Step 2 — collect England player names from lineups CSV
# ---------------------------------------------------------------------------
def get_lineup_england_names() -> list[str]:
    names: dict[str, None] = {}
    with open(LINEUPS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("team", "").lower().startswith("england"):
                n = row.get("player_name", "").strip()
                if n:
                    names[n] = None
    return list(names)


# ---------------------------------------------------------------------------
# Step 3 — build candidate list
# ---------------------------------------------------------------------------
def build_candidates(squad_slugs: list[str], england_names: list[str]) -> list[tuple[str, str]]:
    """
    Returns [(display_name, slug), ...] ordered as:
      - squad-page slugs first (best chance of success)
      - then multi-word lineup names whose slug wasn't already covered
    Single-word (surname-only) names are skipped — they never resolve.
    """
    seen: dict[str, str] = {}  # slug → display_name

    # Squad page slugs (use the slug as display name until we match a real name below)
    for slug in squad_slugs:
        seen[slug] = slug  # placeholder

    # Multi-word England lineup names
    for name in england_names:
        if not is_multi_word(name):
            continue
        slug = to_slug(name)
        if slug not in seen:
            seen[slug] = name
        else:
            # If the current holder is just a slug placeholder, upgrade to real name
            if seen[slug] == slug:
                seen[slug] = name

    def slug_to_name(slug: str) -> str:
        """Convert 'chandler-cunningham-south' → 'Chandler Cunningham-South'."""
        parts = slug.split("-")
        # Heuristic: if the slug has 3+ parts, the last two may be a hyphenated surname.
        # We keep title-case on all parts; the original hyphen structure is ambiguous,
        # so just title-case each word separated by spaces.
        return " ".join(w.capitalize() for w in parts)

    # Build final list: squad slugs first, then remaining
    result: list[tuple[str, str]] = []
    for slug in squad_slugs:
        display = seen[slug]
        if display == slug:  # still just a placeholder — title-case it
            display = slug_to_name(slug)
        result.append((display, slug))
    for name in england_names:
        if not is_multi_word(name):
            continue
        slug = to_slug(name)
        if slug not in {s for _, s in result}:
            result.append((name, slug))

    return result


# ---------------------------------------------------------------------------
# HTML parsing (pure regex — page is fully server-side rendered)
# ---------------------------------------------------------------------------
def _tag_text(html: str, pattern: str) -> str:
    m = re.search(pattern, html, re.DOTALL)
    return re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""


def parse_profile(html: str) -> dict:
    def tc03_val(label: str) -> str:
        pat = (
            r'tc03_player_details_title[^>]*>\s*' + re.escape(label) +
            r'\s*</p>\s*<p[^>]*tc03_player_details_subtitle[^>]*>(.*?)</p>'
        )
        return _tag_text(html, pat)

    def c108_val(label: str) -> str:
        return _tag_text(html, r'<p>\s*' + re.escape(label) + r'\s*</p>\s*<h3>(.*?)</h3>')

    def tc02_val(stat_id: str) -> str:
        m = re.search(
            r'id="tc02-player-stats-data-number--' + re.escape(stat_id) + r'">(.*?)<', html
        )
        return m.group(1).strip() if m else ""

    return {
        "position":          tc03_val("Position"),
        "caps":              tc03_val("Test Caps"),
        "age":               c108_val("Age"),
        "height_cm":         c108_val("Height"),
        "weight_kg":         c108_val("Weight"),
        "career_mins":       tc02_val("minutesPlayed"),
        "avg_mins_per_game": tc02_val("avgMinutesPlayed"),
        "england_wins":      tc02_val("wins"),
        "england_draws":     tc02_val("draws"),
        "england_losses":    tc02_val("losses"),
    }


# ---------------------------------------------------------------------------
# HTTP fetch with single 403/202 retry
# ---------------------------------------------------------------------------
def fetch_page(url: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except Exception:
        return 0, ""


def fetch_with_retry(url: str) -> tuple[int, str]:
    status, html = fetch_page(url)
    if status in (403, 202):
        print(f"    WAF challenge ({status}) — waiting {RETRY_DELAY}s then retrying")
        time.sleep(RETRY_DELAY)
        status, html = fetch_page(url)
    return status, html


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    squad_slugs   = get_squad_slugs()
    england_names = get_lineup_england_names()
    candidates    = build_candidates(squad_slugs, england_names)

    skipped_surnames = sum(
        1 for n in england_names
        if not is_multi_word(n) and to_slug(n) not in {s for _, s in candidates}
    )
    print(f"England lineup names     : {len(england_names)}")
    print(f"Single-word (skipped)    : {skipped_surnames}")
    print(f"Candidates to attempt    : {len(candidates)}\n")

    rows:     list[dict] = []
    successes  = 0
    not_found  = 0
    errors     = 0

    for i, (name, slug) in enumerate(candidates, 1):
        url = PROFILE_URL.format(slug=slug)
        status, html = fetch_with_retry(url)

        if status == 200:
            parsed = parse_profile(html)
            row = {"player_name": name, "slug": slug, "url": url, "status": "ok", **parsed}
            successes += 1
            print(
                f"  [{i}/{len(candidates)}] ✓  {slug:42s}"
                f"  caps={row['caps'] or '—':<4}  pos={row['position'] or '—':<14}"
                f"  ht={row['height_cm'] or '—':<5}  wt={row['weight_kg'] or '—'}"
            )
        elif status == 404:
            row = {f: "" for f in FIELDS}
            row.update({"player_name": name, "slug": slug, "url": url, "status": "not_found"})
            not_found += 1
            print(f"  [{i}/{len(candidates)}] 404 {slug}")
        else:
            row = {f: "" for f in FIELDS}
            row.update({"player_name": name, "slug": slug, "url": url,
                        "status": f"error_{status}"})
            errors += 1
            print(f"  [{i}/{len(candidates)}] ERR {slug}  (HTTP {status})")

        rows.append(row)
        if i < len(candidates):
            time.sleep(DELAY_SECONDS)

    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total = len(candidates)
    print(f"\n{'='*60}")
    print(f"Success rate : {successes}/{total} ({100*successes//total if total else 0}%)")
    print(f"Not found    : {not_found}")
    print(f"Errors       : {errors}")
    print(f"Output       : {OUT_CSV}")

    ok_rows = [r for r in rows if r["status"] == "ok"]
    if ok_rows:
        print(f"\nSample successful rows:")
        for r in ok_rows[:10]:
            print(
                f"  {r['player_name']:<28s}  caps={r['caps']:<4}  "
                f"pos={r['position']:<14}  ht={r['height_cm']:<5}  wt={r['weight_kg']:<5}  "
                f"mins={r['career_mins']:<6}  W/D/L={r['england_wins']}/{r['england_draws']}/{r['england_losses']}"
            )


if __name__ == "__main__":
    main()
