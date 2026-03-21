#!/usr/bin/env python3
"""
Collect all England Men match-centre URLs across all available seasons.
Reports earliest match and total count.
"""
import asyncio
import json
import re
from pathlib import Path
from playwright.async_api import async_playwright

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)
INIT_SCRIPT = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
if (!window.chrome) window.chrome = { runtime: {} };
"""

BASE = "https://www.englandrugby.com/fixtures-and-results/search-results"

JS_COLLECT = """
() => {
    // All unique match-centre hrefs
    const links = new Set(
        Array.from(document.querySelectorAll('a[href*="match-centre"]')).map(a => a.href)
    );
    // Season options
    const seasonWrapper = Array.from(document.querySelectorAll('[data-select-type]'))
        .find(el => el.getAttribute('data-select-type') === 'Season');
    const seasonOpts = seasonWrapper ? JSON.parse(seasonWrapper.getAttribute('data-options') || '{"data":[]}').data : [];
    return { links: Array.from(links), seasonOpts };
}
"""

async def get_season_links(page, season: str) -> list[str]:
    url = f"{BASE}?team=2&season={season}"
    try:
        await page.goto(url, wait_until="load", timeout=30000)
    except Exception as exc:
        print(f"  ERROR loading {season}: {exc}")
        return []
    await page.wait_for_timeout(3000)
    r = await page.evaluate(JS_COLLECT)
    links = r["links"]
    # Deduplicate: each link appears ~3 times in DOM
    return list(dict.fromkeys(links))


async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            channel="chrome", headless=True,
            args=["--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=UA,
            extra_http_headers={
                "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                "Accept-Language": "en-GB,en;q=0.5",
            },
            service_workers="block",
        )
        await ctx.add_init_script(INIT_SCRIPT)
        page = await ctx.new_page()

        # Load first page to get season list
        print("Fetching season list...")
        await page.goto(f"{BASE}?team=2", wait_until="load", timeout=30000)
        try:
            await page.click("button#didomi-notice-agree-button", timeout=5000)
        except Exception:
            pass
        await page.wait_for_timeout(5000)
        r = await page.evaluate(JS_COLLECT)
        seasons = r["seasonOpts"]
        print(f"Found {len(seasons)} seasons: {seasons}\n")

        all_urls: list[str] = []
        per_season: dict[str, list[str]] = {}

        for season in seasons:
            links = await get_season_links(page, season)
            per_season[season] = links
            print(f"  {season}: {len(links)} matches")
            all_urls.extend(links)
            await page.wait_for_timeout(1000)

        # Deduplicate across seasons (some matches may appear in multiple seasons)
        seen = set()
        unique_urls = []
        for u in all_urls:
            if u not in seen:
                seen.add(u)
                unique_urls.append(u)

        print(f"\n{'='*60}")
        print(f"Total unique match URLs: {len(unique_urls)}")
        print(f"Seasons covered: {seasons[0]} → {seasons[-1]}")
        print(f"\nSample of URLs from earliest season ({seasons[-1]}):")
        for u in per_season[seasons[-1]][:5]:
            print(f"  {u}")

        # Save URL list for inspection
        out = Path(__file__).parent.parent / "data" / "raw" / "all_england_match_urls.txt"
        out.write_text("\n".join(unique_urls))
        print(f"\nSaved all URLs to: {out}")

        await browser.close()

asyncio.run(main())
