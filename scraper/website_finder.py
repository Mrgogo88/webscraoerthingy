"""Website discovery and verification.

Three jobs, all free (no API keys):

1. `is_social_url`      — detect "websites" that are really just a Facebook /
                          Instagram / Linktree page. These businesses have no
                          real website — prime redesign leads.
2. `check_website_status` — fast HTTP probe classifying a listed website as
                          live / dead / bot-blocked, so dead links (very
                          common in OpenStreetMap data) become leads instead
                          of wasted crawls.
3. `find_website`       — DuckDuckGo HTML search to discover an official
                          website for businesses OSM lists without one, so
                          "no website" leads are verified, not assumed.
"""

from __future__ import annotations

import difflib
import re
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import requests
from bs4 import BeautifulSoup

import config

logger = config.setup_logging("leadfinder.finder")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_DELAY_SECONDS = 3.5  # be polite; DDG rate-limits aggressive scrapers

# A "website" pointing at one of these is not a real business website.
SOCIAL_ONLY_DOMAINS = (
    "facebook.com", "m.facebook.com", "fb.com", "instagram.com",
    "twitter.com", "x.com", "tiktok.com", "youtube.com", "youtu.be",
    "linktr.ee", "linktree.com", "yelp.com", "business.site",
    "pinterest.com", "wa.me", "whatsapp.com", "g.page", "goo.gl",
)

# Search results from these hosts are directories/aggregators, never the
# business's own site. Skipped during discovery.
DIRECTORY_DOMAINS = SOCIAL_ONLY_DOMAINS + (
    "google.com", "maps.google.com", "duckduckgo.com", "bing.com",
    "yellowpages.com", "bbb.org", "angi.com", "angieslist.com",
    "homeadvisor.com", "thumbtack.com", "houzz.com", "porch.com",
    "mapquest.com", "foursquare.com", "tripadvisor.com", "zocdoc.com",
    "healthgrades.com", "webmd.com", "opencare.com", "ada.org",
    "wikipedia.org", "linkedin.com", "indeed.com", "glassdoor.com",
    "nextdoor.com", "manta.com", "chamberofcommerce.com", "dnb.com",
    "buzzfile.com", "cylex.us.com", "hotfrog.com", "2findlocal.com",
    "superpages.com", "whitepages.com", "birdeye.com", "yellowbook.com",
    "citysearch.com", "merchantcircle.com", "us-info.com", "apple.com",
)

# Words that carry no identity when matching a business name to a domain.
_NAME_STOPWORDS = {
    "the", "and", "of", "a", "an", "llc", "inc", "co", "corp", "company",
    "companies", "llp", "pllc", "pc", "pa", "dds", "dmd", "md", "dr",
    "group", "services", "service", "solutions", "associates",
}

# Industry-generic words: appearing in a domain proves nothing (every dental
# site contains "dental"). Only distinctive tokens confirm a match.
_GENERIC_WORDS = {
    "dental", "dentistry", "dentist", "orthodontics", "smile", "smiles",
    "roofing", "roof", "roofers", "construction", "contractors", "builders",
    "plumbing", "plumbers", "hvac", "heating", "cooling", "air", "electric",
    "electrical", "electricians", "law", "legal", "attorney", "attorneys",
    "lawyers", "firm", "restaurant", "grill", "cafe", "kitchen", "pizza",
    "clinic", "health", "healthcare", "medical", "care", "family", "auto",
    "automotive", "repair", "salon", "spa", "beauty", "hair", "fitness",
    "gym", "realty", "real", "estate", "homes", "insurance", "agency",
    "accounting", "tax", "veterinary", "vet", "animal", "pet", "chiropractic",
    "wellness", "landscaping", "lawn", "cleaning", "texas", "dallas",
    "plano", "usa", "america", "american", "north", "south", "east", "west",
}


@dataclass
class WebsiteStatus:
    """Result of a quick reachability probe."""

    status: str  # 'live' | 'dead' | 'blocked'
    http_code: int | None = None
    final_url: str | None = None
    detail: str | None = None


class DiscoveryUnavailable(Exception):
    """The search engine is rate-limiting or unreachable — the caller must
    NOT record 'no website found'; retry on a later scan instead."""


def _host(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def _matches_domain(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def is_social_url(url: str | None) -> bool:
    """True if the URL is a social-media/profile page, not a real website."""
    if not url:
        return False
    return _matches_domain(_host(url), SOCIAL_ONLY_DOMAINS)


def check_website_status(url: str) -> WebsiteStatus:
    """Quickly probe a URL and classify it. Never raises.

    'dead'    - DNS failure, timeout, 4xx/5xx (except bot-block codes)
    'blocked' - 401/403/406/429/503: site exists but rejects automated
                clients; must not be counted as a broken website
    'live'    - anything that returns a normal page
    """
    headers = {"User-Agent": BROWSER_UA, "Accept-Language": "en-US,en;q=0.9"}
    try:
        resp = requests.get(url, headers=headers, timeout=15, allow_redirects=True, stream=True)
        code = resp.status_code
        final_url = resp.url
        resp.close()
    except requests.exceptions.ConnectionError as exc:
        return WebsiteStatus("dead", detail=f"Connection failed: {str(exc)[:120]}")
    except requests.exceptions.Timeout:
        return WebsiteStatus("dead", detail="Timed out after 15s")
    except requests.RequestException as exc:
        return WebsiteStatus("dead", detail=f"Request failed: {str(exc)[:120]}")

    if code in (401, 403, 406, 429, 503):
        return WebsiteStatus("blocked", code, final_url, f"HTTP {code} (bot protection likely)")
    if code >= 400:
        return WebsiteStatus("dead", code, final_url, f"HTTP {code}")
    return WebsiteStatus("live", code, final_url)


def _significant_tokens(name: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return [t for t in tokens if t not in _NAME_STOPWORDS and len(t) >= 3]


def _match_score(url: str, business_name: str) -> float:
    """How plausibly this domain belongs to this business (0 = no, higher = better).

    Generic industry words ("dental", "roofing") prove nothing — every
    competitor and directory contains them. A match requires either a
    *distinctive* name token in the domain root, or the whole condensed
    name fuzzy-matching the root (for all-generic names like
    "Dallas Dental Care" -> dallasdentalcare.com).
    """
    root = _host(url).split(".")[0]
    tokens = _significant_tokens(business_name)
    if not tokens:
        return 0.0

    distinctive = [t for t in tokens if t not in _GENERIC_WORDS]
    score = 0.0
    for t in distinctive:
        if len(t) >= 4 and t in root:
            score += 2.0
        elif t in root:  # short distinctive token, weaker evidence
            score += 0.5

    condensed = "".join(tokens)
    ratio = difflib.SequenceMatcher(None, condensed, root).ratio()
    if ratio >= 0.6:
        score += 2.0 * ratio

    return score


def _domain_matches_name(url: str, business_name: str) -> bool:
    return _match_score(url, business_name) >= 1.0


def _page_mentions_name(url: str, business_name: str) -> bool:
    """Fetch a candidate's homepage and check the business name appears on it.

    This confirms generic domains a name/domain heuristic can't (e.g.
    "Delicate Dental" -> dentist-dallas.com). Cheap GET, capped read.
    """
    parsed = urlparse(url)
    homepage = f"{parsed.scheme}://{parsed.netloc}/"
    try:
        resp = requests.get(
            homepage, headers={"User-Agent": BROWSER_UA}, timeout=12, stream=True
        )
        if resp.status_code >= 400:
            return False
        # Accumulate up to ~500KB — iter_content chunks arrive at network
        # size, so a single next() would only read the first packet.
        raw = b""
        for chunk in resp.iter_content(65_536):
            raw += chunk
            if len(raw) >= 500_000:
                break
        resp.close()
        text = raw.decode("utf-8", errors="ignore").lower()
    except requests.RequestException:
        return False

    text = re.sub(r"\s+", " ", text)
    phrase = re.sub(r"\s+", " ", business_name.lower().strip())
    if phrase in text:
        return True
    tokens = _significant_tokens(business_name)
    distinctive = [t for t in tokens if t not in _GENERIC_WORDS] or tokens
    return bool(distinctive) and all(t in text for t in distinctive)


def _extract_result_urls(html: str) -> list[str]:
    """Pull organic result URLs out of the DuckDuckGo HTML page."""
    soup = BeautifulSoup(html, "lxml")
    urls: list[str] = []
    for a in soup.select("a.result__a[href]"):
        href = a["href"]
        # DDG wraps results as //duckduckgo.com/l/?uddg=<encoded>&rut=…
        if "uddg=" in href:
            qs = parse_qs(urlparse(href).query)
            target = qs.get("uddg", [None])[0]
            if target:
                urls.append(unquote(target))
        elif href.startswith("http"):
            urls.append(href)
    return urls


def find_website(name: str, city: str = "", state: str = "") -> str | None:
    """Search the web for a business's official website. Returns None when
    nothing trustworthy is found — a conservative miss beats a wrong match.

    Raises DiscoveryUnavailable when the search engine itself is failing or
    rate-limiting, so callers don't mistake an outage for "has no website".
    """
    query = " ".join(p for p in (name, city, state) if p)
    try:
        resp = requests.post(
            DDG_HTML_URL,
            data={"q": query},
            headers={"User-Agent": BROWSER_UA},
            timeout=20,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("Discovery search failed for %r: %s", query, exc)
        raise DiscoveryUnavailable(str(exc)) from exc
    finally:
        time.sleep(DDG_DELAY_SECONDS)

    all_results = _extract_result_urls(resp.text)
    if not all_results:
        # A real query virtually always has *some* results; an empty page
        # means DDG served a challenge/rate-limit page instead.
        logger.warning("Discovery returned zero results for %r — rate-limited?", query)
        raise DiscoveryUnavailable("empty result page (rate-limited)")

    # Collect non-directory candidates, deduped by domain, in rank order.
    candidates: list[str] = []
    seen_hosts: set[str] = set()
    for url in all_results[:10]:
        host = _host(url)
        if not host or host in seen_hosts or _matches_domain(host, DIRECTORY_DOMAINS):
            continue
        seen_hosts.add(host)
        candidates.append(url)

    # Pass 1: a domain that clearly contains the business name is trusted.
    best_url, best_score = None, 0.0
    for position, url in enumerate(candidates):
        score = _match_score(url, name) + max(0.0, (10 - position) * 0.05)
        if score > best_score:
            best_url, best_score = url, score
    if best_url and best_score >= 1.0:
        logger.info("Discovered website for %r: %s (domain match %.2f)", name, best_url, best_score)
        return best_url

    # Pass 2: generic domains — confirm by checking the business name
    # actually appears on the candidate's homepage.
    for url in candidates[:3]:
        if _page_mentions_name(url, name):
            homepage = urlparse(url)
            confirmed = f"{homepage.scheme}://{homepage.netloc}/"
            logger.info("Discovered website for %r via page-content check: %s", name, confirmed)
            return confirmed

    logger.info("No trustworthy website found for %r", name)
    return None
