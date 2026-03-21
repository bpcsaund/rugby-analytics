#!/usr/bin/env python3
"""
RFU match-centre scraper — all tabs.

Scrapes Attack, Defence, Kicking, Breakdown, Set Plays, and Discipline
from each URL in data/raw/rfu_test_urls.txt and outputs:
  - data/raw/rfu_team_stats.csv   (one row per match per team)
  - data/raw/rfu_player_stats.csv (one row per player per match per team)
"""

import asyncio
import csv
import re
import sys
import urllib.parse
from pathlib import Path

from playwright.async_api import Page, async_playwright

# ---------------------------------------------------------------------------
# Paths & browser identity
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent.parent
URLS_FILE = BASE_DIR / "data" / "raw" / "rfu_test_urls.txt"
OUT_DIR = BASE_DIR / "data" / "raw"

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
# Tab names and the CSS slug Opta uses in player table class names
# e.g. .Opta-js-attack-home, .Opta-js-set-plays-home
# ---------------------------------------------------------------------------
TABS = ["Attack", "Defence", "Kicking", "Breakdown", "Set plays", "Discipline"]

TAB_SLUG: dict[str, str] = {
    "Attack":     "attack",
    "Defence":    "defence",
    "Kicking":    "kicking",
    "Breakdown":  "breakdown",
    "Set plays":  "set-plays",
    "Discipline": "discipline",
}

# ---------------------------------------------------------------------------
# Output schema field lists
# ---------------------------------------------------------------------------
TEAM_FIELDS = [
    "url", "match_id", "competition", "season",
    "competition_name", "match_date",
    "team", "home_away",
    "home_score_FT", "away_score_FT", "home_score_HT", "away_score_HT",
    "possession_pct",
    # Attack
    "Tries", "Metres_Gained", "Carries", "Defenders_Beaten",
    "Clean_Breaks", "Passes", "Offloads", "Turnovers_Conceded",
    # Defence
    "Tackles_Attempted", "Missed_Tackles", "Turnovers_Won",
    # Kicking
    "Kicks_In_Play", "Conversions", "Conversion_Accuracy",
    "Penalty_Goals", "Penalty_Goal_Accuracy",
    "Drop_Goals", "Drop_Goal_Accuracy",
    # Breakdown
    "Rucks_Won", "Rucks_Lost", "Rucks_Won_Pct", "Mauls_Won",
    # Set Plays
    "Lineouts_Won", "Lineouts_Lost", "Lineouts_Won_Pct",
    "Scrums_Won", "Scrums_Lost", "Scrums_Won_Pct",
    # Discipline
    "Penalties_Conceded", "Red_Cards", "Yellow_Cards",
]
# Non-stat identifier columns that should remain empty string when absent
TEAM_META_FIELDS = {
    "url", "match_id", "competition", "season",
    "competition_name", "match_date",
    "team", "home_away", "possession_pct",
}

LINEUP_FIELDS = [
    "url", "match_id", "team", "shirt_number", "player_name", "role",
]

PLAYER_FIELDS = [
    "url", "match_id", "competition", "season",
    "team", "home_away", "player",
    # Attack
    "Tries", "Metres_Gained", "Carries", "Defenders_Beaten",
    "Clean_Breaks", "Passes", "Offloads", "Turnovers_Conceded",
    "Try_Assists", "Points_Scored",
    # Defence
    "Tackles_Attempted", "Missed_Tackles", "Turnovers_Won",
    # Kicking
    "Kicks_In_Play", "Conversions", "Penalty_Goals", "Drop_Goals",
    # Set Plays
    "Throws_Won", "Lineouts_Won", "Lineout_Steals",
    # Discipline
    "Penalties_Conceded", "Red_Cards", "Yellow_Cards",
]
PLAYER_META_FIELDS = {"url", "match_id", "competition", "season", "team", "home_away", "player"}

# ---------------------------------------------------------------------------
# Stat label normalisation and label → column mappings
# Keys are lowercase, alphanumeric + spaces only (no punctuation).
# ---------------------------------------------------------------------------
def _norm(label: str) -> str:
    """Normalise a stat label for fuzzy matching.
    Converts '%' → 'pct' so "Rucks won" and "Rucks won %" stay distinct.
    """
    return re.sub(r"[^a-z0-9 ]", "", label.lower().replace("%", "pct")).strip()


TEAM_LABEL_MAP: dict[str, str] = {
    # Possession (may appear as first stat in Attack tab)
    _norm("Possession"):               "possession_pct",
    # Attack
    _norm("Tries"):                    "Tries",
    _norm("Metres gained"):            "Metres_Gained",
    _norm("Carries"):                  "Carries",
    _norm("Defenders beaten"):         "Defenders_Beaten",
    _norm("Clean breaks"):             "Clean_Breaks",
    _norm("Passes"):                   "Passes",
    _norm("Offloads"):                 "Offloads",
    _norm("Turnovers conceded"):       "Turnovers_Conceded",
    # Defence
    _norm("Tackles"):                  "Tackles_Attempted",
    _norm("Tackles attempted"):        "Tackles_Attempted",
    _norm("Missed tackles"):           "Missed_Tackles",
    _norm("Turnovers won"):            "Turnovers_Won",
    # Kicking (Opta uses "Conversion accuracy (%)" with parens — _norm strips them)
    _norm("Kicks in play"):            "Kicks_In_Play",
    _norm("Conversions"):              "Conversions",
    _norm("Conversion accuracy"):      "Conversion_Accuracy",
    _norm("Conversion accuracy (%)"):  "Conversion_Accuracy",
    _norm("Penalty goals"):            "Penalty_Goals",
    _norm("Penalty goal accuracy"):    "Penalty_Goal_Accuracy",
    _norm("Penalty goal accuracy (%)"): "Penalty_Goal_Accuracy",
    _norm("Drop goals"):               "Drop_Goals",
    _norm("Drop goal accuracy"):       "Drop_Goal_Accuracy",
    # Breakdown — Opta repeats "Rucks won" for count AND percentage.
    # JS_EXTRACT_TAB appends " (%)" when the cell value ends with "%",
    # turning the percentage row into "Rucks won (%)" which maps here.
    _norm("Rucks won"):                "Rucks_Won",
    _norm("Rucks won (%)"):            "Rucks_Won_Pct",
    _norm("Rucks won %"):              "Rucks_Won_Pct",
    _norm("Rucks lost"):               "Rucks_Lost",
    _norm("Mauls won"):                "Mauls_Won",
    # Set Plays
    _norm("Lineouts won"):             "Lineouts_Won",
    _norm("Lineouts lost"):            "Lineouts_Lost",
    _norm("Lineouts won %"):           "Lineouts_Won_Pct",
    _norm("Lineouts won (%)"):         "Lineouts_Won_Pct",
    _norm("Scrums won"):               "Scrums_Won",
    _norm("Scrums lost"):              "Scrums_Lost",
    _norm("Scrums won %"):             "Scrums_Won_Pct",
    _norm("Scrums won (%)"):           "Scrums_Won_Pct",
    # Discipline
    _norm("Penalties conceded"):       "Penalties_Conceded",
    _norm("Penalty count"):            "Penalties_Conceded",
    _norm("Red cards"):                "Red_Cards",
    _norm("Yellow cards"):             "Yellow_Cards",
}

PLAYER_LABEL_MAP: dict[str, str] = {
    # Attack
    _norm("Tries"):                    "Tries",
    _norm("Metres gained"):            "Metres_Gained",
    _norm("Carries"):                  "Carries",
    _norm("Defenders beaten"):         "Defenders_Beaten",
    _norm("Clean breaks"):             "Clean_Breaks",
    _norm("Passes"):                   "Passes",
    _norm("Offloads"):                 "Offloads",
    _norm("Turnovers conceded"):       "Turnovers_Conceded",
    _norm("Try assists"):              "Try_Assists",
    _norm("Points"):                   "Points_Scored",
    _norm("Points scored"):            "Points_Scored",
    # Defence
    _norm("Tackles"):                  "Tackles_Attempted",
    _norm("Tackles attempted"):        "Tackles_Attempted",
    _norm("Missed tackles"):           "Missed_Tackles",
    _norm("Turnovers won"):            "Turnovers_Won",
    # Kicking
    _norm("Kicks in play"):            "Kicks_In_Play",
    _norm("Conversions"):              "Conversions",
    _norm("Penalty goals"):            "Penalty_Goals",
    _norm("Drop goals"):               "Drop_Goals",
    # Set Plays
    _norm("Throws won"):               "Throws_Won",
    _norm("Lineouts won"):             "Lineouts_Won",
    _norm("Lineout steals"):           "Lineout_Steals",
    # Discipline
    _norm("Penalties conceded"):       "Penalties_Conceded",
    _norm("Penalty count"):            "Penalties_Conceded",
    _norm("Red cards"):                "Red_Cards",
    _norm("Yellow cards"):             "Yellow_Cards",
}

# ---------------------------------------------------------------------------
# Init script: injected before every page load.
# Combines stealth (hide headless indicators) with opta-widget capture.
# ---------------------------------------------------------------------------
INIT_SCRIPT = """
// ----- Stealth: hide automation signals detected by AWS WAF -----
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
if (!window.chrome) window.chrome = { runtime: {} };

// ----- opta-widget capture -----
(() => {
    window.__rfuOptaData = null;

    function capture() {
        if (window.__rfuOptaData) return;
        for (const w of document.querySelectorAll('opta-widget')) {
            const match = w.getAttribute('match');
            if (match) {
                window.__rfuOptaData = {
                    match_id:    match,
                    competition: w.getAttribute('competition') || '',
                    season:      w.getAttribute('season')      || '',
                };
                return;
            }
        }
    }

    // Capture at DOMContentLoaded (before Opta's own listener fires)
    document.addEventListener('DOMContentLoaded', capture);

    // Also watch mutations in case elements are inserted after DOMContentLoaded
    new MutationObserver(capture).observe(
        document.documentElement,
        { childList: true, subtree: true }
    );
})();
"""

# ---------------------------------------------------------------------------
# JavaScript: extract possession percentages from the SVG pitch widget.
# Opta renders these as <text class="Opta-Possession-value Opta-Home/Away">
# inside the .c038-possession-stats-container element.
# ---------------------------------------------------------------------------
JS_EXTRACT_POSSESSION = """
() => {
    const c = document.querySelector('.c038-possession-stats-container');
    if (!c) return { home: '', away: '' };
    const homeEl = c.querySelector('.Opta-Possession-value.Opta-Home');
    const awayEl = c.querySelector('.Opta-Possession-value.Opta-Away');
    return {
        home: homeEl ? homeEl.textContent.trim() : '',
        away: awayEl ? awayEl.textContent.trim() : '',
    };
}
"""

# ---------------------------------------------------------------------------
# JavaScript: extract match header metadata — scores, competition, date.
# HT score lives in .Opta-Score-Extras as text like "HT 7-10".
# ---------------------------------------------------------------------------
JS_EXTRACT_HEADER = """
() => {
    const homeScoreEl = document.querySelector('.Opta-Score.Opta-Home .Opta-Team-Score');
    const awayScoreEl = document.querySelector('.Opta-Score.Opta-Away .Opta-Team-Score');

    // Half-time score: text is "HT 7-10" (the <abbr> is stripped by innerText)
    let htHome = '', htAway = '';
    const htEl = document.querySelector('tr.Opta-Score-Extras span');
    if (htEl) {
        const htText = htEl.innerText.replace(/HT\\s*/i, '').trim();
        const parts = htText.split('-');
        if (parts.length === 2) { htHome = parts[0].trim(); htAway = parts[1].trim(); }
    }

    const compEl = document.querySelector('.Opta-Competition');
    const dateEl = document.querySelector('.Opta-Date');

    return {
        home_score_FT:    homeScoreEl ? homeScoreEl.innerText.trim() : '',
        away_score_FT:    awayScoreEl ? awayScoreEl.innerText.trim() : '',
        home_score_HT:    htHome,
        away_score_HT:    htAway,
        competition_name: compEl ? compEl.innerText.trim() : '',
        match_date:       dateEl ? dateEl.innerText.trim() : '',
    };
}
"""

# ---------------------------------------------------------------------------
# JavaScript: extract the matchday-23 lineup for both teams.
# Both old and new URL formats use tbody.Opta-Team inside
# .c085-lineup-table-container.  Shirt numbers 1-15 = starter, 16-23 = replacement.
# ---------------------------------------------------------------------------
JS_EXTRACT_LINEUP = """
() => {
    const container = document.querySelector('.c085-lineup-table-container');
    if (!container) return [];
    const rows = [];
    for (const tbody of container.querySelectorAll('tbody.Opta-Team')) {
        const nameEl = tbody.querySelector('tr.Opta-Name th h3 span');
        const team = nameEl ? nameEl.innerText.trim() : '';
        for (const row of tbody.querySelectorAll('tr.Opta-Player')) {
            const shirtEl = row.querySelector('td.Opta-Shirt');
            const playerEl = row.querySelector('td.Opta-Name a');
            if (!shirtEl || !playerEl) continue;
            const shirtNum = parseInt(shirtEl.innerText.trim(), 10);
            const pidMatch = row.className.match(/Opta-Player-([0-9]+)/);
            const playerId = pidMatch ? pidMatch[1] : '';
            rows.push({
                team,
                shirt_number: shirtNum,
                player_name:  playerEl.innerText.trim(),
                player_id:    playerId,
                role:         shirtNum <= 15 ? 'starter' : 'replacement',
            });
        }
    }
    return rows;
}
"""

# ---------------------------------------------------------------------------
# JavaScript: extract team names from the rendered page
# ---------------------------------------------------------------------------
JS_EXTRACT_META = """
() => {
    // Attack tab player tables are the most reliable source for team names
    const homeEl = document.querySelector('.Opta-js-attack-home th.Opta-Team') ||
                   document.querySelector('.Opta-Home .Opta-Team-Name') ||
                   document.querySelector('.Opta-Home .Opta-Team');
    const awayEl = document.querySelector('.Opta-js-attack-away th.Opta-Team') ||
                   document.querySelector('.Opta-Away .Opta-Team-Name') ||
                   document.querySelector('.Opta-Away .Opta-Team');
    return {
        homeName: homeEl ? homeEl.innerText.trim() : null,
        awayName: awayEl ? awayEl.innerText.trim() : null,
    };
}
"""

# ---------------------------------------------------------------------------
# JavaScript: extract team-bar stats + player stats for the active tab.
# Called with (tabSlug) as the Playwright evaluate argument.
# ---------------------------------------------------------------------------
JS_EXTRACT_TAB = """
(tabSlug) => {
    // ---- Team stats -------------------------------------------------------
    // The active tab's <li class="Opta-On"> in the team_graphs widget holds
    // the stats-bars table.
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
                if (outer.length) {
                    const homeVal = outer[0] ? outer[0].innerText.trim() : null;
                    const awayVal = outer[1] ? outer[1].innerText.trim() : null;
                    // When both values are percentages and the label doesn't
                    // already say so, tag the label so the mapping can
                    // distinguish count rows from percentage rows (Opta reuses
                    // the same label, e.g. "Rucks won", for both).
                    const isPct = (homeVal || '').endsWith('%') || (awayVal || '').endsWith('%');
                    const alreadyTagged = currentStat.includes('(%)') || currentStat.endsWith('%');
                    const statLabel = (isPct && !alreadyTagged)
                        ? currentStat + ' (%)'
                        : currentStat;
                    teamStats.push({ stat: statLabel, home: homeVal, away: awayVal });
                }
                currentStat = null;
            }
        }
    }

    // ---- Player stats -----------------------------------------------------
    // Opta uses class names like .Opta-js-attack-home, .Opta-js-set-plays-home.
    // Try slug variants to handle hyphen/underscore/no-separator differences.
    const slugVariants = [
        tabSlug,
        tabSlug.replace(/-/g, '_'),
        tabSlug.replace(/-/g, ''),
    ];

    const playerStats = { home: [], away: [] };
    for (const side of ['home', 'away']) {
        let div = null;
        for (const slug of slugVariants) {
            div = document.querySelector(`.Opta-js-${slug}-${side}`);
            if (div) break;
        }
        if (!div) continue;

        const headers = Array.from(div.querySelectorAll('thead th abbr'))
            .map(a => a.getAttribute('title'));

        for (const row of div.querySelectorAll('tbody tr')) {
            const nameEl = row.querySelector('th.Opta-Player');
            if (!nameEl) continue;
            const cells = Array.from(row.querySelectorAll('td'));
            const stats = {};
            cells.forEach((td, i) => {
                if (headers[i]) {
                    const val = td.getAttribute('data-srt');
                    stats[headers[i]] = (val !== null) ? val : td.innerText.trim();
                }
            });
            const pidMatch = nameEl.className.match(/Opta-Player-([0-9]+)/);
            const playerId = pidMatch ? pidMatch[1] : '';
            playerStats[side].push({ name: nameEl.innerText.trim(), player_id: playerId, stats });
        }
    }

    return { teamStats, playerStats };
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


async def click_tab(page: Page, tab_name: str) -> None:
    """Click the named tab in every Opta nav widget that contains it.

    Uses JS-level click() to bypass the visibility/stable checks that
    ElementHandle.click() enforces — Opta nav links are sometimes
    clipped inside widget containers.
    """
    try:
        await page.evaluate(
            """(name) => {
                const links = Array.from(document.querySelectorAll('.Opta-Nav li a'));
                for (const link of links) {
                    if (link.innerText.trim() === name) link.click();
                }
            }""",
            tab_name,
        )
        await page.wait_for_timeout(600)
    except Exception:
        pass


async def scrape_url(page: Page, url: str) -> dict | None:
    clean_url = url.split("#")[0].strip()
    print(f"  → {clean_url}")

    try:
        await page.goto(clean_url, wait_until="load", timeout=30000)
    except Exception as exc:
        print(f"    ERROR loading: {exc}")
        return None

    await accept_cookies(page)
    await page.wait_for_timeout(8000)  # wait for Opta async render

    # Retrieve the opta-widget attributes captured by INIT_SCRIPT before
    # Opta's JS removed the elements from the DOM.
    widget_data = await page.evaluate("() => window.__rfuOptaData || null")
    match_id   = (widget_data or {}).get("match_id", "")
    competition = (widget_data or {}).get("competition", "")
    season      = (widget_data or {}).get("season", "")

    if not match_id:
        # Fallback for match-centre-elite?matchId=…&competitionId=…&seasonId=…
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(clean_url).query)
        match_id    = (qs.get("matchId")      or [""])[0]
        competition = (qs.get("competitionId") or [""])[0]
        season      = (qs.get("seasonId")      or [""])[0]

    if not match_id:
        print("    WARNING: could not determine match ID — skipping")
        return None

    # Get team names (Attack tab is the default-active tab)
    meta = await page.evaluate(JS_EXTRACT_META)
    home_name = meta.get("homeName") or ""
    away_name = meta.get("awayName") or ""

    # Get possession percentages from the SVG pitch widget
    possession = await page.evaluate(JS_EXTRACT_POSSESSION)
    possession_home = possession.get("home", "")
    possession_away = possession.get("away", "")

    # Get match header: scores, competition name, date
    header = await page.evaluate(JS_EXTRACT_HEADER)

    # Get matchday-23 lineup (widget already rendered, no tab click needed)
    lineup = await page.evaluate(JS_EXTRACT_LINEUP)

    # Iterate every tab and extract stats
    tab_data: dict[str, dict] = {}
    for tab in TABS:
        await click_tab(page, tab)
        await page.wait_for_timeout(800)
        try:
            data = await page.evaluate(JS_EXTRACT_TAB, TAB_SLUG[tab])
        except Exception as exc:
            print(f"    WARNING: error on tab '{tab}': {exc}")
            data = {"teamStats": [], "playerStats": {"home": [], "away": []}}
        tab_data[tab] = data

    n_team = sum(len(v["teamStats"]) for v in tab_data.values())
    n_home = len(tab_data.get("Attack", {}).get("playerStats", {}).get("home", []))
    n_away = len(tab_data.get("Attack", {}).get("playerStats", {}).get("away", []))
    print(
        f"    {home_name} vs {away_name}  |  match {match_id}  |  "
        f"{n_team} team stats  |  {n_home}+{n_away} players"
    )

    return {
        "url": url.strip(),
        "match_id": match_id,
        "competition": competition,
        "season": season,
        "home_team": home_name,
        "away_team": away_name,
        "possession_home": possession_home,
        "possession_away": possession_away,
        "header": header,
        "lineup": lineup,
        "tab_data": tab_data,
    }


# ---------------------------------------------------------------------------
# Helpers to merge per-tab raw data into flat schema dicts
# ---------------------------------------------------------------------------
def _team_stat_dict(tab_data: dict[str, dict], side: str) -> dict[str, str]:
    """Merge all-tab team stats for one side into a schema-keyed flat dict."""
    result: dict[str, str] = {}
    for data in tab_data.values():
        for entry in data.get("teamStats", []):
            col = TEAM_LABEL_MAP.get(_norm(entry.get("stat", "")))
            if col and col not in result:
                val = entry.get(side) or ""
                result[col] = val
    return result


def _player_stat_dicts(tab_data: dict[str, dict], side: str) -> list[dict]:
    """Merge all-tab player stats for one side into per-player flat dicts."""
    players: dict[str, dict[str, str]] = {}
    for data in tab_data.values():
        for p in data.get("playerStats", {}).get(side, []):
            name = p["name"]
            if name not in players:
                players[name] = {}
            for raw_label, value in p["stats"].items():
                col = PLAYER_LABEL_MAP.get(_norm(raw_label))
                if col and col not in players[name]:
                    players[name][col] = value or ""
    return [{"player": name, **stats} for name, stats in players.items()]


# ---------------------------------------------------------------------------
# CSV writers
# ---------------------------------------------------------------------------
def write_team_stats(results: list[dict], path: Path, append: bool = False) -> None:
    mode = "a" if append else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TEAM_FIELDS, extrasaction="ignore")
        if not append:
            writer.writeheader()
        for r in results:
            for side in ("home", "away"):
                team_name = r["home_team"] if side == "home" else r["away_team"]
                stats = _team_stat_dict(r["tab_data"], side)
                poss = r.get("possession_home") if side == "home" else r.get("possession_away")
                row: dict = {f: ("" if f in TEAM_META_FIELDS else "0") for f in TEAM_FIELDS}
                h = r.get("header", {})
                row.update({
                    "url":              r["url"],
                    "match_id":         r["match_id"],
                    "competition":      r["competition"],
                    "season":           r["season"],
                    "competition_name": h.get("competition_name", ""),
                    "match_date":       h.get("match_date", ""),
                    "team":             team_name,
                    "home_away":        side,
                    "home_score_FT":    h.get("home_score_FT", "0") or "0",
                    "away_score_FT":    h.get("away_score_FT", "0") or "0",
                    "home_score_HT":    h.get("home_score_HT", "0") or "0",
                    "away_score_HT":    h.get("away_score_HT", "0") or "0",
                    "possession_pct":   poss or stats.pop("possession_pct", ""),
                })
                row.update({k: v for k, v in stats.items() if v != ""})
                writer.writerow(row)


def write_player_stats(results: list[dict], path: Path, append: bool = False) -> None:
    mode = "a" if append else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=PLAYER_FIELDS, extrasaction="ignore")
        if not append:
            writer.writeheader()
        for r in results:
            for side in ("home", "away"):
                team_name = r["home_team"] if side == "home" else r["away_team"]
                for p in _player_stat_dicts(r["tab_data"], side):
                    row: dict = {f: ("" if f in PLAYER_META_FIELDS else "0") for f in PLAYER_FIELDS}
                    row.update({
                        "url":         r["url"],
                        "match_id":    r["match_id"],
                        "competition": r["competition"],
                        "season":      r["season"],
                        "team":        team_name,
                        "home_away":   side,
                    })
                    row.update({k: v for k, v in p.items() if v != ""})
                    writer.writerow(row)


def _player_id_name_map(tab_data: dict[str, dict], side: str) -> dict[str, str]:
    """Build {player_id: name} from all tab player stats for one side.
    Uses the first non-empty name seen for each player across tabs.
    Matching by Opta player ID (from the Opta-Player-{id} CSS class) is more
    reliable than matching by position — the stats tables are sorted by stat
    contribution, not shirt number.
    """
    mapping: dict[str, str] = {}
    for data in tab_data.values():
        for p in data.get("playerStats", {}).get(side, []):
            pid = p.get("player_id", "")
            if pid and pid not in mapping and p.get("name"):
                mapping[pid] = p["name"]
    return mapping


def write_lineup(results: list[dict], path: Path, append: bool = False) -> None:
    mode = "a" if append else "w"
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=LINEUP_FIELDS, extrasaction="ignore")
        if not append:
            writer.writeheader()
        for r in results:
            home_id_map = _player_id_name_map(r["tab_data"], "home")
            away_id_map = _player_id_name_map(r["tab_data"], "away")
            home_team = r["home_team"]
            for entry in r.get("lineup", []):
                pid = entry.get("player_id", "")
                id_map = home_id_map if entry["team"] == home_team else away_id_map
                # Prefer whichever name is longer — lineup widget has first+last for
                # older matches; stats tabs have first+last for newer ones.
                # Fall back to lineup name for unused substitutes (not in any stats tab).
                stats_name = id_map.get(pid) or ""
                resolved_name = max(stats_name, entry["player_name"], key=len) or entry["player_name"]
                writer.writerow({
                    "url":          r["url"],
                    "match_id":     r["match_id"],
                    "team":         entry["team"],
                    "shirt_number": entry["shirt_number"],
                    "player_name":  resolved_name,
                    "role":         entry["role"],
                })


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def main() -> None:
    args = sys.argv[1:]
    append = "--append" in args
    args = [a for a in args if a != "--append"]
    url_file = Path(args[0]) if args else URLS_FILE
    urls = [
        line.strip()
        for line in url_file.read_text().splitlines()
        if line.strip()
    ]
    print(f"Scraping {len(urls)} URLs\n")

    results: list[dict] = []

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome",
            headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        context = await browser.new_context(
            user_agent=UA,
            extra_http_headers=EXTRA_HEADERS,
            service_workers="block",
        )
        # Register the init script once — it runs before every page navigation.
        await context.add_init_script(INIT_SCRIPT)
        page = await context.new_page()

        for i, url in enumerate(urls, 1):
            print(f"[{i}/{len(urls)}]")
            result = await scrape_url(page, url)
            if result:
                results.append(result)
            if i < len(urls):
                await page.wait_for_timeout(2000)

        await browser.close()

    if not results:
        print("\nNo data collected — check URL accessibility.")
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    team_csv   = OUT_DIR / "rfu_team_stats.csv"
    player_csv = OUT_DIR / "rfu_player_stats.csv"
    lineup_csv = OUT_DIR / "rfu_lineups.csv"

    write_team_stats(results, team_csv, append=append)
    write_player_stats(results, player_csv, append=append)
    write_lineup(results, lineup_csv, append=append)

    print(f"\n✓ {len(results)}/{len(urls)} matches scraped")
    print(f"  {team_csv}")
    print(f"  {player_csv}")
    print(f"  {lineup_csv}")


if __name__ == "__main__":
    asyncio.run(main())
