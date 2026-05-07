#!/usr/bin/env python3
"""
Build data/rugby_analytics.db from all raw CSV files.

Population order:
  1. competitions
  2. players  (incrowdsports profiles + lineups; creates rfu_ stubs for unmatched RFU names)
  3. teams
  4. matches  (incrowdsports + RFU)
  5. player_match_stats  (incrowdsports + RFU, fuzzy-matched)
  6. team_match_stats    (incrowdsports + RFU)
  7. player_international_profiles  (england_player_profiles.csv)

Usage:
  python3 ingestion/build_db.py
"""

import csv
import difflib
import re
import sqlite3
import unicodedata
from collections import defaultdict
from pathlib import Path

BASE_DIR = Path(__file__).parent.parent
RAW_DIR  = BASE_DIR / "data" / "raw"
DB_PATH  = BASE_DIR / "data" / "rugby_analytics.db"

INC_COMPS = ["prem", "premcup", "top14", "champs", "challenge", "urc", "japan"]

COMP_META = {
    "prem":        ("Gallagher Premiership",     "England",       1),
    "premcup":     ("Premiership Rugby Cup",     "England",       2),
    "top14":       ("Top 14",                    "France",        1),
    "champs":      ("Investec Champions Cup",    "Europe",        1),
    "challenge":   ("European Challenge Cup",    "Europe",        2),
    "urc":         ("United Rugby Championship", "Europe",        1),
    "japan":       ("Japan League One",          "Japan",         1),
    "rfu_6nations":("Six Nations",               "International", 1),
    "rfu_rwc":     ("Rugby World Cup",           "International", 1),
    "rfu_tour":    ("Tour Match",                "International", 1),
    "rfu_intl":    ("International",             "International", 1),
    "rfu_nations": ("Nations Championship",      "International", 1),
    "rfu_other":   ("Other International",       "International", 1),
}

RFU_COMP_ID = {
    "1":   "rfu_tour",
    "3":   "rfu_intl",
    "209": "rfu_6nations",
    "210": "rfu_rwc",
    "114": "rfu_nations",
    "365": "rfu_other",
}

# Domestic competition for each team (slug → competition_id)
TEAM_PRIMARY_COMP = {
    # Prem / PremCup
    "bath_rugby": "prem", "bristol_bears": "prem", "exeter_chiefs": "prem",
    "gloucester_rugby": "prem", "harlequins": "prem", "leicester_tigers": "prem",
    "london_irish": "prem", "newcastle_falcons": "prem", "northampton_saints": "prem",
    "sale_sharks": "prem", "saracens": "prem", "wasps": "prem",
    "worcester_warriors": "prem",
    "ampthill": "premcup", "bedford_blues": "premcup", "caldy": "premcup",
    "cornish_pirates": "premcup", "coventry": "premcup",
    "doncaster_knights": "premcup", "ealing_trailfinders": "premcup",
    "hartpury": "premcup", "london_scottish": "premcup",
    "nottingham_rugby": "premcup",
    "newcastle_red_bulls": "premcup",
    # URC
    "leinster_rugby": "urc", "munster_rugby": "urc",
    "connacht_rugby": "urc", "ulster_rugby": "urc",
    "glasgow_warriors": "urc", "edinburgh_rugby": "urc",
    "cardiff_rugby": "urc", "dragons_rfc": "urc",
    "ospreys": "urc", "scarlets": "urc",
    "dhl_stormers": "urc", "vodacom_bulls": "urc",
    "hollywoodbets_sharks": "urc", "fidelity_securedrive_lions": "urc",
    "lions": "urc", "emirates_lions": "urc",
    "benetton_rugby": "urc", "zebre_parma": "urc",
    # Top 14
    "toulouse": "top14", "bordeauxbegles": "top14",
    "la_rochelle": "top14", "stade_francais_paris": "top14",
    "racing_92": "top14", "toulon": "top14",
    "clermont_auvergne": "top14", "montpellier": "top14",
    "castres_olympique": "top14", "bayonne": "top14",
    "pau": "top14", "perpignan": "top14",
    "lyon_ou": "top14", "montauban": "top14",
    # Challenge Cup extras
    "black_lion": "challenge", "toyota_cheetahs": "challenge",
    "vannes": "challenge",
    # Japan
    "saitama_wild_knights": "japan", "kubota_spears": "japan",
    "toshiba_brave_lupus": "japan", "toyota_verblitz": "japan",
    "kobe_kobelco_steelers": "japan", "tokyo_sungoliath": "japan",
    "yokohama_canon_eagles": "japan", "black_rams_tokyo": "japan",
    "honda_heat": "japan", "mitsubishi_dynaboars": "japan",
    "shizuoka_blue_revs": "japan", "urayasu_dracks": "japan",
}

TEAM_COUNTRY = {
    "bath_rugby": "England", "bristol_bears": "England", "exeter_chiefs": "England",
    "gloucester_rugby": "England", "harlequins": "England", "leicester_tigers": "England",
    "london_irish": "England", "newcastle_falcons": "England", "northampton_saints": "England",
    "sale_sharks": "England", "saracens": "England", "wasps": "England",
    "worcester_warriors": "England", "ampthill": "England", "bedford_blues": "England",
    "caldy": "England", "cornish_pirates": "England", "coventry": "England",
    "doncaster_knights": "England", "ealing_trailfinders": "England",
    "hartpury": "England", "london_scottish": "England", "nottingham_rugby": "England",
    "newcastle_red_bulls": "England",
    "leinster_rugby": "Ireland", "munster_rugby": "Ireland",
    "connacht_rugby": "Ireland", "ulster_rugby": "Ireland",
    "glasgow_warriors": "Scotland", "edinburgh_rugby": "Scotland",
    "cardiff_rugby": "Wales", "dragons_rfc": "Wales", "ospreys": "Wales", "scarlets": "Wales",
    "dhl_stormers": "South Africa", "vodacom_bulls": "South Africa",
    "hollywoodbets_sharks": "South Africa", "fidelity_securedrive_lions": "South Africa",
    "lions": "South Africa", "emirates_lions": "South Africa",
    "benetton_rugby": "Italy", "zebre_parma": "Italy",
    "black_lion": "Georgia", "toyota_cheetahs": "South Africa",
}

COMP_COUNTRY = {
    "prem": "England", "premcup": "England",
    "top14": "France",
    "champs": "Europe", "challenge": "Europe",
    "urc": "Europe",
    "japan": "Japan",
}


# ─── Helpers ────────────────────────────────────────────────────────────────

def normalize_name(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9 ]", "", s.lower()).strip()


def normalize_compact(s: str) -> str:
    """Strips ALL spaces/hyphens — catches 'CunninghamSouth' vs 'Cunningham South'."""
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]", "", s.lower())


def team_slug(name: str) -> str:
    name = unicodedata.normalize("NFKD", name)
    name = "".join(c for c in name if not unicodedata.combining(c))
    name = name.lower()
    name = re.sub(r"[^a-z0-9\s]", "", name)
    return re.sub(r"\s+", "_", name.strip())


def safe_int(v) -> "int | None":
    if v is None or str(v).strip() in ("", "-", "N/A", "n/a"):
        return None
    try:
        return int(float(str(v).strip()))
    except (ValueError, TypeError):
        return None


def safe_float(v) -> "float | None":
    if v is None or str(v).strip() in ("", "-", "N/A", "n/a"):
        return None
    try:
        return float(str(v).strip().rstrip("%"))
    except (ValueError, TypeError):
        return None


def parse_rfu_date(raw: str) -> str:
    """'Saturday 14 March 2026' → '2026-03-14'. Returns raw on failure."""
    import datetime
    s = raw.strip()
    parts = s.split(" ", 1)
    days = {"Monday","Tuesday","Wednesday","Thursday","Friday","Saturday","Sunday"}
    if len(parts) == 2 and parts[0] in days:
        s = parts[1]
    for fmt in ("%d %B %Y", "%Y-%m-%d"):
        try:
            return datetime.datetime.strptime(s, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass
    return raw.strip()


# ─── Schema ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS competitions (
  competition_id   TEXT PRIMARY KEY,
  competition_name TEXT,
  country          TEXT,
  tier             INTEGER
);
CREATE TABLE IF NOT EXISTS teams (
  team_id        TEXT PRIMARY KEY,
  team_name      TEXT,
  country        TEXT,
  competition_id TEXT
);
CREATE TABLE IF NOT EXISTS matches (
  match_id         TEXT PRIMARY KEY,
  competition_id   TEXT,
  competition_name TEXT,
  season           TEXT,
  match_date       TEXT,
  round            TEXT,
  home_team_id     TEXT,
  away_team_id     TEXT,
  home_team_name   TEXT,
  away_team_name   TEXT,
  home_score_FT    INTEGER,
  away_score_FT    INTEGER,
  home_score_HT    INTEGER,
  away_score_HT    INTEGER,
  possession_home  REAL,
  possession_away  REAL,
  territory_home   REAL,
  territory_away   REAL,
  source           TEXT
);
CREATE TABLE IF NOT EXISTS players (
  player_id             TEXT PRIMARY KEY,
  first_name            TEXT,
  last_name             TEXT,
  full_name             TEXT,
  date_of_birth         TEXT,
  birth_place           TEXT,
  country               TEXT,
  qualification_country TEXT,
  height_cm             INTEGER,
  weight_kg             INTEGER,
  position              TEXT,
  incrowdsports_id      TEXT,
  rfu_slug              TEXT
);
CREATE TABLE IF NOT EXISTS player_match_stats (
  id                 INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id           TEXT,
  player_id          TEXT,
  team_id            TEXT,
  team_name          TEXT,
  home_away          TEXT,
  shirt_number       INTEGER,
  role               TEXT,
  minutes_played     INTEGER,
  tries              INTEGER,
  metres_gained      INTEGER,
  carries            INTEGER,
  defenders_beaten   INTEGER,
  clean_breaks       INTEGER,
  passes             INTEGER,
  offloads           INTEGER,
  turnovers_conceded INTEGER,
  try_assists        INTEGER,
  points_scored      INTEGER,
  tackles_attempted  INTEGER,
  missed_tackles     INTEGER,
  turnovers_won      INTEGER,
  kicks_in_play      INTEGER,
  conversions        INTEGER,
  penalty_goals      INTEGER,
  drop_goals         INTEGER,
  throws_won         INTEGER,
  lineouts_won       INTEGER,
  lineout_steals     INTEGER,
  penalties_conceded INTEGER,
  red_cards          INTEGER,
  yellow_cards       INTEGER,
  source             TEXT
);
CREATE TABLE IF NOT EXISTS team_match_stats (
  id                    INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id              TEXT,
  team_id               TEXT,
  team_name             TEXT,
  home_away             TEXT,
  tries                 INTEGER,
  metres_gained         INTEGER,
  carries               INTEGER,
  defenders_beaten      INTEGER,
  clean_breaks          INTEGER,
  passes                INTEGER,
  offloads              INTEGER,
  turnovers_conceded    INTEGER,
  tackles_attempted     INTEGER,
  missed_tackles        INTEGER,
  turnovers_won         INTEGER,
  kicks_in_play         INTEGER,
  conversions           INTEGER,
  conversion_accuracy   REAL,
  penalty_goals         INTEGER,
  penalty_goal_accuracy REAL,
  drop_goals            INTEGER,
  drop_goal_accuracy    REAL,
  rucks_won             INTEGER,
  rucks_lost            INTEGER,
  rucks_won_pct         REAL,
  mauls_won             INTEGER,
  lineouts_won          INTEGER,
  lineouts_lost         INTEGER,
  lineouts_won_pct      REAL,
  scrums_won            INTEGER,
  scrums_lost           INTEGER,
  scrums_won_pct        REAL,
  penalties_conceded    INTEGER,
  red_cards             INTEGER,
  yellow_cards          INTEGER,
  possession_pct        REAL,
  territory_pct         REAL,
  source                TEXT
);
CREATE TABLE IF NOT EXISTS player_international_profiles (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  player_id      TEXT,
  nation         TEXT,
  caps           INTEGER,
  career_minutes INTEGER,
  wins           INTEGER,
  draws          INTEGER,
  losses         INTEGER,
  source_url     TEXT
);
CREATE TABLE IF NOT EXISTS weather (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id         TEXT,
  precipitation_mm REAL,
  wind_speed_kph   REAL,
  temperature_c    REAL
);
CREATE TABLE IF NOT EXISTS odds (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  match_id      TEXT,
  bookmaker     TEXT,
  handicap_line REAL,
  home_odds     REAL,
  away_odds     REAL,
  recorded_at   TEXT
);
CREATE TABLE IF NOT EXISTS irb_rankings (
  id             INTEGER PRIMARY KEY AUTOINCREMENT,
  nation         TEXT,
  ranking        INTEGER,
  rating_points  REAL,
  recorded_date  TEXT
);
"""


# ─── Step 1: competitions ────────────────────────────────────────────────────

def populate_competitions(cur: sqlite3.Cursor) -> None:
    rows = [
        (cid, name, country, tier)
        for cid, (name, country, tier) in COMP_META.items()
    ]
    cur.executemany(
        "INSERT OR IGNORE INTO competitions VALUES (?,?,?,?)", rows
    )
    print(f"  competitions : {len(rows)} rows")


# ─── Step 2: players ────────────────────────────────────────────────────────

def populate_players(cur: sqlite3.Cursor) -> None:
    inserted = 0

    # From player_profiles.csv (biographical data, Prem players)
    with open(RAW_DIR / "player_profiles.csv", newline="", encoding="utf-8") as f:
        for p in csv.DictReader(f):
            pid = f"inc_{p['playerId']}"
            full = f"{p['firstName']} {p['lastName']}".strip()
            cur.execute("""
                INSERT OR IGNORE INTO players
                  (player_id, first_name, last_name, full_name,
                   date_of_birth, birth_place, country,
                   height_cm, weight_kg, incrowdsports_id)
                VALUES (?,?,?,?,?,?,?,?,?,?)
            """, (pid, p["firstName"], p["lastName"], full,
                  p["dateOfBirth"] or None, p["birthPlace"] or None,
                  p["country"] or None,
                  safe_int(p["height"]), safe_int(p["weight"]),
                  p["playerId"]))
            inserted += cur.rowcount

    # Collect all incrowdsports player IDs seen in any lineup
    seen: dict[str, dict] = {}
    for comp in INC_COMPS:
        path = RAW_DIR / f"{comp}_lineups.csv"
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pid = row.get("player_id", "").strip()
                if pid and pid not in seen:
                    seen[pid] = {
                        "name": row.get("player_name", "").strip(),
                        "position": row.get("position", "").strip(),
                    }

    # Also pull position from player_stats for completeness
    for comp in INC_COMPS:
        path = RAW_DIR / f"{comp}_player_stats.csv"
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                pid = row.get("player_id", "").strip()
                if pid and pid not in seen:
                    seen[pid] = {
                        "name": row.get("player", "").strip(),
                        "position": row.get("position", "").strip(),
                    }

    for pid_raw, info in seen.items():
        canonical = f"inc_{pid_raw}"
        name = info["name"]
        parts = name.rsplit(" ", 1)
        first = parts[0] if len(parts) > 1 else ""
        last  = parts[-1]
        cur.execute("""
            INSERT OR IGNORE INTO players
              (player_id, first_name, last_name, full_name, position, incrowdsports_id)
            VALUES (?,?,?,?,?,?)
        """, (canonical, first, last, name, info["position"] or None, pid_raw))
        inserted += cur.rowcount

    print(f"  players (inc) : {inserted} inserted")


# ─── Step 3: teams ──────────────────────────────────────────────────────────

def populate_teams(cur: sqlite3.Cursor) -> None:
    teams: dict[str, tuple] = {}  # slug → (name, country, comp_id)

    def add(name: str, comp_key: str) -> None:
        if not name:
            return
        slug = team_slug(name)
        if slug in teams:
            return
        country = TEAM_COUNTRY.get(slug, COMP_COUNTRY.get(comp_key, ""))
        primary = TEAM_PRIMARY_COMP.get(slug, comp_key)
        teams[slug] = (name, country, primary)

    for comp in INC_COMPS:
        path = RAW_DIR / f"{comp}_team_stats.csv"
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                add(row.get("home_team", ""), comp)
                add(row.get("away_team", ""), comp)

    # RFU teams
    with open(RAW_DIR / "rfu_team_stats.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            add(row.get("team", ""), "rfu_intl")

    cur.executemany(
        "INSERT OR IGNORE INTO teams VALUES (?,?,?,?)",
        [(slug, name, country, comp) for slug, (name, country, comp) in teams.items()]
    )
    print(f"  teams         : {len(teams)} rows")


# ─── Step 4: matches ────────────────────────────────────────────────────────

def populate_inc_matches(cur: sqlite3.Cursor) -> int:
    total = 0
    for comp in INC_COMPS:
        path = RAW_DIR / f"{comp}_team_stats.csv"
        by_match: dict[str, list] = {}
        with open(path, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                by_match.setdefault(row["match_id"], []).append(row)

        rows = []
        for mid, team_rows in by_match.items():
            h = next((r for r in team_rows if r["home_away"] == "home"), team_rows[0])
            a = next((r for r in team_rows if r["home_away"] == "away"),
                     team_rows[1] if len(team_rows) > 1 else team_rows[0])
            rows.append((
                f"inc_{mid}", comp, h.get("competition_name", ""),
                h.get("season", ""), h.get("match_date", ""), h.get("round") or None,
                team_slug(h.get("home_team", "")), team_slug(h.get("away_team", "")),
                h.get("home_team", ""), h.get("away_team", ""),
                safe_int(h.get("home_score_FT")), safe_int(h.get("away_score_FT")),
                safe_int(h.get("home_score_HT")), safe_int(h.get("away_score_HT")),
                safe_float(h.get("possession_pct")), safe_float(a.get("possession_pct")),
                safe_float(h.get("territory_pct")),  safe_float(a.get("territory_pct")),
                "incrowdsports",
            ))
        cur.executemany("INSERT OR IGNORE INTO matches VALUES "
                        "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        total += len(rows)
    return total


def populate_rfu_matches(cur: sqlite3.Cursor) -> int:
    by_match: dict[str, list] = {}
    with open(RAW_DIR / "rfu_team_stats.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_match.setdefault(row["match_id"], []).append(row)

    rows = []
    for mid, team_rows in by_match.items():
        h = next((r for r in team_rows if r["home_away"] == "home"), team_rows[0])
        a = next((r for r in team_rows if r["home_away"] == "away"),
                 team_rows[1] if len(team_rows) > 1 else team_rows[0])
        comp_id = RFU_COMP_ID.get(h.get("competition", ""), "rfu_other")
        comp_name = h.get("competition_name", "")
        home_name = h["team"]
        away_name = a["team"] if a is not h else ""
        rows.append((
            f"rfu_{mid}", comp_id, comp_name,
            h.get("season", ""), parse_rfu_date(h.get("match_date", "")), None,
            team_slug(home_name), team_slug(away_name),
            home_name, away_name,
            safe_int(h.get("home_score_FT")), safe_int(h.get("away_score_FT")),
            safe_int(h.get("home_score_HT")), safe_int(h.get("away_score_HT")),
            safe_float(h.get("possession_pct")), safe_float(a.get("possession_pct")),
            None, None,
            "rfu",
        ))
    cur.executemany("INSERT OR IGNORE INTO matches VALUES "
                    "(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    return len(rows)


# ─── Step 5: player_match_stats (incrowdsports) ──────────────────────────────

def populate_inc_player_stats(cur: sqlite3.Cursor) -> int:
    total = 0
    for comp in INC_COMPS:
        # Build lineup lookup: (match_id, player_id) → {shirt, role}
        lineup: dict[tuple, dict] = {}
        path_lu = RAW_DIR / f"{comp}_lineups.csv"
        with open(path_lu, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = (row["match_id"], row["player_id"])
                lineup[key] = {
                    "shirt_number": safe_int(row.get("shirt_number")),
                    "role": row.get("role", "") or None,
                }

        path_ps = RAW_DIR / f"{comp}_player_stats.csv"
        rows = []
        with open(path_ps, newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                mid = row["match_id"]
                pid = row["player_id"]
                lu  = lineup.get((mid, pid), {})
                rows.append((
                    f"inc_{mid}", f"inc_{pid}",
                    team_slug(row["team"]), row["team"],
                    row.get("home_away", ""),
                    lu.get("shirt_number"), lu.get("role"),
                    safe_int(row.get("minutes_played")),
                    safe_int(row.get("Tries")),
                    safe_int(row.get("Metres_Gained")),
                    safe_int(row.get("Carries")),
                    safe_int(row.get("Defenders_Beaten")),
                    safe_int(row.get("Clean_Breaks")),
                    safe_int(row.get("Passes")),
                    safe_int(row.get("Offloads")),
                    safe_int(row.get("Turnovers_Conceded")),
                    safe_int(row.get("Try_Assists")),
                    safe_int(row.get("Points_Scored")),
                    safe_int(row.get("Tackles_Attempted")),
                    safe_int(row.get("Missed_Tackles")),
                    safe_int(row.get("Turnovers_Won")),
                    safe_int(row.get("Kicks_In_Play")),
                    safe_int(row.get("Conversions")),
                    safe_int(row.get("Penalty_Goals")),
                    safe_int(row.get("Drop_Goals")),
                    None,  # throws_won — not in incrowdsports data
                    safe_int(row.get("Lineouts_Won")),
                    safe_int(row.get("Lineout_Steals")),
                    safe_int(row.get("Penalties_Conceded")),
                    safe_int(row.get("Red_Cards")),
                    safe_int(row.get("Yellow_Cards")),
                    "incrowdsports",
                ))

        cur.executemany("""
            INSERT INTO player_match_stats
              (match_id, player_id, team_id, team_name, home_away,
               shirt_number, role, minutes_played,
               tries, metres_gained, carries, defenders_beaten, clean_breaks,
               passes, offloads, turnovers_conceded, try_assists, points_scored,
               tackles_attempted, missed_tackles, turnovers_won, kicks_in_play,
               conversions, penalty_goals, drop_goals,
               throws_won, lineouts_won, lineout_steals,
               penalties_conceded, red_cards, yellow_cards, source)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, rows)
        total += len(rows)
    return total


# ─── Step 6: player_match_stats (RFU) with fuzzy matching ────────────────────

def _build_player_index(cur: sqlite3.Cursor):
    cur.execute("SELECT player_id, full_name, last_name FROM players")
    full_idx: dict[str, str]  = {}  # norm(full_name) → player_id
    last_idx: dict[str, list] = defaultdict(list)  # norm(last_name) → [(pid, full)]
    for pid, full, last in cur.fetchall():
        if full:
            full_idx[normalize_name(full)] = pid
        if last:
            last_idx[normalize_name(last)].append((pid, full or ""))
    return full_idx, last_idx


def _match_rfu_player(
    name: str,
    full_idx: dict, last_idx: dict,
    cache: dict,
    unmatched: set,
    ambiguous: set,
) -> str:
    if name in cache:
        return cache[name]

    norm = normalize_name(name)

    # 1. Exact full-name match
    if norm in full_idx:
        cache[name] = full_idx[norm]
        return cache[name]

    # 2. Fuzzy full-name match (cutoff 0.82)
    matches = difflib.get_close_matches(norm, full_idx.keys(), n=1, cutoff=0.82)
    if matches:
        cache[name] = full_idx[matches[0]]
        return cache[name]

    # 3. Exact surname match — accept only if unambiguous
    candidates = last_idx.get(norm, [])
    if len(candidates) == 1:
        cache[name] = candidates[0][0]
        return cache[name]
    if len(candidates) > 1:
        # Prefer England-club player (inc_ prefix) if there's exactly one
        inc_candidates = [c for c in candidates if c[0].startswith("inc_")]
        if len(inc_candidates) == 1:
            cache[name] = inc_candidates[0][0]
            return cache[name]
        ambiguous.add(name)
        # Still pick first inc_ match rather than drop the row
        if inc_candidates:
            cache[name] = inc_candidates[0][0]
        else:
            cache[name] = candidates[0][0]
        return cache[name]

    # 4. Fuzzy surname match (multi-word surnames like "van Poortvliet")
    matches2 = difflib.get_close_matches(norm, last_idx.keys(), n=1, cutoff=0.85)
    if matches2:
        cands2 = last_idx[matches2[0]]
        if len(cands2) == 1:
            cache[name] = cands2[0][0]
            return cache[name]

    # 5. Unmatched — create a stub rfu_ player
    slug = re.sub(r"\s+", "-", norm)
    new_id = f"rfu_{slug}"
    unmatched.add(name)
    cache[name] = new_id
    return new_id


def populate_rfu_player_stats(cur: sqlite3.Cursor) -> tuple[int, int, int, int]:
    full_idx, last_idx = _build_player_index(cur)

    # Build lineup lookup: (match_id, team, player_name) → {shirt, role}
    lineup_lu: dict[tuple, dict] = {}
    with open(RAW_DIR / "rfu_lineups.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            key = (row["match_id"], row["team"], row["player_name"])
            lineup_lu[key] = {
                "shirt_number": safe_int(row.get("shirt_number")),
                "role": row.get("role", "") or None,
            }

    cache: dict[str, str] = {}
    unmatched: set  = set()
    ambiguous: set  = set()
    new_players: dict[str, str] = {}  # player_id → name (for stub insertion)

    rows_ps = []
    with open(RAW_DIR / "rfu_player_stats.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            name = row.get("player", "").strip()
            if not name:
                continue
            pid = _match_rfu_player(name, full_idx, last_idx, cache, unmatched, ambiguous)
            if pid.startswith("rfu_") and pid not in full_idx.values():
                new_players[pid] = name

            mid  = row.get("match_id", "")
            team = row.get("team", "")
            lu   = lineup_lu.get((mid, team, name), {})
            rows_ps.append((
                f"rfu_{mid}", pid,
                team_slug(team), team,
                row.get("home_away", ""),
                lu.get("shirt_number"), lu.get("role"),
                None,  # minutes_played not in rfu_player_stats
                safe_int(row.get("Tries")),
                safe_int(row.get("Metres_Gained")),
                safe_int(row.get("Carries")),
                safe_int(row.get("Defenders_Beaten")),
                safe_int(row.get("Clean_Breaks")),
                safe_int(row.get("Passes")),
                safe_int(row.get("Offloads")),
                safe_int(row.get("Turnovers_Conceded")),
                safe_int(row.get("Try_Assists")),
                safe_int(row.get("Points_Scored")),
                safe_int(row.get("Tackles_Attempted")),
                safe_int(row.get("Missed_Tackles")),
                safe_int(row.get("Turnovers_Won")),
                safe_int(row.get("Kicks_In_Play")),
                safe_int(row.get("Conversions")),
                safe_int(row.get("Penalty_Goals")),
                safe_int(row.get("Drop_Goals")),
                safe_int(row.get("Throws_Won")),
                safe_int(row.get("Lineouts_Won")),
                safe_int(row.get("Lineout_Steals")),
                safe_int(row.get("Penalties_Conceded")),
                safe_int(row.get("Red_Cards")),
                safe_int(row.get("Yellow_Cards")),
                "rfu",
            ))

    # Insert stub players first
    for pid, name in new_players.items():
        parts = name.rsplit(" ", 1)
        first = parts[0] if len(parts) > 1 else ""
        last  = parts[-1]
        cur.execute("""
            INSERT OR IGNORE INTO players
              (player_id, first_name, last_name, full_name)
            VALUES (?,?,?,?)
        """, (pid, first, last, name))

    cur.executemany("""
        INSERT INTO player_match_stats
          (match_id, player_id, team_id, team_name, home_away,
           shirt_number, role, minutes_played,
           tries, metres_gained, carries, defenders_beaten, clean_breaks,
           passes, offloads, turnovers_conceded, try_assists, points_scored,
           tackles_attempted, missed_tackles, turnovers_won, kicks_in_play,
           conversions, penalty_goals, drop_goals,
           throws_won, lineouts_won, lineout_steals,
           penalties_conceded, red_cards, yellow_cards, source)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
    """, rows_ps)

    return len(rows_ps), len(new_players), len(unmatched), len(ambiguous)


# ─── Step 7: team_match_stats ────────────────────────────────────────────────

def _team_stat_row(row: dict, match_id: str, source: str) -> tuple:
    return (
        match_id,
        team_slug(row["team"]),
        row["team"],
        row.get("home_away", ""),
        safe_int(row.get("Tries")),
        safe_int(row.get("Metres_Gained")),
        safe_int(row.get("Carries")),
        safe_int(row.get("Defenders_Beaten")),
        safe_int(row.get("Clean_Breaks")),
        safe_int(row.get("Passes")),
        safe_int(row.get("Offloads")),
        safe_int(row.get("Turnovers_Conceded")),
        safe_int(row.get("Tackles_Attempted")),
        safe_int(row.get("Missed_Tackles")),
        safe_int(row.get("Turnovers_Won")),
        safe_int(row.get("Kicks_In_Play")),
        safe_int(row.get("Conversions")),
        safe_float(row.get("Conversion_Accuracy")),
        safe_int(row.get("Penalty_Goals")),
        safe_float(row.get("Penalty_Goal_Accuracy")),
        safe_int(row.get("Drop_Goals")),
        safe_float(row.get("Drop_Goal_Accuracy")),
        safe_int(row.get("Rucks_Won")),
        safe_int(row.get("Rucks_Lost")),
        safe_float(row.get("Rucks_Won_Pct")),
        safe_int(row.get("Mauls_Won")),
        safe_int(row.get("Lineouts_Won")),
        safe_int(row.get("Lineouts_Lost")),
        safe_float(row.get("Lineouts_Won_Pct")),
        safe_int(row.get("Scrums_Won")),
        safe_int(row.get("Scrums_Lost")),
        safe_float(row.get("Scrums_Won_Pct")),
        safe_int(row.get("Penalties_Conceded")),
        safe_int(row.get("Red_Cards")),
        safe_int(row.get("Yellow_Cards")),
        safe_float(row.get("possession_pct")),
        safe_float(row.get("territory_pct")),
        source,
    )


INSERT_TEAM_STATS = """
    INSERT INTO team_match_stats
      (match_id, team_id, team_name, home_away,
       tries, metres_gained, carries, defenders_beaten, clean_breaks,
       passes, offloads, turnovers_conceded,
       tackles_attempted, missed_tackles, turnovers_won, kicks_in_play,
       conversions, conversion_accuracy,
       penalty_goals, penalty_goal_accuracy,
       drop_goals, drop_goal_accuracy,
       rucks_won, rucks_lost, rucks_won_pct,
       mauls_won, lineouts_won, lineouts_lost, lineouts_won_pct,
       scrums_won, scrums_lost, scrums_won_pct,
       penalties_conceded, red_cards, yellow_cards,
       possession_pct, territory_pct, source)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
"""


def populate_inc_team_stats(cur: sqlite3.Cursor) -> int:
    total = 0
    for comp in INC_COMPS:
        rows = []
        with open(RAW_DIR / f"{comp}_team_stats.csv", newline="", encoding="utf-8") as f:
            for row in csv.DictReader(f):
                rows.append(_team_stat_row(row, f"inc_{row['match_id']}", "incrowdsports"))
        cur.executemany(INSERT_TEAM_STATS, rows)
        total += len(rows)
    return total


def populate_rfu_team_stats(cur: sqlite3.Cursor) -> int:
    rows = []
    with open(RAW_DIR / "rfu_team_stats.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            rows.append(_team_stat_row(row, f"rfu_{row['match_id']}", "rfu"))
    cur.executemany(INSERT_TEAM_STATS, rows)
    return len(rows)


# ─── Step 8: player_international_profiles ──────────────────────────────────

def populate_international_profiles(cur: sqlite3.Cursor) -> tuple[int, int]:
    # Build full-name lookup from players table — two keys per player for robustness
    cur.execute("SELECT player_id, full_name FROM players WHERE full_name IS NOT NULL")
    name_to_pid: dict[str, str] = {}
    compact_to_pid: dict[str, str] = {}
    for pid, full in cur.fetchall():
        name_to_pid[normalize_name(full)] = pid
        compact_to_pid[normalize_compact(full)] = pid

    inserted = 0
    unmatched_names: list[str] = []

    with open(RAW_DIR / "england_player_profiles.csv", newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            if row.get("status") != "ok":
                continue
            eng_name = row["player_name"].strip()
            norm = normalize_name(eng_name)

            # Try exact match, then fuzzy
            pid = name_to_pid.get(norm)
            if not pid:
                pid = compact_to_pid.get(normalize_compact(eng_name))
            if not pid:
                matches = difflib.get_close_matches(norm, name_to_pid.keys(), n=1, cutoff=0.82)
                if matches:
                    pid = name_to_pid[matches[0]]

            if pid:
                # Update players table with rfu_slug, height/weight/position if missing
                cur.execute("""
                    UPDATE players SET
                      rfu_slug = COALESCE(rfu_slug, ?),
                      height_cm = COALESCE(height_cm, ?),
                      weight_kg = COALESCE(weight_kg, ?),
                      position  = COALESCE(position,  ?)
                    WHERE player_id = ?
                """, (
                    row.get("slug") or None,
                    safe_int(row.get("height_cm")),
                    safe_int(row.get("weight_kg")),
                    row.get("position") or None,
                    pid,
                ))
                cur.execute("""
                    INSERT INTO player_international_profiles
                      (player_id, nation, caps, career_minutes, wins, draws, losses, source_url)
                    VALUES (?,?,?,?,?,?,?,?)
                """, (
                    pid, "England",
                    safe_int(row.get("caps")),
                    safe_int(row.get("career_mins")),
                    safe_int(row.get("england_wins")),
                    safe_int(row.get("england_draws")),
                    safe_int(row.get("england_losses")),
                    row.get("url") or None,
                ))
                inserted += 1
            else:
                unmatched_names.append(eng_name)

    return inserted, len(unmatched_names)


# ─── Main ────────────────────────────────────────────────────────────────────

def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()

    cur = con.cursor()

    print("Populating tables...")
    populate_competitions(cur)

    print("  players ...")
    populate_players(cur)

    print("  teams ...")
    populate_teams(cur)

    print("  matches ...")
    n_inc = populate_inc_matches(cur)
    n_rfu = populate_rfu_matches(cur)
    print(f"  matches       : {n_inc} incrowdsports + {n_rfu} rfu = {n_inc + n_rfu}")

    print("  player_match_stats (incrowdsports) ...")
    n_pms_inc = populate_inc_player_stats(cur)
    print(f"  player_match_stats (inc) : {n_pms_inc}")

    print("  player_match_stats (rfu, fuzzy matching) ...")
    n_pms_rfu, n_new, n_unmatched, n_ambig = populate_rfu_player_stats(cur)
    print(f"  player_match_stats (rfu) : {n_pms_rfu} rows | "
          f"{n_new} new rfu_ stubs | {n_unmatched} unmatched | {n_ambig} ambiguous")

    print("  team_match_stats ...")
    n_tms_inc = populate_inc_team_stats(cur)
    n_tms_rfu = populate_rfu_team_stats(cur)
    print(f"  team_match_stats  : {n_tms_inc} incrowdsports + {n_tms_rfu} rfu = {n_tms_inc + n_tms_rfu}")

    print("  player_international_profiles ...")
    n_intl, n_intl_unmatched = populate_international_profiles(cur)
    print(f"  intl_profiles : {n_intl} inserted | {n_intl_unmatched} unmatched england profiles")

    con.commit()

    # ── Row count report ─────────────────────────────────────────────────────
    print()
    print("=" * 55)
    print(f"{'Table':<35} {'Rows':>8}")
    print("-" * 55)
    tables = [
        "competitions", "teams", "matches", "players",
        "player_match_stats", "team_match_stats",
        "player_international_profiles", "weather", "odds", "irb_rankings",
    ]
    for t in tables:
        cur.execute(f"SELECT COUNT(*) FROM {t}")
        n = cur.fetchone()[0]
        print(f"  {t:<33} {n:>8,}")
    print("=" * 55)
    print(f"  Database : {DB_PATH}")

    # Summary of fuzzy match quality: show a few unmatched RFU names
    print()
    print("Unmatched RFU player names (sample of rfu_ stubs):")
    cur.execute("""
        SELECT DISTINCT p.full_name
        FROM player_match_stats pms
        JOIN players p ON p.player_id = pms.player_id
        WHERE pms.source = 'rfu' AND p.player_id LIKE 'rfu_%'
        ORDER BY p.full_name
        LIMIT 30
    """)
    for (name,) in cur.fetchall():
        print(f"    {name}")

    con.close()


if __name__ == "__main__":
    main()
