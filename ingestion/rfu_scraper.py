#!/usr/bin/env python3
"""
RFU match-centre scraper.

Loads each URL in data/raw/rfu_test_urls.txt, ensures the Attack tab is
active on the Opta match-stats widgets, then extracts:
  - Team-level attack stats  → data/raw/rfu_team_attack_stats.csv
  - Player-level attack stats → data/raw/rfu_player_attack_stats.csv
"""

import asyncio
import csv
import re
from pathlib import Path

from playwright.async_api import Page, async_playwright

# Regex to pull attrs from the first team_graphs opta-widget in raw HTML
_TEAM_GRAPHS_RE = re.compile(
    r"<opta-widget\b[^>]*\btemplate=[\"']?team_graphs[\"']?[^>]*>",
    re.I,
)
_ATTR_RE = re.compile(r'(\w+)=["\']?([^"\'>\s]+)["\']?')

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
URLS_FILE = BASE_DIR / "data" / "raw" / "rfu_test_urls.txt"
OUT_DIR = BASE_DIR / "data" / "raw"

# ---------------------------------------------------------------------------
# Browser identity – without this the site returns 403
# ---------------------------------------------------------------------------
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
EXTRA_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.5",
}

# ---------------------------------------------------------------------------
# Stat field names (full names as rendered by Opta)
# ---------------------------------------------------------------------------
TEAM_STAT_FIELDS = [
    "Tries",
    "Metres gained",
    "Carries",
    "Defenders beaten",
    "Clean breaks",
    "Passes",
    "Offloads",
    "Turnovers conceded",
]

PLAYER_STAT_ABBR = {
    "Tries": "Tries",
    "Metres gained": "Metres",
    "Carries": "Carries",
    "Defenders beaten": "Defenders_Beaten",
    "Clean breaks": "Clean_Breaks",
    "Passes": "Passes",
    "Offloads": "Offloads",
    "Turnovers conceded": "Turnovers_Conceded",
    "Try assists": "Turnovers_Won",
    "Points": "Points",
}

# ---------------------------------------------------------------------------
# JavaScript executed inside the browser to pull all data in one call
# ---------------------------------------------------------------------------
JS_EXTRACT = """
() => {
    // --- Team names ---------------------------------------------------
    // Taken from the player-attack table headers (most reliable source)
    const homeTeamEl = document.querySelector('.Opta-js-attack-home th.Opta-Team');
    const awayTeamEl = document.querySelector('.Opta-js-attack-away th.Opta-Team');
    const homeName = homeTeamEl ? homeTeamEl.innerText.trim() : null;
    const awayName = awayTeamEl ? awayTeamEl.innerText.trim() : null;

    // --- Team-level stats (Attack tab of the team_graphs widget) ------
    // The active tab li contains the stats-bars table.
    const statLi = Array.from(document.querySelectorAll('li.Opta-On'))
        .find(li => li.querySelector('table.Opta-Stats-Bars'));
    const statsTable = statLi ? statLi.querySelector('table.Opta-Stats-Bars') : null;

    const teamStats = [];
    if (statsTable) {
        const rows = Array.from(statsTable.querySelectorAll('tbody tr'));
        let currentStat = null;
        for (const row of rows) {
            const th = row.querySelector('th.Opta-Stats-Bars-Text');
            if (th) {
                currentStat = th.innerText.trim();
            } else if (currentStat) {
                const outer = row.querySelectorAll('td.Opta-Outer');
                teamStats.push({
                    stat:  currentStat,
                    home:  outer[0] ? outer[0].innerText.trim() : null,
                    away:  outer[1] ? outer[1].innerText.trim() : null,
                });
                currentStat = null;
            }
        }
    }

    // --- Player-level stats (Attack tab of the teamsheet widget) ------
    const playerStats = { home: [], away: [] };
    for (const side of ['home', 'away']) {
        const selector = side === 'home'
            ? '.Opta-js-attack-home'
            : '.Opta-js-attack-away';
        const div = document.querySelector(selector);
        if (!div) continue;

        const headers = Array.from(div.querySelectorAll('thead th abbr'))
            .map(a => a.getAttribute('title'));

        for (const row of div.querySelectorAll('tbody tr')) {
            const nameEl = row.querySelector('th.Opta-Player');
            if (!nameEl) continue;
            const cells = Array.from(row.querySelectorAll('td'));
            const stats = {};
            cells.forEach((td, i) => {
                if (headers[i]) stats[headers[i]] = td.getAttribute('data-srt');
            });
            playerStats[side].push({ name: nameEl.innerText.trim(), stats });
        }
    }

    return { homeName, awayName, teamStats, playerStats };
}
"""


# ---------------------------------------------------------------------------
# Scraping helpers
# ---------------------------------------------------------------------------
async def accept_cookies(page: Page) -> None:
    try:
        await page.click("button#didomi-notice-agree-button", timeout=5000)
    except Exception:
        pass


async def click_attack_tabs(page: Page) -> None:
    """Click the Attack tab in every Opta stats widget that has one."""
    try:
        # Each tab nav is a <ul class="Opta-Cf ..."><li><a>Attack</a></li>...
        # Attack is already default-active, but we click to be explicit.
        links = await page.query_selector_all(".Opta-Nav li a")
        for link in links:
            text = (await link.inner_text()).strip()
            if text == "Attack":
                await link.click()
                await page.wait_for_timeout(500)
    except Exception:
        pass


async def scrape_url(page: Page, url: str) -> dict | None:
    clean_url = url.split("#")[0].strip()
    print(f"  → {clean_url}")

    # Capture the HTML response object synchronously (body read afterward).
    # Opta's JS removes <opta-widget> elements from the live DOM once it
    # renders them, so we must read the raw source to get match metadata.
    html_response: list = []

    def on_response(response):
        if (
            response.status == 200
            and "text/html" in response.headers.get("content-type", "")
            and "englandrugby.com" in response.url
            and "match-centre" in response.url
        ):
            html_response.append(response)

    page.on("response", on_response)

    try:
        await page.goto(clean_url, wait_until="load", timeout=30000)
    except Exception as exc:
        print(f"    ERROR loading: {exc}")
        return None
    finally:
        page.remove_listener("response", on_response)

    await accept_cookies(page)

    # Wait for Opta widgets to initialise (they fire XHRs and render async)
    await page.wait_for_timeout(8000)

    await click_attack_tabs(page)
    await page.wait_for_timeout(1000)

    # Parse match metadata from the raw HTML
    competition = season = match_id = ""
    for resp in html_response:
        try:
            body = await resp.text()
        except Exception:
            continue
        m = _TEAM_GRAPHS_RE.search(body)
        if m:
            attrs = dict(_ATTR_RE.findall(m.group()))
            competition = attrs.get("competition", "")
            season = attrs.get("season", "")
            match_id = attrs.get("match", "")
            break

    if not match_id:
        print("    WARNING: could not determine match ID — skipping")
        return None

    try:
        data = await page.evaluate(JS_EXTRACT)
    except Exception as exc:
        print(f"    ERROR extracting data: {exc}")
        return None

    result = {
        "url": url.strip(),
        "match_id": match_id,
        "competition": competition,
        "season": season,
        "home_team": data.get("homeName") or "",
        "away_team": data.get("awayName") or "",
        "team_stats": data.get("teamStats", []),
        "player_stats": data.get("playerStats", {"home": [], "away": []}),
    }

    n_home = len(result["player_stats"]["home"])
    n_away = len(result["player_stats"]["away"])
    n_team = len(result["team_stats"])
    print(
        f"    {result['home_team']} vs {result['away_team']}  |  "
        f"match {match_id}  |  "
        f"{n_team} team stats  |  {n_home}+{n_away} players"
    )
    return result


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------
def write_team_stats(results: list[dict], path: Path) -> None:
    """One row per match, stats in wide format."""
    fieldnames = ["url", "match_id", "competition", "season", "home_team", "away_team"]
    for field in TEAM_STAT_FIELDS:
        slug = field.lower().replace(" ", "_")
        fieldnames += [f"{slug}_home", f"{slug}_away"]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            row: dict = {
                "url": r["url"],
                "match_id": r["match_id"],
                "competition": r["competition"],
                "season": r["season"],
                "home_team": r["home_team"],
                "away_team": r["away_team"],
            }
            stat_map = {s["stat"]: s for s in r["team_stats"]}
            for field in TEAM_STAT_FIELDS:
                slug = field.lower().replace(" ", "_")
                s = stat_map.get(field, {})
                row[f"{slug}_home"] = s.get("home", "")
                row[f"{slug}_away"] = s.get("away", "")
            writer.writerow(row)


def write_player_stats(results: list[dict], path: Path) -> None:
    """One row per player per match."""
    abbrs = list(PLAYER_STAT_ABBR.values())
    fieldnames = [
        "url", "match_id", "competition", "season",
        "team", "home_away", "player",
        *abbrs,
    ]

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            for side in ("home", "away"):
                team_name = r["home_team"] if side == "home" else r["away_team"]
                for player in r["player_stats"].get(side, []):
                    row: dict = {
                        "url": r["url"],
                        "match_id": r["match_id"],
                        "competition": r["competition"],
                        "season": r["season"],
                        "team": team_name,
                        "home_away": side,
                        "player": player["name"],
                    }
                    for full_name, abbr in PLAYER_STAT_ABBR.items():
                        val = player["stats"].get(full_name, "")
                        row[abbr] = val if val is not None else ""
                    writer.writerow(row)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    urls = [
        line.strip()
        for line in URLS_FILE.read_text().splitlines()
        if line.strip()
    ]
    print(f"Scraping {len(urls)} URLs\n")

    results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(channel="chrome", headless=True)
        context = await browser.new_context(
            user_agent=UA,
            extra_http_headers=EXTRA_HEADERS,
        )
        page = await context.new_page()

        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}]")
            result = await scrape_url(page, url)
            if result:
                results.append(result)
            # Polite delay between requests
            if i < len(urls):
                await page.wait_for_timeout(2000)

        await browser.close()

    if not results:
        print("\nNo data collected — check URL accessibility.")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    team_csv = OUT_DIR / "rfu_team_attack_stats.csv"
    player_csv = OUT_DIR / "rfu_player_attack_stats.csv"

    write_team_stats(results, team_csv)
    write_player_stats(results, player_csv)

    print(f"\n✓ {len(results)}/{len(urls)} matches scraped")
    print(f"  {team_csv}")
    print(f"  {player_csv}")


if __name__ == "__main__":
    asyncio.run(main())
