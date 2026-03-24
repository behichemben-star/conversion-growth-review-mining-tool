from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Callable, Awaitable, Optional
from urllib.parse import urlparse

import httpx
from playwright.sync_api import sync_playwright

from app.models import RawReview

logger = logging.getLogger(__name__)

DELAY_BETWEEN_PAGES   = 0.5    # seconds between page fetches
HTTP_TIMEOUT          = 30     # seconds
PAGE_TIMEOUT          = 45_000 # ms (Playwright) — login needs extra time
MAX_CONSECUTIVE_FAIL  = 3      # failures before giving up on a sort pass
TRUSTPILOT_PAGE_LIMIT = 10     # anonymous cap; lifted when authenticated
MAX_PAGES             = 500    # hard safety cap for authenticated scraping

SORT_ORDERS = ["recency", "ratingHigh", "ratingLow"]


class ScraperError(Exception):
    pass


# ── Shared headers ───────────────────────────────────────────────────────────

_HTML_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language":       "en-US,en;q=0.9",
    "Accept-Encoding":       "gzip, deflate, br",
    "Connection":            "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

_API_HEADERS = {
    "User-Agent":      _HTML_HEADERS["User-Agent"],
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Public entry point ───────────────────────────────────────────────────────

async def scrape_trustpilot(
    url: str,
    progress_callback: Callable[[str, int, int], Awaitable[None]],
) -> tuple[list[RawReview], int]:
    """
    Returns (reviews, trustpilot_total_reviews).

    If TRUSTPILOT_EMAIL / TRUSTPILOT_PASSWORD are set, uses an authenticated
    Playwright session — no page cap, all reviews accessible.

    Otherwise falls back to anonymous multi-sort httpx (up to ~600 reviews).
    """
    company_slug = _extract_company_slug(url)

    main_loop = asyncio.get_running_loop()

    def sync_cb(msg: str, pages: int, total: int) -> None:
        fut = asyncio.run_coroutine_threadsafe(
            progress_callback(msg, pages, total), main_loop
        )
        try:
            fut.result(timeout=5)
        except Exception:
            pass

    # ── Authenticated path: cookie-based, no page cap ───────────────────────
    # Trustpilot uses passwordless OTP login — we use session cookies extracted
    # from a manually-logged-in browser session instead of automating the login.
    # Set TRUSTPILOT_COOKIE in .env with the full Cookie header value from DevTools.
    _cookie = os.getenv("TRUSTPILOT_COOKIE", "").strip()
    logger.info("Cookie auth present: %s", bool(_cookie))
    if _cookie:
        await progress_callback("Authenticated mode — fetching all pages…", 0, 0)
        logger.info("Cookie auth mode — scraping all pages for %s", company_slug)
        reviews, trustpilot_total = await _collect_all_authenticated(
            url, company_slug, _cookie, progress_callback
        )
        if reviews:
            logger.info(
                "Cookie-auth scrape: %d reviews (Trustpilot total: %d)",
                len(reviews), trustpilot_total,
            )
            return reviews, trustpilot_total
        logger.warning("Cookie-auth returned 0 reviews — cookies may be expired. Falling back to anonymous.")

    # ── Anonymous path: httpx page 1 + multi-sort up to page 10 ─────────────
    await progress_callback("Fetching Trustpilot page…", 0, 0)
    first_page_data = await _fetch_html_httpx(url)

    if not first_page_data:
        await progress_callback(
            "Direct fetch blocked — launching stealth browser…", 0, 0
        )
        reviews = await asyncio.to_thread(
            _playwright_scrape_all, url, company_slug, 1, None, [], sync_cb
        )
        if not reviews:
            raise ScraperError(
                "No reviews found. Check the URL or the page may be blocked."
            )
        return reviews, 0

    page_props               = _get_page_props(first_page_data)
    pagination               = page_props.get("pagination", {}) or {}
    total_pages              = _read_total_pages(pagination)
    build_id                 = first_page_data.get("buildId", "")
    trustpilot_total_reviews = _read_total_review_count(page_props, total_pages)

    logger.info(
        "Anonymous mode — %s totalPages=%s trustpilot_total=%d",
        company_slug, total_pages or "unknown", trustpilot_total_reviews,
    )

    all_reviews_map: dict[str, RawReview] = {}
    first_sort_last_page = 0

    for sort_idx, sort_order in enumerate(SORT_ORDERS):
        first_page = first_page_data if sort_order == "recency" else None
        await progress_callback(
            f"Pass {sort_idx + 1}/3 — fetching {sort_order} reviews…",
            len(all_reviews_map) // 20,
            len(all_reviews_map),
        )
        sort_reviews, last_page = await _collect_httpx(
            company_slug, first_page, build_id, total_pages,
            progress_callback, sort_order,
        )
        if sort_idx == 0:
            first_sort_last_page = last_page
        before = len(all_reviews_map)
        for r in sort_reviews:
            all_reviews_map[r.id] = r
        logger.info(
            "Sort '%s': %d fetched, %d new unique — total: %d",
            sort_order, len(sort_reviews), len(all_reviews_map) - before, len(all_reviews_map),
        )

    if first_sort_last_page < 3:
        browser_reviews = await asyncio.to_thread(
            _playwright_scrape_all, url, company_slug, 1, total_pages,
            list(all_reviews_map.values()), sync_cb,
        )
        for r in browser_reviews:
            all_reviews_map[r.id] = r

    all_reviews = list(all_reviews_map.values())
    if not all_reviews:
        raise ScraperError("No reviews found. Check the URL or the page may be blocked.")

    logger.info(
        "Anonymous total: %d unique reviews (Trustpilot reports: %d)",
        len(all_reviews), trustpilot_total_reviews,
    )
    return all_reviews, trustpilot_total_reviews


# ── httpx multi-page collector ───────────────────────────────────────────────

async def _collect_httpx(
    company_slug: str,
    first_page_data: Optional[dict],   # None → fetch page 1 via API
    build_id: str,
    total_pages: int,
    progress_callback: Callable[[str, int, int], Awaitable[None]],
    sort_order: str = "recency",
) -> tuple[list[RawReview], int]:
    """
    Collect up to TRUSTPILOT_PAGE_LIMIT pages for one sort order via httpx.
    Returns (reviews, last_successfully_scraped_page).
    """
    all_reviews: list[RawReview] = []
    last_good_page   = 0
    consecutive_fail = 0

    # Hard cap: Trustpilot blocks page 11+ for anonymous users regardless of sort
    cap = TRUSTPILOT_PAGE_LIMIT

    async with httpx.AsyncClient(
        headers=_API_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True
    ) as client:

        # ── Page 1 ──────────────────────────────────────────────────────────
        if first_page_data is not None:
            # Already fetched (recency pass)
            p1_reviews = _parse_reviews_from_api_response(first_page_data)
        else:
            # Fetch via API for ratingHigh / ratingLow passes
            api_url    = _build_api_url(company_slug, build_id, 1, sort_order)
            p1_data    = await _fetch_api_page_data(client, api_url)
            p1_reviews = _parse_reviews_from_api_response(p1_data) if p1_data else []

        if p1_reviews:
            all_reviews.extend(p1_reviews)
            last_good_page = 1
            await progress_callback(
                f"[{sort_order}] Page 1 — {len(p1_reviews)} reviews",
                1, len(all_reviews),
            )

        # ── Pages 2-10 ──────────────────────────────────────────────────────
        for page_num in range(2, cap + 1):

            # Attempt 1: _next/data JSON API (fast)
            api_url      = _build_api_url(company_slug, build_id, page_num, sort_order)
            page_reviews = await _fetch_api_page(client, api_url)

            # Attempt 2: full HTML page (refreshes buildId if stale)
            if page_reviews is None:
                html_url  = (
                    f"https://www.trustpilot.com/review/{company_slug}"
                    f"?sort={sort_order}&page={page_num}"
                )
                next_data = await _fetch_html_httpx(html_url)
                if next_data:
                    page_reviews = _parse_reviews_from_api_response(next_data)
                    new_id = next_data.get("buildId")
                    if new_id and new_id != build_id:
                        logger.info(
                            "[%s] buildId refreshed at page %d", sort_order, page_num
                        )
                        build_id = new_id

            if not page_reviews:
                consecutive_fail += 1
                logger.debug(
                    "[%s] Page %d: no reviews (%d consecutive fail)",
                    sort_order, page_num, consecutive_fail,
                )
                if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                    logger.info(
                        "[%s] Stopping at page %d — %d consecutive failures",
                        sort_order, page_num, consecutive_fail,
                    )
                    break
                await asyncio.sleep(2)
                continue

            consecutive_fail = 0
            last_good_page   = page_num
            all_reviews.extend(page_reviews)
            await progress_callback(
                f"[{sort_order}] Page {page_num}/{cap} — {len(page_reviews)} reviews",
                page_num,
                len(all_reviews),
            )

            if len(page_reviews) < 20:
                break   # partial page = natural end of results

            await asyncio.sleep(DELAY_BETWEEN_PAGES)

    return all_reviews, last_good_page


async def _fetch_api_page_data(
    client: httpx.AsyncClient, api_url: str
) -> Optional[dict]:
    """Fetch one page via _next/data JSON API. Returns raw dict or None."""
    try:
        resp = await client.get(api_url)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception as exc:
        logger.debug("API fetch failed: %s", exc)
        return None


async def _fetch_api_page(
    client: httpx.AsyncClient, api_url: str
) -> Optional[list[RawReview]]:
    """Fetch one page, return parsed reviews or None on any failure."""
    data = await _fetch_api_page_data(client, api_url)
    if not data:
        return None
    reviews = _parse_reviews_from_api_response(data)
    return reviews or None


# ── Cookie-authenticated full scraper ────────────────────────────────────────

async def _collect_all_authenticated(
    url: str,
    company_slug: str,
    cookie: str,
    progress_callback: Callable[[str, int, int], Awaitable[None]],
) -> tuple[list[RawReview], int]:
    """
    Scrape ALL pages using session cookies from a logged-in Trustpilot browser.
    Cookies bypass the anonymous 10-page limit — server returns real reviews.
    Returns (all_reviews, trustpilot_total_reviews).
    """
    authed_headers = {
        **_HTML_HEADERS,
        "Cookie": cookie,
        "Accept": "application/json",
        "x-nextjs-data": "1",
    }

    all_reviews:     list[RawReview] = []
    trustpilot_total = 0
    build_id         = ""
    total_pages      = 0

    async with httpx.AsyncClient(
        headers=authed_headers, timeout=HTTP_TIMEOUT, follow_redirects=False
    ) as client:

        # ── Page 1: HTML to get buildId + metadata ───────────────────────────
        html_headers = {**_HTML_HEADERS, "Cookie": cookie}
        async with httpx.AsyncClient(
            headers=html_headers, timeout=HTTP_TIMEOUT, follow_redirects=True
        ) as html_client:
            resp = await html_client.get(url)

        if resp.status_code != 200:
            logger.error("Cookie-auth: page 1 returned HTTP %d — cookies may be invalid", resp.status_code)
            return [], 0

        match = re.search(
            r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
            resp.text, re.DOTALL,
        )
        if not match:
            logger.error("Cookie-auth: no __NEXT_DATA__ on page 1")
            return [], 0

        first_data       = json.loads(match.group(1))
        page_props       = _get_page_props(first_data)
        pagination       = page_props.get("pagination", {}) or {}
        build_id         = first_data.get("buildId", "")
        total_pages      = _read_total_pages(pagination)
        trustpilot_total = _read_total_review_count(page_props, total_pages)
        cap              = total_pages if total_pages else MAX_PAGES

        logger.info(
            "Cookie-auth: totalPages=%s trustpilot_total=%d cap=%d buildId=%.20s…",
            total_pages or "unknown", trustpilot_total, cap, build_id,
        )

        p1 = _parse_reviews_from_api_response(first_data)
        all_reviews.extend(p1)
        await progress_callback(f"Page 1/{cap} — {len(all_reviews)} reviews", 1, len(all_reviews))

        # ── Pages 2-N via _next/data JSON API ────────────────────────────────
        consec_fail = 0

        for page_num in range(2, cap + 1):
            api_url = _build_api_url(company_slug, build_id, page_num)
            try:
                resp = await client.get(api_url)

                # Redirect to login = cookies expired
                if resp.status_code in (301, 302, 307, 308):
                    location = resp.headers.get("location", "")
                    logger.warning(
                        "Cookie-auth: page %d redirected to %s — session expired",
                        page_num, location,
                    )
                    break

                if resp.status_code != 200:
                    logger.warning("Cookie-auth: page %d HTTP %d", page_num, resp.status_code)
                    consec_fail += 1
                    if consec_fail >= MAX_CONSECUTIVE_FAIL:
                        break
                    await asyncio.sleep(1)
                    continue

                data         = resp.json()
                page_reviews = _parse_reviews_from_api_response(data)

                if not page_reviews:
                    # Try refreshing buildId via HTML if API returns empty
                    if page_num <= 12:
                        html_url = f"https://www.trustpilot.com/review/{company_slug}?page={page_num}"
                        async with httpx.AsyncClient(
                            headers={**_HTML_HEADERS, "Cookie": cookie},
                            timeout=HTTP_TIMEOUT,
                            follow_redirects=False,
                        ) as hc:
                            hr = await hc.get(html_url)
                        if hr.status_code == 200:
                            hm = re.search(
                                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                                hr.text, re.DOTALL,
                            )
                            if hm:
                                hd       = json.loads(hm.group(1))
                                new_bid  = hd.get("buildId", "")
                                if new_bid and new_bid != build_id:
                                    logger.info("Cookie-auth: refreshed buildId at page %d", page_num)
                                    build_id = new_bid
                                page_reviews = _parse_reviews_from_api_response(hd)
                        elif hr.status_code in (301, 302, 307, 308):
                            logger.warning(
                                "Cookie-auth: HTML page %d redirected — session expired", page_num
                            )
                            break

                if not page_reviews:
                    consec_fail += 1
                    logger.warning(
                        "Cookie-auth: page %d empty (%d consecutive)", page_num, consec_fail
                    )
                    if consec_fail >= MAX_CONSECUTIVE_FAIL:
                        logger.info("Cookie-auth: stopping at page %d", page_num)
                        break
                    await asyncio.sleep(2)
                    continue

                consec_fail = 0
                all_reviews.extend(page_reviews)
                await progress_callback(
                    f"Page {page_num}/{cap} — {len(all_reviews)} reviews",
                    page_num, len(all_reviews),
                )

                if len(page_reviews) < 20:
                    break  # last partial page

                await asyncio.sleep(DELAY_BETWEEN_PAGES)

            except Exception as exc:
                logger.warning("Cookie-auth: page %d exception: %s", page_num, exc)
                consec_fail += 1
                if consec_fail >= MAX_CONSECUTIVE_FAIL:
                    break
                await asyncio.sleep(2)

    logger.info("Cookie-auth complete: %d reviews scraped", len(all_reviews))
    return all_reviews, trustpilot_total


# ── Playwright (picks up from any page, runs remaining pages) ────────────────

def _playwright_authenticated_scrape(
    url: str,
    company_slug: str,
    email: str,
    password: str,
    progress_callback: Callable[[str, int, int], None],
) -> tuple[list[RawReview], int]:
    """
    Log into Trustpilot then scrape ALL pages with no cap.
    Uses full page.goto() navigation for every page — guarantees auth cookies
    are sent even when context.request fails beyond page 10.
    Returns (reviews, trustpilot_total_reviews).
    """
    all_reviews:     list[RawReview] = []
    trustpilot_total = 0

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            user_agent=_HTML_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            locale="en-US",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )

        try:
            page = context.new_page()

            # ── Step 1: Login ────────────────────────────────────────────────
            progress_callback("Logging into Trustpilot…", 0, 0)
            logger.info("Auth: navigating to login page")

            page.goto(
                "https://www.trustpilot.com/login",
                wait_until="domcontentloaded",
                timeout=PAGE_TIMEOUT,
            )

            # Wait out Cloudflare
            for _ in range(8):
                t = page.title().lower()
                if "just a moment" in t or "cloudflare" in t:
                    page.wait_for_timeout(4000)
                else:
                    break

            logger.info("Auth: login page loaded — title: %s | url: %s", page.title(), page.url)

            # Trustpilot uses a two-step flow: email first, then password
            # Try email field
            email_sel = 'input[name="email"], input[type="email"], input[id*="email"], input[placeholder*="email" i]'
            try:
                page.wait_for_selector(email_sel, timeout=15_000)
                page.fill(email_sel, email)
                logger.info("Auth: filled email field")
                # Click continue/next if it's a two-step form
                next_btn = 'button[type="submit"], button[data-testid*="continue"], button[data-testid*="next"]'
                page.click(next_btn)
                page.wait_for_timeout(2000)
                logger.info("Auth: after email submit — url: %s", page.url)
            except Exception as e:
                logger.error("Auth: could not find email field: %s", e)
                return [], 0

            # Fill password (may appear after email submit in two-step flow)
            pwd_sel = 'input[name="password"], input[type="password"]'
            try:
                page.wait_for_selector(pwd_sel, timeout=15_000)
                page.fill(pwd_sel, password)
                logger.info("Auth: filled password field")
                page.click('button[type="submit"]')
                page.wait_for_load_state("domcontentloaded", timeout=PAGE_TIMEOUT)
                page.wait_for_timeout(3000)
                logger.info("Auth: after password submit — url: %s | title: %s", page.url, page.title())
            except Exception as e:
                logger.error("Auth: could not fill password: %s", e)
                return [], 0

            # Verify login
            post_login_url = page.url
            if any(x in post_login_url for x in ["/login", "/identify", "/connect", "auth."]):
                logger.error("Auth: login failed — still on auth page: %s", post_login_url)
                logger.error("Auth: page title was: %s", page.title())
                return [], 0

            logger.info("Auth: login SUCCESS — url: %s", post_login_url)
            progress_callback("Logged in — loading reviews…", 0, 0)

            # ── Step 2: Page 1 via browser navigation ────────────────────────
            page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
            page.wait_for_timeout(1500)

            try:
                page.wait_for_selector("script#__NEXT_DATA__", state="attached", timeout=PAGE_TIMEOUT)
            except Exception:
                raise ScraperError(f"Could not load review page (title: '{page.title()}')")

            next_data  = json.loads(page.eval_on_selector("script#__NEXT_DATA__", "el => el.textContent"))
            page_props = _get_page_props(next_data)
            build_id   = next_data.get("buildId", "")
            pagination = page_props.get("pagination", {}) or {}
            total_pages      = _read_total_pages(pagination)
            trustpilot_total = _read_total_review_count(page_props, total_pages)
            cap = total_pages if total_pages else MAX_PAGES

            logger.info(
                "Auth: totalPages=%s trustpilot_total=%d cap=%d",
                total_pages or "unknown", trustpilot_total, cap,
            )

            p1 = _parse_reviews_from_api_response(next_data)
            all_reviews.extend(p1)
            progress_callback(f"Page 1/{cap} — {len(all_reviews)} reviews", 1, len(all_reviews))

            # ── Step 3: All remaining pages — context.request first, ─────────
            #           full page.goto() fallback if empty (pages 11+)
            referer     = url
            consec_fail = 0
            used_goto   = False   # track if we switched to full navigation

            for page_num in range(2, cap + 1):
                reviews_this_page: Optional[list[RawReview]] = None

                # Try fast context.request first
                if not used_goto:
                    reviews_this_page = _context_fetch_page(
                        context, company_slug, build_id, page_num, referer
                    )
                    if not reviews_this_page and page_num > 10:
                        logger.info(
                            "Auth: context.request empty at page %d — switching to full navigation",
                            page_num,
                        )
                        used_goto = True

                # Full page.goto() fallback (always works when authenticated)
                if used_goto or not reviews_this_page:
                    nav_url = (
                        f"https://www.trustpilot.com/review/{company_slug}?page={page_num}"
                    )
                    try:
                        page.goto(nav_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)
                        page.wait_for_timeout(500)
                        # Check we weren't redirected to login
                        if any(x in page.url for x in ["/login", "/connect", "/identify"]):
                            logger.warning("Auth: page %d redirected to login — session expired?", page_num)
                            break
                        raw = page.eval_on_selector("script#__NEXT_DATA__", "el => el.textContent")
                        nd  = json.loads(raw)
                        reviews_this_page = _parse_reviews_from_api_response(nd)
                    except Exception as exc:
                        logger.warning("Auth: page.goto page %d failed: %s", page_num, exc)
                        reviews_this_page = None

                if not reviews_this_page:
                    consec_fail += 1
                    logger.warning("Auth: page %d empty (%d consecutive)", page_num, consec_fail)
                    if consec_fail >= MAX_CONSECUTIVE_FAIL:
                        logger.info("Auth: stopping — %d consecutive empty pages", consec_fail)
                        break
                    time.sleep(1)
                    continue

                consec_fail = 0
                all_reviews.extend(reviews_this_page)
                progress_callback(
                    f"Page {page_num}/{cap} — {len(all_reviews)} reviews scraped",
                    page_num, len(all_reviews),
                )

                if len(reviews_this_page) < 20:
                    break  # last partial page

                time.sleep(DELAY_BETWEEN_PAGES)

        finally:
            context.close()
            browser.close()

    logger.info("Auth scrape complete: %d reviews", len(all_reviews))
    return all_reviews, trustpilot_total


def _playwright_scrape_all(
    url: str,
    company_slug: str,
    start_page: int,
    total_pages: Optional[int],
    already_collected: list[RawReview],
    progress_callback: Callable[[str, int, int], None],
) -> list[RawReview]:
    """
    Stealth Chromium scraper used when httpx is blocked by Cloudflare.

    CRITICAL: Always navigates to PAGE 1 first to obtain Cloudflare clearance
    cookies, then uses context.request for all subsequent pages.  Navigating
    directly to ?page=11+ triggers Trustpilot's login redirect.
    """
    base_offset  = len(already_collected)
    new_reviews: list[RawReview] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-infobars",
                "--lang=en-US",
            ],
        )
        context = browser.new_context(
            user_agent=_HTML_HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            Object.defineProperty(navigator, 'plugins', {get: () => [
                {name:'Chrome PDF Plugin'},{name:'Chrome PDF Viewer'},{name:'Native Client'}
            ]});
            Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});
            if (!window.chrome) {
                window.chrome = {runtime:{}, loadTimes:function(){}, csi:function(){}, app:{}};
            }
            const _oQ = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({state: Notification.permission})
                    : _oQ(p);
        """)

        try:
            page = context.new_page()

            # Step 1: Always load PAGE 1 to get Cloudflare clearance cookies
            page_1_url = f"https://www.trustpilot.com/review/{company_slug}"
            progress_callback(
                "Browser: loading page 1 to establish session…", 0, base_offset
            )
            page.goto(page_1_url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT)

            title = page.title().lower()
            if "just a moment" in title or "cloudflare" in title:
                progress_callback("Browser: solving Cloudflare challenge…", 0, base_offset)
                page.wait_for_timeout(8000)

            try:
                page.wait_for_selector(
                    "script#__NEXT_DATA__", state="attached", timeout=PAGE_TIMEOUT
                )
            except Exception:
                t = page.title()
                raise ScraperError(
                    f"Browser could not load Trustpilot page 1 (title: '{t}'). "
                    "Check the URL or try again later."
                )

            raw_nd    = page.eval_on_selector("script#__NEXT_DATA__", "el => el.textContent")
            next_data = json.loads(raw_nd)
            pp1       = _get_page_props(next_data)

            if "reviews" not in pp1:
                raise ScraperError(
                    "Browser landed on an unexpected page (possibly a login wall)."
                )

            # Step 2: Extract buildId and cap
            build_id = next_data.get("buildId", "")
            cap      = TRUSTPILOT_PAGE_LIMIT   # never exceed page 10 for anonymous

            logger.info(
                "Browser session established — buildId=%.20s…", build_id
            )

            # Collect page 1 reviews if this is a full-browser run
            if start_page == 1:
                p1_reviews = _parse_reviews_from_api_response(next_data)
                if p1_reviews:
                    new_reviews.extend(p1_reviews)
                    progress_callback(
                        f"Browser: page 1 — {base_offset + len(new_reviews)} reviews total",
                        1,
                        base_offset + len(new_reviews),
                    )
                fetch_from = 2
            else:
                fetch_from = start_page

            # Step 3: Fetch pages via context.request (shares browser cookies)
            consecutive_fail = 0
            referer = f"https://www.trustpilot.com/review/{company_slug}"

            for page_num in range(fetch_from, cap + 1):
                page_reviews = _context_fetch_page(
                    context, company_slug, build_id, page_num, referer
                )

                if not page_reviews:
                    consecutive_fail += 1
                    logger.warning(
                        "Browser: page %d no reviews (%d consecutive)",
                        page_num, consecutive_fail,
                    )
                    if consecutive_fail >= MAX_CONSECUTIVE_FAIL:
                        logger.info(
                            "Browser: stopping at page %d after %d consecutive failures",
                            page_num, consecutive_fail,
                        )
                        break
                    time.sleep(2)
                    continue

                consecutive_fail = 0
                new_reviews.extend(page_reviews)
                progress_callback(
                    f"Browser: page {page_num}/{cap} — "
                    f"{base_offset + len(new_reviews)} reviews total",
                    page_num,
                    base_offset + len(new_reviews),
                )

                if len(page_reviews) < 20:
                    break

                time.sleep(DELAY_BETWEEN_PAGES)

        finally:
            context.close()
            browser.close()

    return new_reviews


# ── Browser context request helper ──────────────────────────────────────────

def _context_fetch_page(
    context,
    company_slug: str,
    build_id: str,
    page_num: int,
    referer: str,
    sort_order: str = "recency",
) -> Optional[list[RawReview]]:
    """
    Fetch one page using Playwright's context.request (shares cookie jar).
    Tries _next/data JSON first, then full HTML fallback.
    """
    # Attempt 1: _next/data JSON API
    api_url = _build_api_url(company_slug, build_id, page_num, sort_order)
    try:
        resp = context.request.get(
            api_url,
            headers={
                "Accept":        "application/json",
                "x-nextjs-data": "1",
                "Referer":       referer,
            },
            timeout=HTTP_TIMEOUT * 1000,
        )
        logger.debug("_next/data [%s] page %d → HTTP %s", sort_order, page_num, resp.status)
        if resp.ok:
            data    = resp.json()
            reviews = _parse_reviews_from_api_response(data)
            if reviews:
                return reviews
            logger.debug("_next/data [%s] page %d: 0 reviews", sort_order, page_num)
    except Exception as exc:
        logger.debug("_next/data [%s] page %d error: %s", sort_order, page_num, exc)

    # Attempt 2: full HTML fallback
    html_url = (
        f"https://www.trustpilot.com/review/{company_slug}"
        f"?sort={sort_order}&page={page_num}"
    )
    try:
        resp = context.request.get(
            html_url,
            headers={
                "Accept":  "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Referer": referer,
            },
            timeout=HTTP_TIMEOUT * 1000,
        )
        logger.debug(
            "HTML [%s] page %d → HTTP %s  url=%s",
            sort_order, page_num, resp.status, resp.url,
        )
        if resp.ok:
            html = resp.text()
            if "isSignup" in html and '"reviews"' not in html:
                logger.warning(
                    "HTML [%s] page %d redirected to login page", sort_order, page_num
                )
                return None
            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                html,
                re.DOTALL,
            )
            if match:
                data    = json.loads(match.group(1))
                reviews = _parse_reviews_from_api_response(data)
                if reviews:
                    return reviews
    except Exception as exc:
        logger.debug("HTML [%s] page %d error: %s", sort_order, page_num, exc)

    return None


# ── httpx HTML helper ────────────────────────────────────────────────────────

async def _fetch_html_httpx(url: str) -> Optional[dict]:
    """Fetch an HTML page via httpx and extract __NEXT_DATA__ JSON."""
    try:
        async with httpx.AsyncClient(
            headers=_HTML_HEADERS, timeout=HTTP_TIMEOUT, follow_redirects=True
        ) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.debug("HTML GET %s → %s", url, resp.status_code)
                return None
            match = re.search(
                r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                resp.text,
                re.DOTALL,
            )
            if not match:
                return None
            return json.loads(match.group(1))
    except Exception as exc:
        logger.debug("_fetch_html_httpx failed: %s", exc)
        return None


# ── Shared helpers ───────────────────────────────────────────────────────────

def _extract_company_slug(url: str) -> str:
    parsed = urlparse(url)
    path   = parsed.path.rstrip("/")
    parts  = path.split("/")
    if len(parts) >= 3 and parts[1] == "review":
        return parts[2]
    raise ScraperError(
        f"Cannot extract company slug from URL: {url}. "
        "Expected format: https://www.trustpilot.com/review/brand.com"
    )


def _build_api_url(
    company_slug: str, build_id: str, page: int, sort: str = "recency"
) -> str:
    return (
        f"https://www.trustpilot.com/_next/data/{build_id}"
        f"/review/{company_slug}.json?sort={sort}&page={page}"
    )


def _get_page_props(api_data: dict) -> dict:
    """Extract pageProps regardless of nesting level."""
    pp = api_data.get("props", {}).get("pageProps", {})
    if not pp:
        pp = api_data.get("pageProps", {})
    return pp or {}


def _read_total_pages(pagination: dict) -> int:
    raw = (
        pagination.get("totalPages")
        or pagination.get("total_pages")
        or pagination.get("totalNumberOfPages")
        or pagination.get("pageCount")
        or 0
    )
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _read_total_review_count(page_props: dict, total_pages: int) -> int:
    """
    Try multiple locations in Trustpilot's __NEXT_DATA__ to find the real
    total review count, for display in the UI cap-banner.
    """
    # 1. businessUnit.numberOfReviews (most reliable)
    bu = page_props.get("businessUnit", {}) or {}
    if bu.get("numberOfReviews"):
        try:
            return int(bu["numberOfReviews"])
        except (TypeError, ValueError):
            pass

    # 2. Sum of rating distribution (e.g. ratingDistribution: [{stars:5,count:1200},…])
    for key in ("ratingDistribution", "filters"):
        dist = page_props.get(key)
        if isinstance(dist, list) and dist:
            total = sum(
                item.get("count", 0)
                for item in dist
                if isinstance(item, dict)
            )
            if total:
                return total

    # 3. totalReviews directly on pagination
    pagination = page_props.get("pagination", {}) or {}
    for field in ("totalReviews", "total", "count"):
        val = pagination.get(field)
        if val:
            try:
                return int(val)
            except (TypeError, ValueError):
                pass

    # 4. Estimate from totalPages (least reliable — each page ≈ 20 reviews)
    if total_pages:
        return total_pages * 20

    return 0


def _parse_reviews_from_api_response(api_data: dict) -> list[RawReview]:
    page_props = _get_page_props(api_data)
    result: list[RawReview] = []
    for r in page_props.get("reviews", []):
        try:
            rating = r.get("rating", {})
            stars  = rating.get("stars", 3) if isinstance(rating, dict) else int(rating)

            dates         = r.get("dates", {})
            published_raw = dates.get("publishedDate") or r.get("createdAt", "")
            try:
                published = datetime.fromisoformat(published_raw.replace("Z", "+00:00"))
            except (ValueError, AttributeError):
                published = datetime.now(timezone.utc)

            consumer      = r.get("consumer", {})
            consumer_name = consumer.get("displayName", "Anonymous")
            labels        = r.get("labels", {})
            is_verified   = labels.get("verification", {}).get("isVerified", False)
            reply         = r.get("companyReply", {})
            company_reply = reply.get("text") if reply else None

            result.append(
                RawReview(
                    id=str(r.get("id", "")),
                    rating=stars,
                    title=r.get("title") or None,
                    text=r.get("text", "") or "",
                    published_date=published,
                    consumer_name=consumer_name,
                    is_verified=is_verified,
                    company_reply=company_reply,
                )
            )
        except Exception:
            continue
    return result
