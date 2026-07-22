"""LeadFinder pipeline orchestrator and CLI entry point.

Pipeline (v2 — lead-focused):
    1. OSM discovery        -> find & save businesses
    2. Social-only check    -> a Facebook/Instagram "website" = no real site
    3. Website verification -> probe listed sites: live / dead / bot-blocked
    4. Website discovery    -> free web search finds sites OSM doesn't know
    5. Crawl + Lighthouse   -> only live sites, deduped by domain
    6. Scoring              -> 0-100 quality score + lead priority

CLI usage:
    python app.py scan --city Dallas --state TX --category dentists --radius 10
    python app.py dashboard
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urlparse

import config
from database.database import get_session, init_db
from database.models import Business, WebsiteAnalysis
from scraper import chains, google_places
from scraper import lighthouse as lh
from scraper import osm_search, website_finder
from scraper.crawler import crawl_many_sync
from scraper.scoring import compute_score

logger = config.setup_logging("leadfinder.app")

# Cap DuckDuckGo discovery lookups per scan to stay under the rate-limit radar.
# Google Places (when configured) is the primary lookup and gets a much
# higher cap — its free quota comfortably covers full-area verification.
MAX_DISCOVERY_PER_SCAN = 30
MAX_GOOGLE_LOOKUPS_PER_SCAN = 300


def _discover_websites_for(
    businesses: list,
    city: str,
    state: str,
    report: "ScanReport",
    notify: Callable[[str], None],
) -> None:
    """Find websites for businesses that have none listed, updating the DB.

    Google Places (authoritative) is tried first when configured; DuckDuckGo
    search is the free fallback. Outages are never recorded as 'no website'.
    """
    use_google = google_places.is_configured()
    ddg_failures = 0
    ddg_available = True
    google_available = True

    for i, b in enumerate(businesses, 1):
        url = None
        verified = False  # True once a source authoritatively answered

        if use_google and google_available:
            try:
                url = google_places.lookup_website(
                    b.name, city=b.city or city, state=b.state or state,
                    lat=b.latitude, lon=b.longitude,
                )
                verified = True  # Google answered: site, or confirmed none
            except google_places.GooglePlacesError as exc:
                google_available = False
                msg = f"Google lookup failing ({exc}) — falling back to web search."
                report.errors.append(msg)
                notify(msg)

        if not verified and ddg_available:
            try:
                url = website_finder.find_website(
                    b.name, city=b.city or city, state=b.state or state
                )
                verified = True
            except website_finder.DiscoveryUnavailable:
                ddg_failures += 1
                if ddg_failures >= 3:
                    ddg_available = False
                    msg = "Web search is rate-limiting — remaining lookups postponed to the next scan."
                    notify(msg)
                    report.errors.append(msg)
                else:
                    import time as _time

                    _time.sleep(15)

        if not verified:
            if not ddg_available and not (use_google and google_available):
                break  # nothing left to try
            continue

        with get_session() as session:
            row = session.get(Business, b.id)
            if row is None:  # deleted mid-scan (e.g. via Clear data)
                continue
            if url and website_finder.is_social_url(url):
                row.website = url
                row.website_source = "discovered"
                row.website_status = "social_only"
                report.social_only += 1
            elif url:
                row.website = url
                row.website_source = "google" if (use_google and google_available) else "discovered"
                report.websites_discovered += 1
            else:
                row.website_status = "not_found"
        if i % 10 == 0:
            notify(f"Website lookup: {i}/{len(businesses)} done…")


@dataclass
class ScanReport:
    """Summary of one full scan, for CLI output and the dashboard."""

    businesses_found: int = 0
    businesses_new: int = 0
    google_found: int = 0
    chains_excluded: int = 0
    no_website: int = 0
    social_only: int = 0
    websites_discovered: int = 0
    dead_websites: int = 0
    blocked_websites: int = 0
    crawled_ok: int = 0
    crawl_failed: int = 0
    audited: int = 0
    outdated_found: int = 0
    addresses_filled: int = 0
    errors: list[str] = field(default_factory=list)


def _domain(url: str) -> str:
    return urlparse(url).netloc.lower().removeprefix("www.")


def run_scan(
    category: str,
    city: str = "",
    state: str = "",
    zip_code: str = "",
    radius_miles: float = 10.0,
    run_lighthouse: bool = True,
    discover_websites: bool = True,
    reanalyze: bool = False,
    progress: Callable[[str], None] | None = None,
) -> ScanReport:
    """Run the full discovery + verification + audit pipeline."""
    report = ScanReport()
    notify = progress or (lambda msg: None)
    init_db()
    key = category.strip().lower()

    # --- 1. Discover businesses via OpenStreetMap (+ Google if configured) --
    notify(f"Searching OpenStreetMap for {category!r}…")
    try:
        coords = osm_search.geocode(city=city, state=state, zip_code=zip_code)
        found = osm_search.search_businesses(
            category, city=city, state=state, zip_code=zip_code,
            radius_miles=radius_miles, coords=coords,
        )
    except (osm_search.GeocodingError, osm_search.OverpassError) as exc:
        report.errors.append(str(exc))
        logger.error("Discovery failed: %s", exc)
        return report

    report.businesses_found = len(found)
    report.businesses_new = osm_search.save_businesses(found, city=city, state=state)
    notify(f"OpenStreetMap: {report.businesses_found} businesses ({report.businesses_new} new).")

    if google_places.is_configured():
        notify("Searching Google Maps (Places API)…")
        try:
            gfound = google_places.search_places(
                category, lat=coords[0], lon=coords[1],
                radius_miles=radius_miles, city=city, state=state,
            )
            report.google_found = len(gfound)
            new_from_google = osm_search.save_businesses(gfound, city=city, state=state)
            no_site = sum(1 for g in gfound if not g.website)
            notify(
                f"Google Maps: {len(gfound)} businesses ({new_from_google} new), "
                f"{no_site} with no website on their listing."
            )
            report.businesses_new += new_from_google
        except google_places.GooglePlacesError as exc:
            msg = f"Google Places search failed: {exc}"
            report.errors.append(msg)
            logger.error(msg)

    # --- 1b. Flag chains/franchises — corporate sites are not local leads --
    report.chains_excluded = chains.mark_chains(category=key)
    if report.chains_excluded:
        notify(f"Excluded {report.chains_excluded} chain/franchise locations from lead analysis.")

    if reanalyze:
        with get_session() as session:
            ids = [b.id for b in session.query(Business).filter(Business.category == key)]
            session.query(WebsiteAnalysis).filter(
                WebsiteAnalysis.business_id.in_(ids)
            ).delete(synchronize_session=False)
        notify("Cleared previous analyses for re-scan.")

    # --- 2. Social-only detection ------------------------------------------
    with get_session() as session:
        businesses = (
            session.query(Business)
            .filter(Business.category == key, Business.is_chain == False)  # noqa: E712
            .all()
        )
        for b in businesses:
            if b.website and website_finder.is_social_url(b.website):
                b.website_status = "social_only"
                report.social_only += 1
    if report.social_only:
        notify(f"{report.social_only} businesses only have a social-media page — hot leads.")

    # --- 3. Discover websites for businesses OSM lists without one ---------
    if discover_websites:
        with get_session() as session:
            missing = (
                session.query(Business)
                .filter(
                    Business.category == key,
                    Business.is_chain == False,  # noqa: E712 — don't waste searches on chains
                    Business.website.is_(None),
                    # don't re-search businesses we already looked up
                    Business.website_status.is_(None),
                )
                .all()
            )
        if missing:
            cap = MAX_GOOGLE_LOOKUPS_PER_SCAN if google_places.is_configured() else MAX_DISCOVERY_PER_SCAN
            capped = missing[:cap]
            source = "Google Maps" if google_places.is_configured() else "the web"
            if len(missing) > len(capped):
                notify(f"Looking up {len(capped)} of {len(missing)} missing websites on {source}…")
            else:
                notify(f"Looking up {len(capped)} businesses with no listed website on {source}…")
            _discover_websites_for(capped, city, state, report, notify)
            notify(
                f"Website lookup done: found {report.websites_discovered} real websites; "
                f"businesses still without one are verified no-website leads."
            )

    # --- 4. Verify listed websites (live / dead / blocked) -----------------
    from sqlalchemy import or_

    with get_session() as session:
        to_verify = (
            session.query(Business)
            .filter(
                Business.category == key,
                Business.is_chain == False,  # noqa: E712
                Business.website.isnot(None),
                # NULL-safe: unverified rows have website_status = NULL, and
                # SQL "NULL NOT IN (...)" is never true — check IS NULL explicitly.
                or_(
                    Business.website_status.is_(None),
                    Business.website_status.in_(["blocked"]),  # recheck bot-blocks
                    *( [Business.website_status.notin_(["social_only"])] if reanalyze else [] ),
                ),
            )
            .all()
        )
    if to_verify:
        notify(f"Verifying {len(to_verify)} listed websites…")
    status_by_domain: dict[str, website_finder.WebsiteStatus] = {}
    for b in to_verify:
        dom = _domain(b.website)
        if dom not in status_by_domain:
            status_by_domain[dom] = website_finder.check_website_status(b.website)
        st = status_by_domain[dom]
        with get_session() as session:
            session.get(Business, b.id).website_status = st.status
        if st.status == "dead":
            report.dead_websites += 1
        elif st.status == "blocked":
            report.blocked_websites += 1
    if report.dead_websites:
        notify(f"{report.dead_websites} listed websites are dead — hot leads.")

    # --- 5. Pick crawl targets (live + blocked, deduped by domain) ---------
    with get_session() as session:
        analyzed_ids = {row[0] for row in session.query(WebsiteAnalysis.business_id)}
        candidates = [
            (b.id, b.website, b.website_status)
            for b in session.query(Business).filter(
                Business.category == key,
                Business.is_chain == False,  # noqa: E712
                Business.website.isnot(None),
                Business.website_status.in_(["live", "blocked"]),
            )
            if b.id not in analyzed_ids
        ]
        # Record dead sites as analyses immediately — no crawl needed.
        for b in session.query(Business).filter(
            Business.category == key,
            Business.is_chain == False,  # noqa: E712
            Business.website_status == "dead",
        ):
            if b.id not in analyzed_ids:
                st = status_by_domain.get(_domain(b.website))
                session.add(
                    WebsiteAnalysis(
                        business_id=b.id,
                        url=b.website,
                        website_score=0,
                        lead_priority="High Priority Lead",
                        crawl_error=f"Website unreachable: {st.detail if st else 'dead'}",
                    )
                )
                analyzed_ids.add(b.id)

    # Crawl one representative per domain; share results with duplicates.
    by_domain: dict[str, list[tuple[int, str, str]]] = {}
    for bid, url, st in candidates:
        by_domain.setdefault(_domain(url), []).append((bid, url, st))
    targets = [(group[0][0], group[0][1]) for group in by_domain.values()]

    if not targets:
        notify("No new live websites to crawl.")
        return report

    notify(f"Crawling {len(targets)} websites ({len(candidates)} businesses)…")
    crawl_results = crawl_many_sync(targets)

    # --- 6. Lighthouse + scoring, saved per business -----------------------
    lighthouse_ok = run_lighthouse and lh.is_available()
    if run_lighthouse and not lighthouse_ok:
        msg = "Lighthouse unavailable — skipping performance/SEO audits"
        report.errors.append(msg)
        logger.warning(msg)

    for i, (dom, group) in enumerate(by_domain.items(), 1):
        rep_id, rep_url = group[0][0], group[0][1]
        was_blocked = group[0][2] == "blocked"
        crawl = crawl_results.get(rep_id)
        if crawl is None:
            continue

        scores = lh.LighthouseScores()
        if crawl.error:
            report.crawl_failed += 1
        else:
            report.crawled_ok += 1
            if lighthouse_ok:
                notify(f"Auditing {i}/{len(by_domain)}: {dom}")
                scores = lh.run_lighthouse(crawl.final_url or rep_url)
                if scores.error is None:
                    report.audited += 1

        breakdown = compute_score(
            https_enabled=crawl.https_enabled,
            mobile_friendly=crawl.mobile_friendly,
            seo_score=scores.seo_score,
            performance_score=scores.performance_score,
            accessibility_score=scores.accessibility_score,
            best_practices_score=scores.best_practices_score,
            contact_page_found=crawl.contact_page_found,
            meta_description=crawl.meta_description,
            social_links_count=len(crawl.social_links),
            broken_images=crawl.broken_images,
            load_time=crawl.load_time,
            is_outdated=crawl.is_outdated,
            copyright_year=crawl.copyright_year,
        )

        if crawl.error and was_blocked:
            # Site exists but blocks automation — unknown quality, NOT a
            # dead-site lead. Marking it high-priority would be a lie.
            website_score, priority = None, "Unverified"
        elif crawl.error:
            website_score, priority = 0, "High Priority Lead"
        else:
            website_score, priority = breakdown.score, breakdown.priority

        for bid, url, _st in group:
            try:
                with get_session() as session:
                    session.add(
                        WebsiteAnalysis(
                            business_id=bid,
                            url=url,
                            title=crawl.title,
                            meta_description=crawl.meta_description,
                            https_enabled=crawl.https_enabled,
                            load_time=crawl.load_time,
                            image_count=crawl.image_count,
                            broken_images=crawl.broken_images,
                            internal_links=crawl.internal_links,
                            external_links=crawl.external_links,
                            mobile_friendly=crawl.mobile_friendly,
                            contact_page_found=crawl.contact_page_found,
                            emails_found=json.dumps(crawl.emails_found),
                            phones_found=json.dumps(crawl.phones_found),
                            social_links=json.dumps(crawl.social_links),
                            screenshot_path=crawl.screenshot_path,
                            copyright_year=crawl.copyright_year,
                            legacy_signals=json.dumps(crawl.legacy_signals),
                            is_outdated=crawl.is_outdated,
                            performance_score=scores.performance_score,
                            seo_score=scores.seo_score,
                            accessibility_score=scores.accessibility_score,
                            best_practices_score=scores.best_practices_score,
                            website_score=website_score,
                            lead_priority=priority,
                            crawl_error=crawl.error or scores.error,
                        )
                    )
                    biz_row = session.get(Business, bid)
                    # Successful crawl of a "blocked" site upgrades it to live.
                    if was_blocked and not crawl.error:
                        biz_row.website_status = "live"
                    # Contact enrichment: fill missing phone from the website.
                    if biz_row and not biz_row.phone and crawl.phones_found:
                        biz_row.phone = crawl.phones_found[0]
            except Exception as exc:  # noqa: BLE001 - keep processing other sites
                report.errors.append(f"DB save failed for business {bid}: {exc}")
                logger.error("DB save failed for business %s: %s", bid, exc)

    # --- 7. Contact enrichment: fill missing addresses via reverse geocode --
    with get_session() as session:
        missing_addr = (
            session.query(Business)
            .filter(
                Business.category == key,
                Business.is_chain == False,  # noqa: E712
                Business.address.is_(None),
                Business.latitude.isnot(None),
            )
            .limit(30)  # Nominatim is 1 req/sec — cap per scan
            .all()
        )
    if missing_addr:
        notify(f"Filling in {len(missing_addr)} missing addresses…")
        for b in missing_addr:
            formatted = osm_search.reverse_geocode(b.latitude, b.longitude)
            if formatted:
                with get_session() as session:
                    session.get(Business, b.id).address = formatted
                report.addresses_filled += 1

    with get_session() as session:
        report.outdated_found = (
            session.query(WebsiteAnalysis)
            .join(Business, WebsiteAnalysis.business_id == Business.id)
            .filter(Business.category == key, WebsiteAnalysis.is_outdated == True)  # noqa: E712
            .count()
        )
        report.no_website = (
            session.query(Business)
            .filter(
                Business.category == key,
                Business.is_chain == False,  # noqa: E712
                (Business.website.is_(None)) | (Business.website_status == "social_only"),
            )
            .count()
        )

    notify("Scan complete.")
    return report


def verify_missing_websites(progress: Callable[[str], None] | None = None) -> ScanReport:
    """Re-check every business (all categories) that has no website on file.

    Uses Google Places when configured (authoritative), DuckDuckGo otherwise.
    Also retries businesses previously marked 'not_found' — earlier sources
    may have missed sites that Google knows about.
    """
    report = ScanReport()
    notify = progress or (lambda msg: None)
    init_db()

    with get_session() as session:
        # Reset stale 'not_found' verdicts so the better source re-checks them.
        if google_places.is_configured():
            session.query(Business).filter(
                Business.website.is_(None), Business.website_status == "not_found"
            ).update({Business.website_status: None}, synchronize_session=False)

    with get_session() as session:
        missing = (
            session.query(Business)
            .filter(
                Business.is_chain == False,  # noqa: E712
                Business.website.is_(None),
                Business.website_status.is_(None),
            )
            .all()
        )
    if not missing:
        notify("Nothing to verify — every business has a website or a verified no-website status.")
        return report

    notify(f"Verifying {len(missing)} businesses with no website on file…")
    _discover_websites_for(missing, "", "", report, notify)
    notify(
        f"Done: found websites for {report.websites_discovered} businesses; "
        f"{report.social_only} are social-media-only; the rest are verified no-website leads."
    )
    return report


def clear_all_data() -> tuple[int, int]:
    """Delete every business, analysis, and screenshot. Returns row counts."""
    init_db()
    with get_session() as session:
        n_analyses = session.query(WebsiteAnalysis).delete(synchronize_session=False)
        n_businesses = session.query(Business).delete(synchronize_session=False)
    if config.SCREENSHOT_DIR.exists():
        for shot in config.SCREENSHOT_DIR.glob("business_*.png"):
            try:
                shot.unlink()
            except OSError as exc:
                logger.warning("Could not delete %s: %s", shot, exc)
    logger.info("Cleared %d businesses and %d analyses", n_businesses, n_analyses)
    return n_businesses, n_analyses


def _cmd_scan(args: argparse.Namespace) -> int:
    report = run_scan(
        category=args.category,
        city=args.city,
        state=args.state,
        zip_code=args.zip,
        radius_miles=args.radius,
        run_lighthouse=not args.no_lighthouse,
        discover_websites=not args.no_discovery,
        reanalyze=args.reanalyze,
        progress=print,
    )
    print()
    print("=== Scan report ===")
    print(f"Businesses found:      {report.businesses_found} (OSM) + {report.google_found} (Google)")
    print(f"New businesses:        {report.businesses_new}")
    print(f"Chains excluded:       {report.chains_excluded}")
    print(f"No real website:       {report.no_website}  (incl. {report.social_only} social-only)")
    print(f"Websites discovered:   {report.websites_discovered}  (via web search)")
    print(f"Dead websites:         {report.dead_websites}")
    print(f"Bot-blocked websites:  {report.blocked_websites}")
    print(f"Crawled OK:            {report.crawled_ok}")
    print(f"Crawl failures:        {report.crawl_failed}")
    print(f"Lighthouse audits:     {report.audited}")
    print(f"Outdated websites:     {report.outdated_found}")
    print(f"Addresses filled in:   {report.addresses_filled}")
    for err in report.errors:
        print(f"WARNING: {err}")
    return 0 if not report.errors else 1


def _cmd_verify(_args: argparse.Namespace) -> int:
    report = verify_missing_websites(progress=print)
    for err in report.errors:
        print(f"WARNING: {err}")
    return 0


def _cmd_clear(_args: argparse.Namespace) -> int:
    n_biz, n_an = clear_all_data()
    print(f"Cleared {n_biz} businesses and {n_an} analyses (screenshots deleted).")
    return 0


def _cmd_dashboard(_args: argparse.Namespace) -> int:
    """Launch the Streamlit dashboard."""
    dashboard = config.BASE_DIR / "dashboard" / "streamlit_app.py"
    return subprocess.call([sys.executable, "-m", "streamlit", "run", str(dashboard)])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="leadfinder", description="LeadFinder - local business website audit tool")
    sub = parser.add_subparsers(dest="command", required=True)

    scan = sub.add_parser("scan", help="Find and audit businesses in an area")
    scan.add_argument("--category", required=True, help='e.g. "roofers", "dentists"')
    scan.add_argument("--city", default="", help="City name")
    scan.add_argument("--state", default="", help="State, e.g. TX")
    scan.add_argument("--zip", default="", help="ZIP code")
    scan.add_argument("--radius", type=float, default=10.0, help="Radius in miles (default 10)")
    scan.add_argument("--no-lighthouse", action="store_true", help="Skip Lighthouse audits")
    scan.add_argument("--no-discovery", action="store_true", help="Skip web search for missing websites")
    scan.add_argument("--reanalyze", action="store_true", help="Re-analyze already-scanned websites")
    scan.set_defaults(func=_cmd_scan)

    verify = sub.add_parser("verify", help="Re-check websites for all businesses with none on file")
    verify.set_defaults(func=_cmd_verify)

    clear = sub.add_parser("clear", help="Delete all scanned data")
    clear.set_defaults(func=_cmd_clear)

    dash = sub.add_parser("dashboard", help="Launch the Streamlit dashboard")
    dash.set_defaults(func=_cmd_dashboard)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
