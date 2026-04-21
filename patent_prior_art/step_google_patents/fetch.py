from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

GOOGLE_PATENTS_URL = "https://patents.google.com/patent/{patent_number}/en"
REQUEST_TIMEOUT = 20
NAV_TIMEOUT = 30_000


def fetch_html(patent_number: str, no_headless: bool = False) -> str:
    """Return full HTML for a Google Patents page, trying requests first."""
    url = GOOGLE_PATENTS_URL.format(patent_number=patent_number)
    log.info(f"Fetching {url}")

    try:
        import requests
        resp = requests.get(
            url,
            headers={"User-Agent": "Mozilla/5.0 (compatible; patent-prior-art-bot/1.0)"},
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        html = resp.text
        # Sanity check: a valid patent page has itemprop="claims"
        if 'itemprop="claims"' in html:
            log.info(f"Fetched {len(html):,} chars via requests")
            return html
        log.warning("requests returned HTML without claims section — falling back to Playwright")
    except Exception as e:
        log.warning(f"requests failed ({e}) — falling back to Playwright")

    return _fetch_playwright(url, no_headless=no_headless)


def _fetch_playwright(url: str, no_headless: bool = False) -> str:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not no_headless)
        page = browser.new_page()
        try:
            page.goto(url, timeout=NAV_TIMEOUT)
            page.wait_for_selector('[itemprop="claims"]', timeout=NAV_TIMEOUT)
            time.sleep(1)
            html = page.content()
            log.info(f"Fetched {len(html):,} chars via Playwright")
            return html
        finally:
            browser.close()
