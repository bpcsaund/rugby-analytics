"""
Pull a match handicap (spread) line from The Odds API (the-odds-api.com).

Coverage caveat: The Odds API's *rugby union* catalogue is only
`rugbyunion_six_nations` (and historically `rugbyunion_world_cup`) -- there is
NO Rugby Championship / Nations Championship / autumn-international / July-tour
coverage. So this auto-populates a market line during the Six Nations and is a
graceful no-op the rest of the year; pass --market-line by hand otherwise.

Key: put `ODDS_API_KEY=...` in a gitignored `.env` at the repo root, or set it
in the environment. Free tier = 500 requests/month; each call here is 1 request.
"""

import os
from pathlib import Path

import httpx

BASE = "https://api.the-odds-api.com/v4"
ROOT = Path(__file__).resolve().parent.parent
# team id (our convention) -> substrings that identify it in Odds API team names
_NAME_HINTS = {
    "england": ["england"], "ireland": ["ireland"], "france": ["france"],
    "australia": ["australia"], "wales": ["wales"], "scotland": ["scotland"],
    "italy": ["italy"], "new_zealand": ["new zealand", "all blacks"],
    "south_africa": ["south africa", "springbok"], "argentina": ["argentina", "pumas"],
}


def _load_key() -> str | None:
    if os.environ.get("ODDS_API_KEY"):
        return os.environ["ODDS_API_KEY"]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith("ODDS_API_KEY="):
                return line.split("=", 1)[1].strip()
    return None


def _matches(team_id: str, api_name: str) -> bool:
    low = api_name.lower()
    return any(h in low for h in _NAME_HINTS.get(team_id, [team_id.replace("_", " ")]))


def fetch_market_line(home_team: str, away_team: str, match_date: str,
                      regions: str = "uk,eu") -> dict | None:
    """
    Home team's expected winning margin per the market's handicap line, e.g.
    {"market_margin": 7.0, "source": "the-odds-api / rugbyunion_six_nations / Pinnacle"}.
    None if no key, no active rugby-union competition, or no matching fixture.
    """
    key = _load_key()
    if not key:
        print("fetch_odds: no ODDS_API_KEY (set it in .env or the environment) -- skipping.")
        return None
    try:
        sports = httpx.get(f"{BASE}/sports/", params={"apiKey": key}, timeout=30).json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"fetch_odds: sports list failed ({e}).")
        return None
    union = [s["key"] for s in sports if s.get("key", "").startswith("rugbyunion_") and s.get("active")]
    if not union:
        print("fetch_odds: no active rugby-union competition on The Odds API right now "
              "(it only ever carries the Six Nations) -- pass --market-line by hand.")
        return None

    for sport in union:
        try:
            resp = httpx.get(f"{BASE}/sports/{sport}/odds/", params={
                "apiKey": key, "regions": regions, "markets": "spreads", "oddsFormat": "decimal",
            }, timeout=30)
            resp.raise_for_status()
            events = resp.json()
        except (httpx.HTTPError, ValueError) as e:
            print(f"fetch_odds: odds fetch failed for {sport} ({e}).")
            continue
        for ev in events:
            if not (_matches(home_team, ev.get("home_team", "")) and _matches(away_team, ev.get("away_team", ""))):
                continue
            if match_date not in ev.get("commence_time", ""):
                continue
            for bm in ev.get("bookmakers", []):
                for mk in bm.get("markets", []):
                    if mk.get("key") != "spreads":
                        continue
                    for oc in mk.get("outcomes", []):
                        if _matches(home_team, oc.get("name", "")) and oc.get("point") is not None:
                            return {"market_margin": -float(oc["point"]),
                                    "source": f"the-odds-api / {sport} / {bm.get('title')}"}
    print(f"fetch_odds: no {home_team} v {away_team} spread found for {match_date}.")
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    a = ap.parse_args()
    print(fetch_market_line(a.home, a.away, a.date))
