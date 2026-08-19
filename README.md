# Rugby Analytics

A personal data project for collecting, structuring, and analyzing rugby data across club and international rugby: match/player/team stats, squad composition, and coach-era comparisons.

## What's in here

**League/club data** — lineups and match/player/team stats scraped for Premiership, Premiership Cup, Champions Cup, Challenge Cup, URC, Top 14, and Japan Rugby League One, plus a dedicated RFU (England Rugby match centre) scraper for domestic and international fixtures.

**International/national-team data** — match history, squad ages, and per-player minutes pulled from the World Rugby pulselive API, covering 10 national teams (England, Ireland, Scotland, Wales, France, Italy, New Zealand, South Africa, Argentina, Australia) from 2015 (extended pre-2019 for the Eddie Jones era) through 2026.

**Squad tooling** — given a newly-announced squad, `squad_lookup.py` resolves each player's age-as-of-matchday and current caps from the built roster; England squad announcements from 6 Nations 2023 through Summer 2026 are tracked with caps/age history.

**Coach usage analysis** — rotation, attrition, and minutes-usage comparison across five national coach tenures (Farrell/Ireland, Borthwick/England, Galthié/France, Erasmus's second stint/South Africa, Townsend/Scotland), output as dashboard figures.

**SQLite build** — `ingestion/build_db.py` consolidates all raw CSVs (competitions, players, teams, matches, stats, international profiles) into `data/rugby_analytics.db`, fuzzy-matching player identities across sources.

## Structure

```
rugby_analytics/
├── data/
│   ├── raw/              # Scraped/exported CSVs per competition and team
│   └── rugby_analytics.db
├── ingestion/            # Scrapers and dataset builders
├── analysis/             # Analysis scripts and modules
├── outputs/
│   ├── figures/          # Generated plots and dashboards
│   └── reports/
└── notebooks/            # Exploration notebooks
```

## Notes

- World Rugby data comes from the undocumented pulselive JSON API (no auth required); club/league data is scraped per-competition, with a dedicated scraper for England Rugby's match centre.
- API responses are cached locally (gitignored) so pipelines are re-runnable without re-hitting rate limits.
