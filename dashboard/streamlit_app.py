"""LeadFinder Streamlit dashboard (v2 — lead-focused).

Pages:
    Overview        - lead funnel stats and a form to launch new scans
    Leads           - opportunity-ranked lead table, CSV export, clear button
    Business Detail - full audit, screenshot, problems, recommendations
"""

from __future__ import annotations

import sys
from pathlib import Path

# Allow `streamlit run dashboard/streamlit_app.py` from anywhere.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

import dataclasses

import config
from app import ScanReport, clear_all_data, run_scan
from database.database import get_session, init_db
from database.models import Business, WebsiteAnalysis
from scraper.osm_search import available_categories
from scraper.scoring import score_from_analysis

st.set_page_config(page_title="LeadFinder", page_icon="🎯", layout="wide")
init_db()

PRIORITY_COLORS = {
    "High Priority Lead": "🔴",
    "Needs Improvement": "🟠",
    "Average": "🟡",
    "Excellent": "🟢",
    "Unverified": "⚪",
}

# Lead types, hottest first. Opportunity drives the default sort.
LEAD_TYPE_OPPORTUNITY = {
    "No Website": 100,
    "Social Media Only": 95,
    "Dead Website": 90,
    "Outdated Website": 85,
    "Very Bad Website": None,  # 100 - score (score < 40)
    "Bad Website": None,       # 100 - score (40-59)
    "Average Website": None,   # 100 - score (60-79)
    "Good Website": None,      # 100 - score (80+)
    "Unverified (Bot-Blocked)": 50,
    "Website Not Checked Yet": 45,
    "Not Analyzed": 30,
}
HOT_TYPES = ["No Website", "Social Media Only", "Dead Website", "Outdated Website",
             "Very Bad Website", "Bad Website"]
NEEDS_WEBSITE_TYPES = ["No Website", "Social Media Only", "Dead Website"]
TYPE_ICONS = {
    "No Website": "🔥",
    "Social Media Only": "📱",
    "Dead Website": "💀",
    "Outdated Website": "🕰️",
    "Very Bad Website": "🔴",
    "Bad Website": "🟠",
    "Average Website": "🟡",
    "Good Website": "🟢",
    "Unverified (Bot-Blocked)": "⚪",
    "Website Not Checked Yet": "🔍",
    "Not Analyzed": "❔",
}


def _classify_lead(website, status, score, priority, is_outdated=False) -> tuple[str, int]:
    """Return (lead_type, opportunity 0-100) for one business row."""
    if status == "social_only":
        return "Social Media Only", 95
    if not website:
        # Only businesses whose absence was VERIFIED (Google/web search came
        # up empty) count as No Website. Unchecked ones must not pollute the
        # lead list — run a scan or `python app.py verify` to check them.
        if status == "not_found":
            return "No Website", 100
        return "Website Not Checked Yet", 45
    if status == "dead":
        return "Dead Website", 90
    if priority == "Unverified":
        return "Unverified (Bot-Blocked)", 50
    if is_outdated:
        return "Outdated Website", 85
    if score is not None:
        if score < 40:
            return "Very Bad Website", 100 - int(score)
        if score < 60:
            return "Bad Website", 100 - int(score)
        if score < 80:
            return "Average Website", 100 - int(score)
        return "Good Website", max(0, 100 - int(score))
    return "Not Analyzed", 30


# --- Data access -----------------------------------------------------------
@st.cache_data(ttl=30)
def load_data() -> pd.DataFrame:
    """Businesses joined with their analysis as one flat DataFrame."""
    with get_session() as session:
        rows = (
            session.query(Business, WebsiteAnalysis)
            .outerjoin(WebsiteAnalysis, Business.id == WebsiteAnalysis.business_id)
            .all()
        )
    records = []
    for biz, an in rows:
        score = an.website_score if an else None
        priority = an.lead_priority if an else None
        lead_type, opportunity = _classify_lead(
            biz.website, biz.website_status, score, priority,
            is_outdated=bool(an.is_outdated) if an else False,
        )
        records.append(
            {
                "id": biz.id,
                "Business Name": biz.name,
                "Category": biz.category,
                "City": biz.city,
                "State": biz.state,
                "Phone": biz.phone,
                "Address": biz.address,
                "Website": biz.website,
                "Source": biz.website_source,
                "Status": biz.website_status,
                "Website Score": score,
                "SEO Score": an.seo_score if an else None,
                "Performance Score": an.performance_score if an else None,
                "Accessibility": an.accessibility_score if an else None,
                "Lead Type": lead_type,
                "Opportunity": opportunity,
                "Lead Priority": priority or lead_type,
                "Chain": bool(biz.is_chain),
                "Emails": ", ".join(an.emails_list) if an else "",
                "Copyright Year": an.copyright_year if an else None,
            }
        )
    return pd.DataFrame(records)


def get_business(business_id: int) -> tuple[Business | None, WebsiteAnalysis | None]:
    with get_session() as session:
        biz = session.get(Business, business_id)
        an = biz.analysis if biz else None
    return biz, an


# --- Sidebar: filters + navigation -----------------------------------------
st.sidebar.title("🎯 LeadFinder")
page = st.sidebar.radio("Page", ["Overview", "Leads", "Business Detail"])

df = load_data()

st.sidebar.divider()
st.sidebar.subheader("Filters")
local_only = st.sidebar.toggle(
    "🏠 Small local businesses only", value=True,
    help="Hides chains and franchises (Insomnia Cookies, MINT Dentistry, …) "
         "detected via brand tags, a known-chain list, and multi-location patterns.",
)
industries = sorted(df["Category"].dropna().unique()) if not df.empty else []
cities = sorted(df["City"].dropna().unique()) if not df.empty else []
sel_industry = st.sidebar.multiselect("Industry", industries)
sel_city = st.sidebar.multiselect("City", cities)
score_range = st.sidebar.slider("Website score range", 0, 100, (0, 100))

filtered = df.copy()
if local_only and not filtered.empty:
    filtered = filtered[~filtered["Chain"]]
if sel_industry:
    filtered = filtered[filtered["Category"].isin(sel_industry)]
if sel_city:
    filtered = filtered[filtered["City"].isin(sel_city)]
if not filtered.empty and score_range != (0, 100):
    has_score = filtered["Website Score"].notna()
    filtered = filtered[
        ~has_score  # keep no-website leads visible regardless of score filter
        | (
            (filtered["Website Score"] >= score_range[0])
            & (filtered["Website Score"] <= score_range[1])
        )
    ]


# --- Page: Overview ---------------------------------------------------------
def page_overview() -> None:
    st.title("Overview")

    base = df[~df["Chain"]] if not df.empty else df
    if base.empty:
        total = hot = no_site = social = dead = outdated = 0
        avg = None
    else:
        total = len(base)
        hot = int(base["Lead Type"].isin(HOT_TYPES).sum())
        no_site = int((base["Lead Type"] == "No Website").sum())
        social = int((base["Lead Type"] == "Social Media Only").sum())
        dead = int((base["Lead Type"] == "Dead Website").sum())
        outdated = int((base["Lead Type"] == "Outdated Website").sum())
        scored = base["Website Score"].dropna()
        avg = scored.mean() if not scored.empty else None

    c1, c2, c3, c4, c5, c6, c7 = st.columns(7)
    c1.metric("Businesses", total)
    c2.metric("🔥 Hot leads", hot)
    c3.metric("No website", no_site)
    c4.metric("Social-only", social)
    c5.metric("Dead sites", dead)
    c6.metric("🕰️ Outdated", outdated)
    c7.metric("Avg score", f"{avg:.0f}" if avg is not None else "—")

    st.divider()
    st.subheader("Run a new scan")
    with st.form("scan_form"):
        col1, col2, col3 = st.columns(3)
        city = col1.text_input("City", placeholder="Dallas")
        state = col2.text_input("State", placeholder="TX")
        zip_code = col3.text_input("ZIP code (optional)")
        col4, col5, col6 = st.columns(3)
        categories = col4.multiselect("Business categories", available_categories(),
                                       placeholder="Choose one or more")
        select_all = col4.checkbox("All categories")
        custom_category = col5.text_input("…or custom categories (comma-separated)")
        radius = col6.slider("Radius (miles)", 1, 50, 10)
        oc1, oc2, oc3 = st.columns(3)
        use_lighthouse = oc1.checkbox("Lighthouse audits (slower, more accurate)", value=True)
        use_discovery = oc2.checkbox("Web-search missing websites", value=True)
        reanalyze = oc3.checkbox("Re-analyze already-scanned sites", value=False)
        submitted = st.form_submit_button("🔍 Scan", type="primary")

    from scraper.google_places import is_configured as google_configured

    if google_configured():
        st.caption("✅ Google Maps source active — scans also pull businesses from Google, "
                   "including ones with no website on their listing.")
    else:
        st.caption("💡 Want businesses straight from **Google Maps** (including ones with no "
                   "website connected)? Add a free-tier `GOOGLE_PLACES_API_KEY` to your `.env` "
                   "— see README for the 2-minute setup.")

    if submitted:
        chosen_categories = list(available_categories()) if select_all else list(categories)
        if custom_category:
            chosen_categories += [c.strip() for c in custom_category.split(",") if c.strip()]
        seen: set[str] = set()
        final_categories = []
        for c in chosen_categories:
            key = c.lower()
            if key not in seen:
                seen.add(key)
                final_categories.append(c)

        if not final_categories:
            st.error("Pick or type at least one business category.")
        elif not (city or zip_code):
            st.error("Enter at least a city or ZIP code.")
        else:
            n = len(final_categories)
            status = st.status(f"Scanning {n} categor{'y' if n == 1 else 'ies'}…", expanded=True)
            reports = []
            for i, cat in enumerate(final_categories, 1):
                status.write(f"**[{i}/{n}] {cat}**")
                reports.append(run_scan(
                    category=cat, city=city, state=state, zip_code=zip_code,
                    radius_miles=float(radius), run_lighthouse=use_lighthouse,
                    discover_websites=use_discovery, reanalyze=reanalyze,
                    progress=lambda msg: status.write(msg),
                ))
            status.update(label="Scan finished", state="complete")

            report = ScanReport()
            for r in reports:
                for f in dataclasses.fields(report):
                    if f.name == "errors":
                        continue
                    setattr(report, f.name, getattr(report, f.name) + getattr(r, f.name))
                report.errors.extend(r.errors)

            st.success(
                f"Found {report.businesses_found} businesses across {n} categor{'y' if n == 1 else 'ies'} · "
                f"{report.no_website} without a real website · "
                f"{report.dead_websites} dead sites · "
                f"{report.websites_discovered} sites discovered via web search · "
                f"{report.crawled_ok} crawled · {report.audited} audited"
            )
            for err in report.errors:
                st.warning(err)
            load_data.clear()
            st.rerun()

    if not df.empty:
        st.divider()
        st.subheader("Lead breakdown")
        counts = df["Lead Type"].value_counts()
        st.bar_chart(counts)


# --- Page: Leads ------------------------------------------------------------
def page_leads() -> None:
    st.title("Leads")

    top_left, top_right = st.columns([3, 1])
    with top_left:
        st.caption("Ranked by opportunity — businesses with no site, a social-only "
                   "presence, a dead site, or a very bad site come first.")
    with top_right:
        with st.popover("🗑️ Clear data"):
            st.warning("Deletes **all** scanned businesses, audits, and screenshots.")
            confirm = st.checkbox("Yes, I'm sure", key="clear_confirm")
            if st.button("Clear all data", type="primary", disabled=not confirm):
                n_biz, n_an = clear_all_data()
                load_data.clear()
                st.toast(f"Cleared {n_biz} businesses and {n_an} audits.")
                st.rerun()

    if filtered.empty:
        st.info("No businesses match the current filters. Run a scan from the Overview page.")
        return

    view = st.radio(
        "Show me",
        ["🏢 Needs a website", "🕰️ Outdated websites", "🔥 All opportunities", "Everything"],
        horizontal=True,
        help="Needs a website = no site at all, social-media-only, or a dead site. "
             "Outdated = live sites built with pre-2010 techniques or stale copyright years.",
    )

    if view == "🏢 Needs a website":
        table = filtered[filtered["Lead Type"].isin(NEEDS_WEBSITE_TYPES)].copy()
        empty_msg = ("No verified no-website businesses yet — run a scan with web-search "
                     "discovery enabled to confirm which businesses truly have no site.")
    elif view == "🕰️ Outdated websites":
        table = filtered[filtered["Lead Type"] == "Outdated Website"].copy()
        empty_msg = ("No outdated websites detected yet. Era detection runs during crawls — "
                     "re-scan with 'Re-analyze already-scanned sites' checked to analyze "
                     "existing websites for pre-2010 design signals.")
    elif view == "🔥 All opportunities":
        table = filtered[filtered["Lead Type"].isin(HOT_TYPES)].copy()
        empty_msg = "No hot leads match the current filters."
    else:
        table = filtered.copy()
        empty_msg = "No businesses match the current filters. Run a scan from the Overview page."

    unchecked = int((filtered["Lead Type"] == "Website Not Checked Yet").sum())
    if unchecked and view in ("🏢 Needs a website", "🔥 All opportunities"):
        st.caption(
            f"🔍 {unchecked} businesses haven't been website-checked yet and are excluded "
            f"from this view — run a scan (or `python app.py verify`) to check them."
        )

    if table.empty:
        st.info(empty_msg)
        return

    table["Type"] = table["Lead Type"].map(lambda t: f"{TYPE_ICONS.get(t, '')} {t}")

    def _full_address(r) -> str:
        # Reverse-geocoded addresses already contain city/state/zip — only
        # append parts that aren't already present, and drop NaN/None noise.
        base = str(r["Address"]) if pd.notna(r["Address"]) and r["Address"] else ""
        parts = [base] if base else []
        for extra in (r["City"], r["State"]):
            s = str(extra) if pd.notna(extra) and extra else ""
            if s and s.lower() not in base.lower():
                parts.append(s)
        return ", ".join(parts)

    table["Full Address"] = table.apply(_full_address, axis=1)
    table = table.sort_values(["Opportunity", "Business Name"], ascending=[False, True])

    if view == "🏢 Needs a website":
        # Focused view: contact info front and center for outreach.
        columns = ["id", "Business Name", "Type", "Phone", "Full Address", "Website", "Opportunity"]
    elif view == "🕰️ Outdated websites":
        columns = ["id", "Business Name", "Copyright Year", "Phone", "Emails", "Full Address",
                   "Website", "Website Score", "Opportunity"]
    else:
        columns = ["id", "Business Name", "Type", "Opportunity", "Phone", "Emails", "Full Address",
                   "Website", "Website Score", "SEO Score", "Performance Score"]

    st.dataframe(
        table[columns],
        hide_index=True,
        width="stretch",
        column_config={
            "id": st.column_config.NumberColumn("ID", width="small"),
            "Website": st.column_config.LinkColumn("Website"),
            "Full Address": st.column_config.TextColumn("Address", width="large"),
            "Opportunity": st.column_config.ProgressColumn(
                "Opportunity", min_value=0, max_value=100, format="%d"
            ),
            "Website Score": st.column_config.NumberColumn("Site Score"),
        },
    )

    csv = table.drop(columns=["Type"]).to_csv(index=False).encode("utf-8")
    st.download_button("⬇️ Export leads to CSV", csv, "leadfinder_leads.csv", "text/csv")
    st.caption("Note an ID and open the Business Detail page for the full audit.")


# --- Page: Business Detail ---------------------------------------------------
def page_detail() -> None:
    st.title("Business Detail")

    if df.empty:
        st.info("No businesses yet — run a scan first.")
        return

    options = filtered if not filtered.empty else df
    options = options.sort_values("Opportunity", ascending=False)
    labels = {
        int(row.id): f"{row['Business Name']} (#{int(row.id)}) — {row['Lead Type']}"
        for _, row in options.iterrows()
    }
    business_id = st.selectbox(
        "Select a business", list(labels), format_func=lambda i: labels[i]
    )
    biz, an = get_business(int(business_id))
    if biz is None:
        st.error("Business not found.")
        return

    row = df[df["id"] == biz.id].iloc[0]
    lead_type = row["Lead Type"]
    chain_badge = " · 🏪 chain/franchise (excluded from leads)" if row["Chain"] else ""
    st.markdown(f"### {TYPE_ICONS.get(lead_type, '')} {lead_type} · opportunity {row['Opportunity']}/100{chain_badge}")

    left, right = st.columns([1, 1])
    with left:
        st.subheader(biz.name)
        st.write(f"**Category:** {biz.category or '—'}")
        address = ", ".join(p for p in [biz.address, biz.city, biz.state, biz.zip_code] if p)
        st.write(f"**Address:** {address or '—'}")
        st.write(f"**Phone:** {biz.phone or '—'}")
        if biz.website:
            source_note = " *(found via web search — verify before outreach)*" \
                if biz.website_source == "discovered" else ""
            st.write(f"**Website:** [{biz.website}]({biz.website}){source_note}")
            status_labels = {
                "live": "🟢 Live", "dead": "💀 Dead / unreachable",
                "blocked": "⚪ Bot-blocked (couldn't audit)",
                "social_only": "📱 Social media page only",
            }
            st.write(f"**Status:** {status_labels.get(biz.website_status, biz.website_status or '—')}")
        else:
            st.error("**No website found anywhere** — verified by web search. "
                     "This is a prime prospect for a new site.")
        if biz.latitude and biz.longitude:
            st.map(pd.DataFrame({"lat": [biz.latitude], "lon": [biz.longitude]}), zoom=12)

    with right:
        if an and an.screenshot_path:
            shot = config.BASE_DIR / an.screenshot_path
            if shot.exists():
                st.image(str(shot), caption="Homepage screenshot", width="stretch")

    if an is None:
        if biz.website and biz.website_status in ("live", "blocked"):
            st.info("Website not audited yet — run a scan for this category.")
        return

    st.divider()
    st.subheader("Website audit")
    if an.crawl_error:
        st.error(f"Crawl issue: {an.crawl_error}")

    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Website Score", an.website_score if an.website_score is not None else "—")
    m2.metric("SEO", an.seo_score if an.seo_score is not None else "—")
    m3.metric("Performance", an.performance_score if an.performance_score is not None else "—")
    m4.metric("Accessibility", an.accessibility_score if an.accessibility_score is not None else "—")
    m5.metric("Best Practices", an.best_practices_score if an.best_practices_score is not None else "—")

    priority = an.lead_priority or "—"
    st.write(f"**Lead priority:** {PRIORITY_COLORS.get(priority, '⚪')} {priority}")

    facts = {
        "HTTPS": "✅" if an.https_enabled else "❌",
        "Mobile viewport": "✅" if an.mobile_friendly else "❌",
        "Contact page": "✅" if an.contact_page_found else "❌",
        "Load time": f"{an.load_time:.1f}s" if an.load_time else "—",
        "Images (broken)": f"{an.image_count or 0} ({an.broken_images or 0})",
        "Links int/ext": f"{an.internal_links or 0}/{an.external_links or 0}",
        "Emails found": ", ".join(an.emails_list) or "—",
        "Phones found": ", ".join(an.phones_list) or "—",
        "Social profiles": ", ".join(an.social_links_list) or "—",
        "Copyright year": str(an.copyright_year) if an.copyright_year else "—",
    }
    st.table(pd.DataFrame(facts.items(), columns=["Check", "Result"]))

    if an.is_outdated or an.legacy_signals_list:
        st.subheader("🕰️ Design-era analysis")
        if an.is_outdated:
            st.error("This website appears to be **badly outdated** — built with "
                     "pre-2010 techniques and likely never redesigned.")
        for signal in an.legacy_signals_list:
            st.markdown(f"- 🧓 {signal}")

    breakdown = score_from_analysis(an)
    c1, c2 = st.columns(2)
    with c1:
        st.subheader("Problems found")
        if an.crawl_error and an.website_score == 0:
            st.markdown("- ⚠️ The listed website does not work at all — "
                        "customers clicking it hit a dead end.")
        elif breakdown.problems:
            for p in breakdown.problems:
                st.markdown(f"- ⚠️ {p}")
        else:
            st.success("No significant problems detected.")
    with c2:
        st.subheader("Recommended improvements")
        if an.crawl_error and an.website_score == 0:
            st.markdown("- 💡 Build a fresh website — anything live beats a broken link.")
        elif breakdown.recommendations:
            for r in breakdown.recommendations:
                st.markdown(f"- 💡 {r}")
        else:
            st.write("Nothing major — this site is in good shape.")


PAGES = {"Overview": page_overview, "Leads": page_leads, "Business Detail": page_detail}
PAGES[page]()
