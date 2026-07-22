# 🎯 LeadFinder

An AI-ready local business website audit tool. LeadFinder discovers local
businesses in any US area, crawls and audits their websites, scores their
online presence 0–100, and surfaces the businesses that most need a website
redesign — your best prospects.

Built entirely on **free, open-source services** — no paid APIs.

The core question LeadFinder answers: **which local businesses have no
website, a dead website, or a website so bad it costs them customers?**

## How it works

```
City/ZIP + category + radius
        │
        ▼
 ┌────────────────────────┐
 │ OpenStreetMap search   │  tag search + business-name search
 │ (Nominatim + Overpass) │  across 3 failover mirrors
 └────────────────────────┘
        │
        ▼
 ┌────────────────────────┐
 │ Website triage         │  • "website" that's a Facebook page → social-only lead
 │                        │  • listed site probed: live / dead / bot-blocked
 │                        │  • no site listed? free web search finds + verifies
 │                        │    the real site (or confirms there isn't one)
 └────────────────────────┘
        │  (only live sites, deduped by domain)
        ▼
 ┌─────────────────┐   ┌──────────────────┐
 │ Playwright      │──▶│ Lighthouse CLI   │
 │ homepage crawl  │   │ perf / SEO /     │
 │ + screenshot    │   │ a11y / practices │
 └─────────────────┘   └──────────────────┘
        │
        ▼
  Quality score (0–100) + lead type + opportunity rank
        │
        ▼
  Streamlit dashboard (hot leads first, CSV export)
```

### Lead types (hottest first)

| Lead type | Meaning |
|---|---|
| 🔥 No Website | No site listed anywhere, confirmed by web search |
| 📱 Social Media Only | Their "website" is a Facebook/Instagram/Linktree page |
| 💀 Dead Website | Listed site 404s, won't resolve, or times out (verified) |
| 🔴 Very Bad Website | Live but scores < 40/100 |
| 🟠 Bad Website | Scores 40–59 |
| 🟡 / 🟢 Average / Good | Scores 60+ — not a priority |
| ⚪ Unverified | Site blocks bots — quality unknown, **not** counted as a lead |

## Requirements

- Python 3.12
- Node.js 18+ (for the Lighthouse CLI)

## Setup

```bash
# 1. Python dependencies
pip install -r requirements.txt

# 2. Chromium for crawling (also used by Lighthouse — no Chrome install needed)
python -m playwright install chromium --with-deps

# 3. Lighthouse CLI (local install, no global pollution)
npm install lighthouse

# 4. Configuration (optional — defaults work out of the box)
cp .env.example .env
```

## Usage

### Scan from the command line

```bash
python app.py scan --category roofers --city Plano --state TX --radius 10
```

Options:

| Flag | Description | Default |
|---|---|---|
| `--category` | Business type (see list below) | required |
| `--city` / `--state` / `--zip` | Location (at least city or ZIP) | — |
| `--radius` | Search radius in miles | 10 |
| `--no-lighthouse` | Skip Lighthouse audits (much faster) | off |
| `--no-discovery` | Skip web search for missing websites | off |
| `--reanalyze` | Re-audit websites already analyzed | off |

Other commands:

```bash
python app.py clear        # delete all scanned data (also a button in the dashboard)
```

### Launch the dashboard

```bash
python app.py dashboard
# or: streamlit run dashboard/streamlit_app.py
```

Dashboard pages:

- **Overview** — lead funnel stats (hot leads, no-website, social-only, dead
  sites) and a form to run new scans
- **Leads** — opportunity-ranked table with a 🏢 **no-website-only checkbox**
  (businesses listed with no real site connected, address + phone shown for
  outreach), a 🔥 hot-leads-only toggle, lead-type filters, CSV export, and
  a clear-all-data button
- **Business Detail** — full audit, homepage screenshot, problems found, and
  recommended improvements per business

The sidebar's 🏠 **Small local businesses only** toggle (on by default)
hides chains and franchises — Insomnia Cookies, MINT Dentistry, Great
Clips and the like are detected via OSM brand tags, a known-chain list
(`scraper/chains.py`, easy to extend), and multi-location patterns (same
name or same website domain appearing 3+ times).

## Google Maps as a source (optional, recommended)

Google's business data is far richer than OpenStreetMap's. LeadFinder can
pull businesses straight from Google Maps — including businesses whose
Google listing has **no website connected**, your hottest lead type — via
the official Places API (scraping Google Maps violates its ToS, so a key
is required; the API has a free monthly quota that covers regular use).

Setup (~2 minutes):

1. Go to [console.cloud.google.com](https://console.cloud.google.com), create a project
2. Enable **Places API (New)** and create an API key (billing account
   required for the free quota; you are not charged within it)
3. Add to `.env`: `GOOGLE_PLACES_API_KEY=your-key`

Scans then automatically merge Google results with OSM (deduplicated by
name + location), and businesses without a `websiteUri` go straight into
the no-website lead flow for verification.

### Supported categories

roofers, dentists, doctors, hvac, restaurants, law firms, electricians,
plumbers, landscapers, auto repair, real estate, insurance agents,
accountants, gyms, hair salons, barbers, veterinarians, chiropractors,
cafes, bakeries, cleaning services, painters, pest control, photographers,
florists, pet groomers, physical therapists, optometrists, daycares,
tattoo shops, moving companies, towing — plus any custom term (falls back
to a generic OpenStreetMap tag + business-name search).

## Scoring model

Each analyzed website gets a 0–100 score:

| Criterion | Points |
|---|---|
| HTTPS enabled | +10 (−20 if missing) |
| Mobile viewport tag | +20 (−20 if missing) |
| Lighthouse SEO | up to +20 |
| Lighthouse Performance | up to +20 |
| Lighthouse Accessibility | up to +10 |
| Contact page found | +10 |
| Modern indicators (meta description, social links, best practices ≥ 80) | up to +10 |
| Broken images | −10 |
| Load time > 8s | −10 |

Classification: **80–100** Excellent · **60–79** Average · **40–59** Needs
Improvement · **0–39** High Priority Lead. Sites that fail to load at all
score 0 (the strongest lead of all).

## Project structure

```
├── app.py                     # pipeline orchestrator + CLI
├── config.py                  # env-driven configuration & logging
├── database/
│   ├── database.py            # engine, sessions, init
│   └── models.py              # Business, WebsiteAnalysis ORM models
├── scraper/
│   ├── osm_search.py          # Nominatim geocoding + Overpass business search
│   ├── crawler.py             # async Playwright homepage crawler
│   ├── lighthouse.py          # Lighthouse CLI wrapper
│   └── scoring.py             # 0-100 quality score + lead classification
├── dashboard/
│   └── streamlit_app.py       # 3-page Streamlit dashboard
├── prompts/
│   └── website_analysis_prompt.md  # Phase 2 AI audit prompt template
├── data/
│   ├── leads.db               # SQLite database (generated)
│   └── screenshots/           # homepage screenshots (generated)
└── tests/                     # pytest suite (offline, no network needed)
```

## Testing

```bash
python -m pytest tests/ -v
```

The suite covers the database models, OSM response parsing, HTML signal
extraction, and the scoring algorithm — all offline with no network calls.

## Deployment

LeadFinder **cannot run on serverless hosts like Vercel.** It needs a
persistent process (Streamlit's WebSocket connection), a real browser
(Playwright's headless Chromium for crawling), a Node CLI shell-out
(Lighthouse), and a writable disk (the SQLite database + screenshots) —
none of which fit a stateless, short-lived function. Use a container host
instead. A `Dockerfile` is included and works on any of these:

**Render**
1. Push this repo to GitHub/GitLab and connect it in the Render dashboard
   as a Blueprint (`render.yaml` is already set up with a `/data` disk).
2. Set `GOOGLE_PLACES_API_KEY` in the service's environment if you're using
   it (optional).
3. **Persistent disks require a paid instance type** (Starter or above) —
   Render's free tier has no disk support, so the leads database would be
   wiped on every restart/deploy on the free plan.

**Railway**
1. Push this repo and create a new project from it — Railway auto-detects
   the `Dockerfile` (`railway.json` pins the build to it explicitly).
2. Attach a **Volume** (Railway dashboard → your service → Volumes) mounted
   at `/data` so the database and screenshots survive restarts/redeploys.
   Railway's volumes work on its free trial tier, unlike Render's disks.
3. Set `GOOGLE_PLACES_API_KEY` as a service variable if needed.

**Any Docker host (Fly.io, a VPS, etc.)**
```bash
docker build -t leadfinder .
docker run -p 8501:8501 -v leadfinder_data:/data \
    -e GOOGLE_PLACES_API_KEY=your-key-if-any \
    leadfinder
```
Mount a volume at `/data` the same way — that's where `DATABASE_PATH`,
`SCREENSHOT_DIR`, and `LOG_FILE` all point by default inside the container
(see the `ENV` lines in the `Dockerfile`).

## Data accuracy — what to expect

OpenStreetMap is community-maintained: coverage is thinner than Google
Maps and website tags are often missing or stale. LeadFinder compensates:

- **Business-name search** finds businesses whose OSM category tag is
  missing ("Smith Roofing LLC" tagged only as a generic office).
- **Website discovery** runs a free web search for every business without a
  listed site, confirms candidates by checking the business name appears on
  the page, and only then records the site. "No Website" therefore means
  *verified absent*, not "OSM didn't know".
- **Website verification** probes every listed URL before trusting it —
  OSM's dead links become Dead Website leads instead of false audits.
- **Bot-blocked sites** are labeled Unverified rather than being falsely
  reported as broken.

Businesses that exist on Google but not in OSM at all still won't appear —
that gap requires the paid Google Places API (a natural Phase 2 add-on).

## Reliability notes

- **Overpass mirrors**: the search fails over across three public
  instances, with two retry rounds and detection of silent server-side
  timeouts. The optional name search degrades gracefully under load.
- **Nominatim / DuckDuckGo policies**: identifying User-Agent, ~1 req/sec
  rate limiting, and a 30-lookup discovery cap per scan.
- **Fault isolation**: a website that times out, 404s, or doesn't resolve is
  recorded with its error and scored 0 — it never crashes a batch. Crawls
  run concurrently (default 3 at a time) with per-site timeouts. Duplicate
  listings sharing one domain are crawled once and share the audit.

## Phase 2 roadmap

- AI-generated audits using `prompts/website_analysis_prompt.md`
- Redesign mockup generation
- Outreach email/call-script generation
- Lightweight CRM (lead status, notes, follow-ups)
