#!/usr/bin/env python3
"""
Second-pass scraper: retries rows in england_player_profiles.csv that have
error status (403 / 202 WAF blocks) using Playwright, which can execute the
AWS WAF JavaScript challenge that urllib cannot.

Modifies england_player_profiles.csv in-place:
  - error rows that now return 200 → updated to 'ok' with full stats
  - error rows that now return 404 → updated to 'not_found'
  - error rows that still fail → status updated to reflect new code

Also corrects the suspect Bevan Rodd height entry (18 → blank).

Usage:
  python3 ingestion/rfu_profile_scraper_pass2.py
"""

import csv
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE_DIR = Path(__file__).parent.parent
PROFILES_CSV = BASE_DIR / "data" / "raw" / "england_player_profiles.csv"

PROFILE_BASE = "https://www.englandrugby.com/follow/england-men/senior-men/{slug}"

FIELDS = [
    "player_name", "slug", "url",
    "position", "caps",
    "age", "height_cm", "weight_kg",
    "career_mins", "avg_mins_per_game",
    "england_wins", "england_draws", "england_losses",
    "status",
]

DELAY_SECONDS = 10


# ---------------------------------------------------------------------------
# HTML parsing — identical to rfu_profile_scraper.py
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
# Main
# ---------------------------------------------------------------------------
def main() -> None:
    # Read CSV
    with open(PROFILES_CSV, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))

    error_indices = [i for i, r in enumerate(rows) if r["status"].startswith("error")]
    print(f"Total rows        : {len(rows)}")
    print(f"Error rows to retry: {len(error_indices)}")
    print()

    successes = 0
    not_found = 0
    still_errors = 0

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            locale="en-GB",
        )
        page = context.new_page()

        for n, idx in enumerate(error_indices, 1):
            row = rows[idx]
            slug = row["slug"]
            url = PROFILE_BASE.format(slug=slug)

            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
                status = response.status if response else 0

                # Wait for WAF JS challenge to complete if served a challenge page
                if status == 202 or (response and "challenge" in (response.url or "").lower()):
                    page.wait_for_load_state("networkidle", timeout=15000)
                    status = 200  # if we get here, challenge passed

                if status == 200:
                    # Give the page a moment to fully render server-side content
                    page.wait_for_timeout(1000)
                    html = page.content()

                    # Confirm it's actually a player page (not a redirect / error page)
                    if "tc03_player_details" not in html and "tc02-player-stats" not in html:
                        # Landed on a 404/redirect page that returned HTTP 200
                        row.update({f: "" for f in FIELDS if f not in ("player_name", "slug", "url")})
                        row["status"] = "not_found"
                        not_found += 1
                        print(f"  [{n}/{len(error_indices)}] 404 (soft) {slug}")
                    else:
                        parsed = parse_profile(html)
                        row.update(parsed)
                        row["status"] = "ok"
                        successes += 1
                        print(
                            f"  [{n}/{len(error_indices)}] ✓  {slug:42s}"
                            f"  caps={row['caps'] or '—':<4}  pos={row['position'] or '—':<14}"
                            f"  ht={row['height_cm'] or '—':<5}  wt={row['weight_kg'] or '—'}"
                        )
                elif status == 404:
                    row.update({f: "" for f in FIELDS if f not in ("player_name", "slug", "url")})
                    row["status"] = "not_found"
                    not_found += 1
                    print(f"  [{n}/{len(error_indices)}] 404 {slug}")
                else:
                    row["status"] = f"error_{status}"
                    still_errors += 1
                    print(f"  [{n}/{len(error_indices)}] ERR {slug}  (HTTP {status})")

            except Exception as e:
                row["status"] = "error_exception"
                still_errors += 1
                print(f"  [{n}/{len(error_indices)}] EXC {slug}  ({e})")

            if n < len(error_indices):
                time.sleep(DELAY_SECONDS)

        browser.close()

    # Fix suspect Bevan Rodd height (18 is clearly wrong — should be 180-something)
    for row in rows:
        if row["slug"] == "bevan-rodd" and row.get("height_cm") == "18":
            row["height_cm"] = ""
            print("\nFixed: bevan-rodd height_cm 18 → '' (suspect entry)")

    # Write updated CSV
    with open(PROFILES_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        w.writerows(rows)

    total_ok = sum(1 for r in rows if r["status"] == "ok")
    print(f"\n{'='*60}")
    print(f"Second-pass results : +{successes} ok, +{not_found} not_found, {still_errors} still error")
    print(f"Total ok rows now   : {total_ok}")
    print(f"Output              : {PROFILES_CSV}")

    ok_new = [r for r in rows if r["status"] == "ok" and r["slug"] in
              [rows[i]["slug"] for i in error_indices]]
    if ok_new:
        print(f"\nNewly resolved rows:")
        for r in ok_new:
            print(
                f"  {r['player_name']:<28s}  caps={r['caps']:<4}  "
                f"pos={r['position']:<14}  ht={r['height_cm']:<5}  wt={r['weight_kg']:<5}  "
                f"mins={r['career_mins']:<6}  W/D/L={r['england_wins']}/{r['england_draws']}/{r['england_losses']}"
            )


if __name__ == "__main__":
    main()
