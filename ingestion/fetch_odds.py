"""
Pull a match line (home-team expected winning margin) from a betting API.

Two sources, tried in order:

1. api-sports.io "API-RUGBY" (v1.rugby.api-sports.io, key ``API_SPORTS_KEY``).
   Broad competition coverage incl. the Rugby Championship. Free tier 100
   req/day. Prefers a handicap/spread market; falls back to converting the
   money-line (1X2) implied probability into an approximate margin.

2. The Odds API (the-odds-api.com, key ``ODDS_API_KEY``). Rugby-union
   catalogue is Six Nations only, so this is a no-op outside Feb-March.

Keys live in a gitignored ``.env`` at the repo root (``KEY=value`` per line) or
the environment. Each successful lookup is 1-3 API requests.
"""

import math
import os
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
ODDS_API_BASE = "https://api.the-odds-api.com/v4"
API_SPORTS_BASE = "https://v1.rugby.api-sports.io"

_NAME_HINTS = {
    "england": ["england"], "ireland": ["ireland"], "france": ["france"],
    "australia": ["australia", "wallabies"], "wales": ["wales"], "scotland": ["scotland"],
    "italy": ["italy", "azzurri"], "new_zealand": ["new zealand", "all blacks"],
    "south_africa": ["south africa", "springbok"], "argentina": ["argentina", "pumas", "los pumas"],
}


def _hints(team_id: str) -> list[str]:
    return _NAME_HINTS.get(team_id, [team_id.replace("_", " ")])


def _matches(team_id: str, api_name: str) -> bool:
    low = (api_name or "").lower()
    return any(h in low for h in _hints(team_id))


def _load_key(name: str) -> str | None:
    if os.environ.get(name):
        return os.environ[name]
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip()
    return None


def _prob_to_margin(p_home: float) -> float:
    """Implied home win prob -> expected margin, via the ~15pt margin sd seen in the backtest."""
    p = min(max(p_home, 1e-4), 1 - 1e-4)
    # inverse of P(margin>0) = Phi(mu/sd): mu = sd * Phi^-1(p)
    # Phi^-1 via a rational approximation (Acklam) is overkill here; use logit*scale, close enough
    return 15.0 * math.log(p / (1 - p)) / 1.7


# --------------------------------------------------------------------------- #
# api-sports.io  API-RUGBY
# --------------------------------------------------------------------------- #
def _from_api_sports(home_team: str, away_team: str, match_date: str) -> dict | None:
    key = _load_key("API_SPORTS_KEY")
    if not key:
        return None
    h = {"x-apisports-key": key}
    year = int(match_date[:4])
    try:
        with httpx.Client(base_url=API_SPORTS_BASE, headers=h, timeout=30) as c:
            games = c.get("/games", params={"date": match_date}).json().get("response", [])
            game = next((g for g in games
                         if _matches(home_team, g["teams"]["home"]["name"])
                         and _matches(away_team, g["teams"]["away"]["name"])), None)
            if game is None:
                print(f"fetch_odds[api-sports]: no {home_team} v {away_team} game on {match_date}.")
                return None
            gid = game["id"]
            odds_resp = c.get("/odds", params={"game": gid}).json().get("response", [])
    except (httpx.HTTPError, ValueError, KeyError, StopIteration) as e:
        print(f"fetch_odds[api-sports]: {e}")
        return None

    handicap_margin = money_prob = None
    bookmaker = None
    for entry in odds_resp:
        for bm in entry.get("bookmakers", []):
            for bet in bm.get("bets", []):
                nm = bet.get("name", "").lower()
                vals = bet.get("values", [])
                if "handicap" in nm or "spread" in nm:
                    for v in vals:
                        if _matches(home_team, str(v.get("value", ""))) and v.get("handicap") is not None:
                            handicap_margin = -float(v["handicap"])
                            bookmaker = bm.get("name")
                elif nm in ("match winner", "3way result", "1x2", "home/away"):
                    try:
                        odds_by = {str(v["value"]).lower(): float(v["odd"]) for v in vals}
                    except (KeyError, ValueError, TypeError):
                        continue
                    inv = {k: 1 / o for k, o in odds_by.items() if o > 0}
                    tot = sum(inv.values())
                    for k, q in inv.items():
                        if _matches(home_team, k) or k == "home":
                            money_prob = q / tot
                            bookmaker = bookmaker or bm.get("name")
    if handicap_margin is not None:
        return {"market_margin": round(handicap_margin, 1),
                "source": f"api-sports.io / {bookmaker} / handicap"}
    if money_prob is not None:
        return {"market_margin": round(_prob_to_margin(money_prob), 1),
                "source": f"api-sports.io / {bookmaker} / money-line->margin (approx)"}
    print("fetch_odds[api-sports]: game found but no usable handicap or money-line market.")
    return None


# --------------------------------------------------------------------------- #
# The Odds API  (Six Nations only)
# --------------------------------------------------------------------------- #
def _from_odds_api(home_team: str, away_team: str, match_date: str, regions: str = "uk,eu") -> dict | None:
    key = _load_key("ODDS_API_KEY")
    if not key:
        return None
    try:
        sports = httpx.get(f"{ODDS_API_BASE}/sports/", params={"apiKey": key}, timeout=30).json()
    except (httpx.HTTPError, ValueError) as e:
        print(f"fetch_odds[odds-api]: sports list failed ({e}).")
        return None
    union = [s["key"] for s in sports if s.get("key", "").startswith("rugbyunion_") and s.get("active")]
    if not union:
        return None
    for sport in union:
        try:
            events = httpx.get(f"{ODDS_API_BASE}/sports/{sport}/odds/", params={
                "apiKey": key, "regions": regions, "markets": "spreads", "oddsFormat": "decimal",
            }, timeout=30).json()
        except (httpx.HTTPError, ValueError):
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
    return None


def fetch_market_line(home_team: str, away_team: str, match_date: str) -> dict | None:
    """
    Home team's expected winning margin per the market, e.g.
    {"market_margin": 7.0, "source": "api-sports.io / Bet365 / handicap"}.
    None if neither source has the fixture (or no keys configured).
    """
    for fn in (_from_api_sports, _from_odds_api):
        got = fn(home_team, away_team, match_date)
        if got:
            return got
    print(f"fetch_odds: no market line found for {home_team} v {away_team} {match_date} "
          "-- pass --market-line by hand.")
    return None


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--home", required=True)
    ap.add_argument("--away", required=True)
    ap.add_argument("--date", required=True, help="YYYY-MM-DD")
    a = ap.parse_args()
    print(fetch_market_line(a.home, a.away, a.date))
