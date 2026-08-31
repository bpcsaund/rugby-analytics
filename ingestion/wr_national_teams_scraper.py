"""
World Rugby (pulselive) API scraper for national-team Test match history.

Extends the local CSV dataset in data/raw/sheets_export/ to cover six more
national teams (Wales, Scotland, Italy, New Zealand, South Africa, Argentina)
for 2019-2026, matching the existing style of france_rugby.csv / australia_rugby.csv
/ ireland_rugby.csv (wide match-by-match sheet) plus a long-format per-player
minutes file like ireland_minutes_jul2026.csv.

Data source: undocumented JSON API at api.wr-rims-prod.pulselive.com, no auth.
See project instructions / memory for endpoint details.

This script is organised as a pipeline of resumable stages, each backed by a
JSON cache under data/raw/wr_api_cache/ so it can be re-run without re-hitting
the API. Run stages via the CLI at the bottom (`python wr_national_teams_scraper.py <stage>`)
or just `all`.
"""
import csv
import json
import os
import time
import sys
from datetime import datetime, timedelta

import httpx

BASE = "https://api.wr-rims-prod.pulselive.com/rugby/v3"
ROOT = os.path.expanduser("~/rugby_analytics")
CACHE = os.path.join(ROOT, "data/raw/wr_api_cache")
BULK_CACHE = os.path.join(CACHE, "bulk")
MATCH_CACHE = os.path.join(CACHE, "match")
RANK_CACHE = os.path.join(CACHE, "rankings")
OUT_DIR = os.path.join(ROOT, "data/raw/sheets_export")

for d in (BULK_CACHE, MATCH_CACHE, RANK_CACHE):
    os.makedirs(d, exist_ok=True)

# Team ids we care about (per task brief)
TEAM_IDS = {
    "33": "wales",
    "35": "scotland",
    "37": "nz",
    "39": "sa",
    "40": "argentina",
    "41": "italy",
}

SLEEP = 1.0
CLIENT = httpx.Client(timeout=30.0)


def _get(url, params=None):
    for attempt in range(5):
        try:
            r = CLIENT.get(url, params=params)
            if r.status_code == 200:
                return r.json()
            else:
                print(f"  WARN status={r.status_code} url={url} params={params}")
                time.sleep(1.0)
        except httpx.HTTPError as e:
            print(f"  WARN exception {e} url={url}")
            time.sleep(1.0)
    return None


# ---------------------------------------------------------------------------
# Stage 1: bulk match list
# ---------------------------------------------------------------------------

def fetch_bulk():
    page = 0
    page_size = 100
    total_calls = 0
    while True:
        cache_path = os.path.join(BULK_CACHE, f"page_{page}.json")
        if os.path.exists(cache_path):
            with open(cache_path) as f:
                d = json.load(f)
        else:
            d = _get(f"{BASE}/match", params={
                "states": "U,UP,L,CC,C",
                "sort": "asc",
                "page": page,
                "pageSize": page_size,
                "startDate": "2019-01-01",
                "endDate": "2026-12-31",
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


def _bulk_sort_key(filename: str) -> tuple[int, int]:
    # page_N.json -> (1, N); page_pre2019_N.json -> (0, N), sorting before the main range
    parts = filename.split("_")
    if parts[1] == "pre2019":
        return (0, int(parts[2].split(".")[0]))
    return (1, int(parts[1].split(".")[0]))


def load_bulk():
    matches = []
    files = sorted(os.listdir(BULK_CACHE), key=_bulk_sort_key)
    for fn in files:
        with open(os.path.join(BULK_CACHE, fn)) as f:
            d = json.load(f)
        matches.extend(d["content"])
    return matches


def target_matches():
    """Return list of match dicts (deduped) where at least one team is in TEAM_IDS
    and status == 'C' (complete)."""
    matches = load_bulk()
    seen = {}
    for m in matches:
        if m.get("status") != "C":
            continue
        team_ids = [t["id"] for t in m["teams"]]
        if not any(tid in TEAM_IDS for tid in team_ids):
            continue
        seen[m["matchAltId"]] = m
    return list(seen.values())


# ---------------------------------------------------------------------------
# Stage 2: per-match summary + timeline
# ---------------------------------------------------------------------------

def fetch_match_details():
    matches = target_matches()
    print(f"target matches: {len(matches)}")
    calls = 0
    skipped = []
    for i, m in enumerate(matches):
        alt_id = m["matchAltId"]
        summ_path = os.path.join(MATCH_CACHE, f"{alt_id}_summary.json")
        tl_path = os.path.join(MATCH_CACHE, f"{alt_id}_timeline.json")
        if not os.path.exists(summ_path):
            d = _get(f"{BASE}/match/{alt_id}/summary")
            calls += 1
            time.sleep(SLEEP)
            if d is None:
                skipped.append((alt_id, "summary_fetch_failed"))
                continue
            with open(summ_path, "w") as f:
                json.dump(d, f)
        if not os.path.exists(tl_path):
            d = _get(f"{BASE}/match/{alt_id}/timeline")
            calls += 1
            time.sleep(SLEEP)
            if d is None:
                skipped.append((alt_id, "timeline_fetch_failed"))
                continue
            with open(tl_path, "w") as f:
                json.dump(d, f)
        if i % 25 == 0:
            print(f"  {i}/{len(matches)} done, calls={calls}")
    print(f"match detail fetch done, api calls={calls}, skipped={len(skipped)}")
    for s in skipped:
        print("  SKIP", s)


# ---------------------------------------------------------------------------
# Stage 3: rankings by date
# ---------------------------------------------------------------------------

def fetch_rankings():
    matches = target_matches()
    dates_needed = set()
    for m in matches:
        d = datetime.strptime(m["time"]["label"], "%Y-%m-%d")
        day_before = (d - timedelta(days=1)).strftime("%Y-%m-%d")
        dates_needed.add(day_before)
    print(f"unique ranking dates needed: {len(dates_needed)}")
    calls = 0
    for i, dt in enumerate(sorted(dates_needed)):
        path = os.path.join(RANK_CACHE, f"{dt}.json")
        if os.path.exists(path):
            continue
        d = _get(f"{BASE}/rankings/mru", params={"language": "en", "date": dt})
        calls += 1
        time.sleep(SLEEP)
        if d is None:
            print(f"  FAILED rankings for {dt}")
            continue
        with open(path, "w") as f:
            json.dump(d, f)
        if i % 25 == 0:
            print(f"  {i}/{len(dates_needed)} rankings done")
    print(f"rankings fetch done, api calls={calls}")


# ---------------------------------------------------------------------------
# Stage 4: build output CSVs
# ---------------------------------------------------------------------------

TEAM_ID_TO_COUNTRY = {
    "33": "Wales",
    "35": "Scotland",
    "37": "New Zealand",
    "39": "South Africa",
    "40": "Argentina",
    "41": "Italy",
}

OPPOSITION_CODE = {
    "Wales": "wales", "Scotland": "scotland", "Italy": "italy",
    "New Zealand": "nz", "South Africa": "sa", "Argentina": "argentina",
    "England": "england", "France": "france", "Australia": "australia",
    "Ireland": "ireland", "Japan": "japan", "Fiji": "fiji", "Georgia": "georgia",
    "Tonga": "tonga", "Samoa": "samoa", "USA": "usa", "Canada": "canada",
    "Uruguay": "uruguay", "Portugal": "portugal", "Namibia": "namibia",
    "Romania": "romania", "British & Irish Lions": "lions",
    "Maori All Blacks": "maori abs", "Chile": "chile", "Spain": "spain",
    "Russia": "russia",
}

# Teams whose matches against our 6 are not full internationals / not in our
# vocabulary (club or invitational sides) -- entire row skipped.
EXCLUDE_OPPONENTS = {"Barbarians", "Queensland Reds", "Randwick"}

SANZAAR_NAMES = {"New Zealand", "South Africa", "Argentina", "Australia"}

WC_HOST = {"2019": "Japan", "2023": "France"}

# Manual overrides for edge cases identified during research (date, team_id) -> tournament code.
# Both are standalone one-off friendlies played just before the official Autumn
# Nations Cup 2020 kicked off (round 1 was 13 Nov 2020) -- not part of any
# championship structure.
TOURNAMENT_OVERRIDES = {
    ("2020-10-23", "35"): "friendly",  # Scotland v Georgia
    ("2020-10-24", "33"): "friendly",  # France v Wales (Wales side)
}

# Coach era tables: list of (start_date, end_date_inclusive, code) per team id.
# Researched via WebSearch July 2026; sources noted in final report.
COACH_ERAS = {
    "33": [  # Wales
        ("2000-01-01", "2019-11-01", "wg"),   # Warren Gatland (1st stint), through RWC2019 bronze final
        ("2019-11-02", "2022-11-30", "wp"),   # Wayne Pivac
        ("2022-12-01", "2025-02-08", "wg"),   # Warren Gatland (2nd stint), resigned after Italy loss 8 Feb 2025
        ("2025-02-09", "2025-08-31", "ms"),   # Matt Sherratt (interim: rest of 6N 2025 + Japan tour)
        ("2025-09-01", "2099-01-01", "st"),   # Steve Tandy, first match 9 Nov 2025
    ],
    "35": [  # Scotland
        ("2000-01-01", "2099-01-01", "gt"),   # Gregor Townsend, continuous 2017-2026+
    ],
    "41": [  # Italy
        ("2000-01-01", "2019-11-21", "cos"),  # Conor O'Shea, through RWC2019, resigned 21 Nov 2019
        ("2019-11-22", "2021-03-31", "fs"),   # Franco Smith (interim then permanent)
        ("2021-04-01", "2023-12-31", "kc"),   # Kieran Crowley, from July 2021 through Nov 2023 internationals
        ("2024-01-01", "2099-01-01", "gq"),   # Gonzalo Quesada, from Jan 2024 / 2024 6N
    ],
    "37": [  # New Zealand
        ("2000-01-01", "2019-11-01", "sh"),   # Steve Hansen, through RWC2019 bronze final
        ("2019-11-02", "2023-10-28", "if"),   # Ian Foster, through RWC2023 final
        ("2023-10-29", "2099-01-01", "sr"),   # Scott Robertson, from 2024
    ],
    "39": [  # South Africa
        ("2000-01-01", "2019-11-02", "re"),   # Rassie Erasmus (1st stint), through RWC2019 final
        ("2019-11-03", "2023-10-28", "jn"),   # Jacques Nienaber (SA played no Tests at all in 2020), through RWC2023 final
        ("2023-10-29", "2099-01-01", "re"),   # Rassie Erasmus (2nd stint), from 2024
    ],
    "40": [  # Argentina
        ("2000-01-01", "2021-11-30", "ml"),   # Mario Ledesma, through end of 2021 internationals
        ("2022-07-02", "2099-01-01", "mc"),   # Michael Cheika, debut 2 Jul 2022
    ],
}


def get_coach(team_id, date_str):
    for start, end, code in COACH_ERAS[team_id]:
        if start <= date_str <= end:
            return code
    return ""


def classify_tournament(own_name, opp_name, date_str, competition_label, h_a, team_id):
    """Return (tournament_code, test_flag) for a match."""
    comp = competition_label or ""
    month = int(date_str[5:7])
    year = date_str[:4]

    # Lions matches -- special-cased (only 2 cases in our whole dataset)
    if opp_name == "British & Irish Lions":
        if own_name == "South Africa":
            return "lions", "yes"
        # Argentina v Lions, Dublin, 20 Jun 2025 -- officially uncapped one-off
        return "friendly", "no"
    if opp_name == "Maori All Blacks":
        return "tour", "no"

    override = TOURNAMENT_OVERRIDES.get((date_str, team_id))
    if override:
        return override, "yes"

    if "Rugby World Cup" in comp:
        return "wc", "yes"
    if "Six Nations" in comp:
        return "6n", "yes"
    if "Nations Championship" in comp:
        return "nc", "yes"
    # SA v NZ "Rugby's Greatest Rivalry" (from 2026) is a standalone bilateral
    # series, not the Rugby Championship -- classify as a tour, not rc.
    if "Greatest Rivalry" in comp:
        return "tour", "yes"
    if own_name in SANZAAR_NAMES and opp_name in SANZAAR_NAMES and month in (6, 7, 8, 9, 10):
        return "rc", "yes"
    if month in (10, 11, 12):
        return ("eoyt", "yes") if h_a == "h" else ("tour", "yes")
    if month in (6, 7, 8):
        if month == 8 and year in WC_HOST:
            return "friendly", "yes"
        return "tour", "yes"
    return "friendly", "yes"


def get_h_a(team_id, opp_id, venue_country, tournament_code):
    if tournament_code == "wc":
        return "n"  # none of our 6 teams hosted RWC2019/2023
    team_country = TEAM_ID_TO_COUNTRY.get(team_id)
    if venue_country == team_country:
        return "h"
    return "a"


def strip_accents(s):
    import unicodedata
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c))


def norm_surname(last_known):
    return strip_accents(last_known).lower().strip()


RANK_DATE_CACHE = {}


def get_rank_pos(team_id, date_before):
    """Look up rank position for team_id as of date_before (day before kickoff),
    falling back to earlier dates (up to 10 days back) if the exact date wasn't
    published."""
    d = datetime.strptime(date_before, "%Y-%m-%d")
    for back in range(0, 11):
        dt = (d - timedelta(days=back)).strftime("%Y-%m-%d")
        if dt in RANK_DATE_CACHE:
            entries = RANK_DATE_CACHE[dt]
        else:
            path = os.path.join(RANK_CACHE, f"{dt}.json")
            if not os.path.exists(path):
                RANK_DATE_CACHE[dt] = None
                continue
            with open(path) as f:
                data = json.load(f)
            entries = {e["team"]["id"]: e["pos"] for e in data["entries"]}
            RANK_DATE_CACHE[dt] = entries
        if entries and team_id in entries:
            return entries[team_id]
    return None


def load_match(alt_id):
    with open(os.path.join(MATCH_CACHE, f"{alt_id}_summary.json")) as f:
        summ = json.load(f)
    with open(os.path.join(MATCH_CACHE, f"{alt_id}_timeline.json")) as f:
        tl = json.load(f)
    return summ, tl


def compute_minutes(team_idx, summary, timeline):
    """Return dict player_id -> (shirt:int, display_name, last_known, started:bool, minutes:int).

    Event types that move a player on/off the pitch:
      - "Sub On" / "Sub Off": normal tactical substitution, direction is explicit.
      - "IR" (Injury Replacement) / "HIAO" (HIA failed, player out): the feed
        tags BOTH the player leaving and the player entering with the SAME
        type at the SAME timestamp, so direction isn't in the event itself --
        it has to be inferred as a toggle of that player's current on/off
        state (verified against known shirt numbers/positions in several
        matches: e.g. a starting prop tagged "IR" alongside a bench front-row
        replacement tagged "IR" at the identical second).
    A player who never appears in these events is on for the whole match if
    they started, or unused (0 minutes) if they were a bench replacement.
    """
    players = [p for p in summary["teams"][team_idx]["teamList"]["list"] if p.get("number")]
    TOGGLE_TYPES = ("Sub On", "Sub Off", "IR", "HIAO")
    events_by_player = {}
    all_secs = []
    for e in timeline.get("timeline", []):
        if e.get("teamIndex") != team_idx:
            continue
        if e.get("type") not in TOGGLE_TYPES:
            continue
        pid = e.get("playerId")
        secs = e["time"]["secs"]
        events_by_player.setdefault(pid, []).append((secs, e["type"]))
        all_secs.append(secs)
    match_end = max([4800] + all_secs)

    result = {}
    for p in players:
        pid = p["player"]["id"]
        shirt = int(p["number"])
        started = shirt <= 15
        evs = sorted(events_by_player.get(pid, []), key=lambda x: x[0])
        if not evs:
            minutes = 80 if started else 0
        else:
            total = 0
            if started:
                state, last = "on", 0
            else:
                state, last = "off", None
            for secs, typ in evs:
                if typ in ("IR", "HIAO"):
                    # Direction isn't explicit in the event -- infer as a
                    # toggle of current state (best-effort; may be
                    # re-anchored below by a later explicit Sub On/Off,
                    # which is treated as authoritative).
                    direction = "off" if state == "on" else "on"
                else:
                    direction = "off" if typ == "Sub Off" else "on"
                if direction == "off":
                    if state == "on":
                        total += max(0, secs - last)
                    state, last = "off", None
                else:
                    # A redundant "on" signal for a player we already have on
                    # the pitch is ignored rather than re-anchoring (tested
                    # empirically against the sum-of-minutes-per-match == 1200
                    # invariant across the whole dataset; this variant produced
                    # the lower aggregate deviation of the two options tried).
                    if state == "off":
                        state, last = "on", secs
            if state == "on" and last is not None:
                total += max(0, match_end - last)
            minutes = round(total / 60)
        first = p["player"]["name"].get("first") or {}
        first_known = first.get("known") or first.get("official") or ""
        last_known = p["player"]["name"]["last"]["known"] or p["player"]["name"]["last"]["official"]
        result[pid] = {
            "shirt": shirt,
            "display": p["player"]["name"]["display"],
            "last_known": last_known,
            "first_known": first_known,
            "started": started,
            "minutes": minutes,
        }
    return result


def build_surname_map(team_id, matches):
    """Scan all matches for this team, group appearances by normalized surname,
    disambiguate collisions with the shortest unique first-name-initial prefix."""
    # surname -> {player_id: first_known}
    groups = {}
    for m in matches:
        team_ids = [t["id"] for t in m["teams"]]
        if team_id not in team_ids:
            continue
        idx = team_ids.index(team_id)
        summ, tl = load_match(m["matchAltId"])
        info = compute_minutes(idx, summ, tl)
        for pid, rec in info.items():
            surname = norm_surname(rec["last_known"])
            groups.setdefault(surname, {})[pid] = rec["first_known"]

    disambig = {}  # (surname, player_id) -> output string
    for surname, players in groups.items():
        if len(players) == 1:
            pid = next(iter(players))
            disambig[(surname, pid)] = surname
            continue
        # need disambiguation
        L = 1
        while True:
            prefixes = {pid: strip_accents(fn).lower()[:L] for pid, fn in players.items()}
            if len(set(prefixes.values())) == len(prefixes):
                break
            L += 1
            if L > 10:
                break  # give up, fall back to full first name
        for pid, fn in players.items():
            prefix = strip_accents(fn).lower()[:L]
            disambig[(surname, pid)] = f"{surname}, {prefix}"
    return disambig


def build_team_data(team_id, short_code, matches, disambig):
    wide_rows = []
    minutes_rows = []
    skips = []
    for m in matches:
        team_ids = [t["id"] for t in m["teams"]]
        if team_id not in team_ids:
            continue
        idx = team_ids.index(team_id)
        opp_idx = 1 - idx
        own_name = m["teams"][idx]["name"]
        opp_name = m["teams"][opp_idx]["name"]
        if opp_name in EXCLUDE_OPPONENTS:
            continue
        if opp_name not in OPPOSITION_CODE:
            skips.append((m["matchAltId"], m["time"]["label"], f"unmapped opponent {opp_name}"))
            continue
        date_str = m["time"]["label"]
        try:
            summ, tl = load_match(m["matchAltId"])
        except FileNotFoundError:
            skips.append((m["matchAltId"], date_str, "missing cache file"))
            continue
        if not summ.get("teams") or len(summ["teams"]) < 2:
            skips.append((m["matchAltId"], date_str, "empty/malformed summary"))
            continue

        venue_country = m.get("venue", {}).get("country")
        own_score = m["scores"][idx]
        opp_score = m["scores"][opp_idx]
        opp_id = m["teams"][opp_idx]["id"]

        # provisional h/a using tournament classification (wc needs no h/a input, others do)
        tournament, test_flag = classify_tournament(
            own_name, opp_name, date_str, m.get("competition"), None, team_id
        )
        h_a = get_h_a(team_id, opp_id, venue_country, tournament)
        # re-classify now that we know h_a (affects eoyt/tour split)
        tournament, test_flag = classify_tournament(
            own_name, opp_name, date_str, m.get("competition"), h_a, team_id
        )

        if own_score > opp_score:
            result = "w"
        elif own_score < opp_score:
            result = "l"
        else:
            result = "d"

        date_before = (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
        if opp_name in ("British & Irish Lions", "Maori All Blacks"):
            own_irb = opp_irb = irb_diff = "n/a"
        else:
            own_pos = get_rank_pos(team_id, date_before)
            opp_pos = get_rank_pos(opp_id, date_before)
            own_irb = own_pos if own_pos is not None else ""
            opp_irb = opp_pos if opp_pos is not None else ""
            irb_diff = (opp_pos - own_pos) if (own_pos is not None and opp_pos is not None) else ""

        coach = get_coach(team_id, date_str)
        opp_code = OPPOSITION_CODE[opp_name]

        info = compute_minutes(idx, summ, tl)
        by_shirt = {}
        for pid, rec in info.items():
            surname = norm_surname(rec["last_known"])
            out_name = disambig.get((surname, pid), surname)
            by_shirt[rec["shirt"]] = out_name
            minutes_rows.append({
                "date": datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y"),
                "opposition": opp_name,
                "shirt": rec["shirt"],
                "player": rec["display"],
                "started": "y" if rec["started"] else "n",
                "minutes": rec["minutes"],
            })
        lineup = [by_shirt.get(i, "") for i in range(1, 24)]

        row = {
            "date": datetime.strptime(date_str, "%Y-%m-%d").strftime("%d/%m/%Y"),
            "year": date_str[:4],
            "coach": coach,
            "opposition": opp_code,
            "tournament": tournament,
            "test": test_flag,
            "h / a": h_a,
            f"{short_code} irb": own_irb,
            "opp irb": opp_irb,
            "irb difference": irb_diff,
            "higher / lower": "",
            "result": result,
            "points scored ": own_score,
            "points conceded": opp_score,
            "difference": own_score - opp_score,
        }
        for i in range(1, 24):
            row[str(i)] = lineup[i - 1]
        row["_sort_date"] = date_str
        wide_rows.append(row)
    wide_rows.sort(key=lambda r: r["_sort_date"])
    minutes_rows.sort(key=lambda r: datetime.strptime(r["date"], "%d/%m/%Y"))
    return wide_rows, minutes_rows, skips


def write_wide_csv(path, short_code, rows):
    header = (
        ["date", "year", "coach", "opposition", "tournament", "test", "h / a",
         f"{short_code} irb", "opp irb", "irb difference", "higher / lower",
         "result", "points scored ", "points conceded", "difference"]
        + [str(i) for i in range(1, 24)]
    )
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def write_minutes_csv(path, rows):
    header = ["date", "opposition", "shirt", "player", "started", "minutes"]
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def build_all():
    matches = target_matches()
    report = {}
    for team_id, short_code in TEAM_IDS.items():
        print(f"building {short_code} ...")
        disambig = build_surname_map(team_id, matches)
        wide_rows, minutes_rows, skips = build_team_data(team_id, short_code, matches, disambig)
        out_name = {"nz": "new_zealand", "sa": "south_africa"}.get(short_code, short_code)
        write_wide_csv(os.path.join(OUT_DIR, f"{out_name}_rugby.csv"), short_code, wide_rows)
        write_minutes_csv(os.path.join(OUT_DIR, f"{out_name}_minutes.csv"), minutes_rows)
        report[short_code] = {
            "rows": len(wide_rows),
            "date_range": (wide_rows[0]["date"], wide_rows[-1]["date"]) if wide_rows else None,
            "skips": skips,
        }
        print(f"  {short_code}: {len(wide_rows)} matches, {len(skips)} skipped")
    return report


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "all"
    if stage in ("all", "bulk"):
        fetch_bulk()
    if stage in ("all", "match"):
        fetch_match_details()
    if stage in ("all", "rankings"):
        fetch_rankings()
    if stage in ("all", "build"):
        build_all()
