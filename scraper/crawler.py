"""Website crawler: visits each business homepage with Playwright and extracts
quality signals (title, meta description, HTTPS, load time, images, links,
mobile viewport, contact page, emails, social links) plus a screenshot.

Designed so one bad website never crashes a batch: every crawl returns a
CrawlResult, with `error` set when the site could not be loaded.
"""

from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout
from playwright.async_api import async_playwright

import config
from scraper import modernity

logger = config.setup_logging("leadfinder.crawler")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
# US phone formats: (214) 368-6792, 214-368-6792, 214.368.6792, +1 214 368 6792
PHONE_RE = re.compile(r"(?:\+?1[\s.-]?)?\(?\b(\d{3})\)?[\s.-](\d{3})[\s.-](\d{4})\b")

# Common false positives inside asset filenames, e.g. "logo@2x.png"
EMAIL_JUNK_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".css", ".js")

SOCIAL_DOMAINS = (
    "facebook.com", "instagram.com", "twitter.com", "x.com", "linkedin.com",
    "youtube.com", "tiktok.com", "yelp.com", "pinterest.com", "nextdoor.com",
)

CONTACT_HINTS = ("contact", "contact-us", "contactus", "get-in-touch", "reach-us")


@dataclass
class CrawlResult:
    """Everything extracted from one homepage visit."""

    url: str
    final_url: str | None = None
    title: str | None = None
    meta_description: str | None = None
    https_enabled: bool = False
    load_time: float | None = None  # seconds
    image_count: int = 0
    broken_images: int = 0
    internal_links: int = 0
    external_links: int = 0
    mobile_friendly: bool = False  # has a viewport meta tag
    contact_page_found: bool = False
    emails_found: list[str] = field(default_factory=list)
    phones_found: list[str] = field(default_factory=list)
    social_links: list[str] = field(default_factory=list)
    screenshot_path: str | None = None
    # Design-era analysis (see scraper/modernity.py)
    copyright_year: int | None = None
    legacy_signals: list[str] = field(default_factory=list)
    is_outdated: bool = False
    error: str | None = None


def _extract_from_html(result: CrawlResult, html: str, page_url: str) -> None:
    """Parse the rendered HTML with BeautifulSoup and fill in the result."""
    soup = BeautifulSoup(html, "lxml")

    if soup.title and soup.title.string:
        result.title = soup.title.string.strip()[:500]

    meta = soup.find("meta", attrs={"name": re.compile("^description$", re.I)})
    if meta and meta.get("content"):
        result.meta_description = meta["content"].strip()

    result.mobile_friendly = soup.find("meta", attrs={"name": "viewport"}) is not None

    result.image_count = len(soup.find_all("img"))

    page_host = urlparse(page_url).netloc.lower().removeprefix("www.")
    internal = external = 0
    socials: set[str] = set()
    contact_found = False
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("javascript:", "#")):
            continue
        if href.lower().startswith("mailto:"):
            addr = href[7:].split("?")[0].strip()
            if addr:
                result.emails_found.append(addr)
            continue
        if href.lower().startswith("tel:"):
            number = re.sub(r"[^\d+]", "", href[4:])
            if len(number) >= 10:
                result.phones_found.append(number)
            continue
        absolute = urljoin(page_url, href)
        host = urlparse(absolute).netloc.lower().removeprefix("www.")
        if not host:
            continue
        if host == page_host:
            internal += 1
            path = urlparse(absolute).path.lower()
            if any(hint in path for hint in CONTACT_HINTS):
                contact_found = True
        else:
            external += 1
            if any(host == d or host.endswith("." + d) for d in SOCIAL_DOMAINS):
                socials.add(absolute)

    result.internal_links = internal
    result.external_links = external
    result.social_links = sorted(socials)
    result.contact_page_found = contact_found

    # Emails and phone numbers in visible text (in addition to mailto:/tel:)
    page_text = soup.get_text(" ")
    for match in EMAIL_RE.findall(page_text):
        if not match.lower().endswith(EMAIL_JUNK_SUFFIXES):
            result.emails_found.append(match)
    for area, mid, last in PHONE_RE.findall(page_text):
        result.phones_found.append(f"({area}) {mid}-{last}")
    # Dedupe, preserve order
    result.emails_found = list(dict.fromkeys(e.lower() for e in result.emails_found))
    result.phones_found = list(dict.fromkeys(result.phones_found))[:5]

    # Design-era analysis on the full rendered HTML
    era = modernity.analyze_html(html)
    result.copyright_year = era.copyright_year
    result.legacy_signals = era.legacy_signals
    result.is_outdated = era.is_outdated


async def _count_broken_images(page) -> int:
    """Count <img> elements that failed to load (naturalWidth == 0)."""
    try:
        return await page.evaluate(
            """() => Array.from(document.images)
                    .filter(img => img.src && img.complete && img.naturalWidth === 0)
                    .length"""
        )
    except PlaywrightError:
        return 0


async def crawl_website(url: str, business_id: int, browser) -> CrawlResult:
    """Crawl one homepage. Never raises; failures are recorded in result.error."""
    result = CrawlResult(url=url)
    context = None
    try:
        context = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
            ),
            ignore_https_errors=True,
        )
        page = await context.new_page()

        start = time.monotonic()
        response = await page.goto(
            url,
            timeout=config.CRAWL_TIMEOUT_SECONDS * 1000,
            wait_until="load",
        )
        # Give lazy content a moment to settle, but don't hang on busy pages.
        try:
            await page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeout:
            pass
        result.load_time = round(time.monotonic() - start, 2)

        result.final_url = page.url
        result.https_enabled = page.url.lower().startswith("https://")

        if response and response.status >= 400:
            result.error = f"HTTP {response.status}"
            return result

        html = await page.content()
        _extract_from_html(result, html, page.url)
        result.broken_images = await _count_broken_images(page)

        # Screenshot of the viewport (full_page can hang on infinite-scroll sites)
        config.ensure_dirs()
        shot_path = config.SCREENSHOT_DIR / f"business_{business_id}.png"
        await page.screenshot(path=str(shot_path))
        result.screenshot_path = str(shot_path.relative_to(config.BASE_DIR))

        logger.info("Crawled %s (%.1fs, title=%r)", url, result.load_time, result.title)
    except PlaywrightTimeout:
        result.error = f"Timed out after {config.CRAWL_TIMEOUT_SECONDS}s"
        logger.warning("Timeout crawling %s", url)
    except PlaywrightError as exc:
        result.error = f"Browser error: {str(exc).splitlines()[0][:300]}"
        logger.warning("Browser error crawling %s: %s", url, result.error)
    except Exception as exc:  # noqa: BLE001 - one bad site must not kill the batch
        result.error = f"Unexpected error: {exc!r}"
        logger.error("Unexpected error crawling %s: %r", url, exc)
    finally:
        if context is not None:
            try:
                await context.close()
            except PlaywrightError:
                pass
    return result


async def crawl_many(targets: list[tuple[int, str]]) -> dict[int, CrawlResult]:
    """Crawl multiple (business_id, url) pairs with bounded concurrency.

    Returns {business_id: CrawlResult}.
    """
    results: dict[int, CrawlResult] = {}
    semaphore = asyncio.Semaphore(config.CRAWL_CONCURRENCY)

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:

            async def worker(business_id: int, url: str) -> None:
                async with semaphore:
                    results[business_id] = await crawl_website(url, business_id, browser)

            await asyncio.gather(*(worker(bid, url) for bid, url in targets))
        finally:
            await browser.close()

    return results


def crawl_many_sync(targets: list[tuple[int, str]]) -> dict[int, CrawlResult]:
    """Synchronous wrapper for crawl_many (for CLI / Streamlit use)."""
    return asyncio.run(crawl_many(targets))
