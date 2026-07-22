"""Business discovery via OpenStreetMap (Nominatim geocoding + Overpass search).

Both services are free and require no API key. Nominatim's usage policy
requires an identifying User-Agent and max ~1 request/second, which this
module respects.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import requests

import config

logger = config.setup_logging("leadfinder.osm")

MILES_TO_METERS = 1609.34

# Public Overpass instances, tried in order. The main instance is often
# overloaded (504s), so we fall back to community mirrors.
OVERPASS_MIRRORS = [
    config.OVERPASS_URL,
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
]

# Map user-friendly category names to OSM tag filters.
# Each entry is a list of (key, value) tag pairs queried with OR semantics.
# Multiple tags per category widen coverage: the same business type is
# tagged inconsistently across OSM.
CATEGORY_TAGS: dict[str, list[tuple[str, str]]] = {
    "roofing companies": [("craft", "roofer"), ("shop", "roofing")],
    "roofers": [("craft", "roofer"), ("shop", "roofing")],
    "dentists": [("amenity", "dentist"), ("healthcare", "dentist")],
    "doctors": [("amenity", "doctors"), ("healthcare", "doctor")],
    "hvac companies": [("craft", "hvac"), ("shop", "hvac")],
    "hvac": [("craft", "hvac"), ("shop", "hvac")],
    "restaurants": [("amenity", "restaurant"), ("amenity", "fast_food")],
    "law firms": [("office", "lawyer"), ("office", "notary")],
    "lawyers": [("office", "lawyer")],
    "electricians": [("craft", "electrician"), ("shop", "electrical")],
    "plumbers": [("craft", "plumber"), ("shop", "plumbing")],
    "landscapers": [("craft", "gardener"), ("shop", "garden_centre")],
    "car repair": [("shop", "car_repair")],
    "auto repair": [("shop", "car_repair"), ("shop", "tyres")],
    "real estate": [("office", "estate_agent")],
    "insurance agents": [("office", "insurance")],
    "accountants": [("office", "accountant"), ("office", "tax_advisor")],
    "gyms": [("leisure", "fitness_centre"), ("club", "sport")],
    "hair salons": [("shop", "hairdresser"), ("shop", "beauty")],
    "veterinarians": [("amenity", "veterinary")],
    "chiropractors": [("healthcare", "chiropractor"), ("office", "chiropractor")],
    "cafes": [("amenity", "cafe"), ("shop", "coffee")],
    "bakeries": [("shop", "bakery"), ("shop", "pastry")],
    "cleaning services": [("craft", "cleaning"), ("shop", "dry_cleaning")],
    "painters": [("craft", "painter")],
    "pest control": [("craft", "pest_control"), ("office", "pest_control")],
    "photographers": [("craft", "photographer"), ("shop", "photo")],
    "florists": [("shop", "florist")],
    "pet groomers": [("shop", "pet_grooming"), ("shop", "pet")],
    "physical therapists": [("healthcare", "physiotherapist")],
    "optometrists": [("healthcare", "optometrist"), ("shop", "optician")],
    "daycares": [("amenity", "childcare"), ("amenity", "kindergarten")],
    "tattoo shops": [("shop", "tattoo")],
    "barbers": [("shop", "hairdresser")],
    "moving companies": [("office", "moving_company")],
    "towing": [("service", "towing"), ("shop", "car_repair")],
}

# Keywords used for the name-based fallback search on custom categories:
# maps a category word to what would appear in a business *name*.
_NAME_SEARCH_STRIP = ("companies", "company", "services", "service", "shops", "shop", "firms", "firm")


class GeocodingError(Exception):
    """Raised when a location cannot be resolved to coordinates."""


class OverpassError(Exception):
    """Raised when the Overpass API request fails."""


@dataclass
class FoundBusiness:
    """A business as returned from OpenStreetMap, before DB insertion."""

    osm_id: str
    name: str
    category: str
    address: str | None
    city: str | None
    state: str | None
    zip_code: str | None
    phone: str | None
    website: str | None
    latitude: float | None
    longitude: float | None
    is_chain: bool = False


def available_categories() -> list[str]:
    return sorted(CATEGORY_TAGS)


def geocode(city: str = "", state: str = "", zip_code: str = "") -> tuple[float, float]:
    """Resolve a location to (lat, lon) using Nominatim.

    Any combination of city/state/zip works; at least one must be provided.
    """
    query = ", ".join(part for part in (city, state, zip_code) if part.strip())
    if not query:
        raise GeocodingError("Provide at least a city, state, or ZIP code.")

    params = {"q": f"{query}, USA", "format": "json", "limit": 1}
    headers = {"User-Agent": config.OSM_USER_AGENT}
    try:
        resp = requests.get(config.NOMINATIM_URL, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        results = resp.json()
    except requests.RequestException as exc:
        raise GeocodingError(f"Nominatim request failed: {exc}") from exc

    if not results:
        raise GeocodingError(f"Could not geocode location: {query!r}")

    lat, lon = float(results[0]["lat"]), float(results[0]["lon"])
    logger.info("Geocoded %r -> (%.5f, %.5f)", query, lat, lon)
    # Respect Nominatim's 1 req/sec policy in case of back-to-back calls.
    time.sleep(1)
    return lat, lon


def reverse_geocode(lat: float, lon: float) -> str | None:
    """Resolve coordinates to a street address via Nominatim.

    Used to fill in addresses for businesses whose OSM entry has coordinates
    but no addr:* tags — so every lead has contact info. Returns None on
    failure (never raises).
    """
    url = config.NOMINATIM_URL.replace("/search", "/reverse")
    params = {"lat": lat, "lon": lon, "format": "json", "zoom": 18}
    headers = {"User-Agent": config.OSM_USER_AGENT}
    try:
        resp = requests.get(url, params=params, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except (requests.RequestException, ValueError) as exc:
        logger.warning("Reverse geocode failed for (%s, %s): %s", lat, lon, exc)
        return None
    finally:
        time.sleep(1)  # Nominatim usage policy: max 1 req/sec

    addr = data.get("address", {})
    street = " ".join(p for p in [addr.get("house_number"), addr.get("road")] if p)
    city = addr.get("city") or addr.get("town") or addr.get("suburb")
    parts = [street or None, city, addr.get("state"), addr.get("postcode")]
    formatted = ", ".join(p for p in parts if p)
    return formatted or None


def _build_overpass_query(tags: list[tuple[str, str]], lat: float, lon: float, radius_m: int) -> str:
    """Build an Overpass QL query for nodes/ways/relations matching any tag pair."""
    clauses = []
    for key, value in tags:
        for element in ("node", "way", "relation"):
            clauses.append(f'{element}["{key}"="{value}"](around:{radius_m},{lat},{lon});')
    body = "\n  ".join(clauses)
    return f"""[out:json][timeout:60];
(
  {body}
);
out center tags;"""


def _build_name_query(name_terms: list[str], lat: float, lon: float, radius_m: int) -> str:
    """Build a business-name regex query (catches businesses whose category
    tag is missing but whose name says what they do, e.g. "Smith Roofing
    LLC" tagged only as office=company).

    Kept separate from the tag query: name regexes are expensive server-side
    and can time out under load — the caller treats this query as optional.
    """
    clauses = []
    for term in name_terms:
        safe = term.replace('"', "").replace("\\", "")
        clauses.append(f'nwr["name"~"{safe}",i](around:{radius_m},{lat},{lon});')
    body = "\n  ".join(clauses)
    return f"""[out:json][timeout:30];
(
  {body}
);
out center tags;"""


def _name_search_terms(category: str) -> list[str]:
    """Derive business-name search words from a category, e.g.
    'roofing companies' -> ['roofing'], 'dentists' -> ['dentist', 'dental'].
    """
    words = [w for w in category.lower().split() if w not in _NAME_SEARCH_STRIP]
    terms = []
    for w in words:
        base = w.rstrip("s") if len(w) > 4 else w
        terms.append(base)
    # Common noun variants that appear in business names
    extras = {"dentist": "dental", "roofer": "roofing", "plumber": "plumbing",
              "electrician": "electric", "lawyer": "law"}
    for t in list(terms):
        if t in extras:
            terms.append(extras[t])
    return terms[:3]


# Tag keys that identify an element as a business (vs. a street/building/park
# that merely has a matching name).
_BUSINESS_TAG_KEYS = ("shop", "craft", "office", "amenity", "healthcare", "leisure", "club")
# Tag keys that identify an element as definitely NOT a business.
_NON_BUSINESS_TAG_KEYS = ("highway", "railway", "landuse", "natural", "boundary", "waterway", "place")


def _looks_like_business(tags: dict) -> bool:
    """Client-side filter for name-regex matches: keep businesses, drop
    streets/areas. Elements with contact info are kept even without a
    category tag — a named POI with a phone number is a business."""
    if any(k in tags for k in _NON_BUSINESS_TAG_KEYS):
        return False
    if any(k in tags for k in _BUSINESS_TAG_KEYS):
        return True
    return any(k in tags for k in ("phone", "contact:phone", "website", "contact:website", "addr:housenumber"))


def _parse_element(element: dict, category: str) -> FoundBusiness | None:
    """Convert one Overpass element into a FoundBusiness. Returns None if unusable."""
    tags = element.get("tags", {})
    name = tags.get("name")
    if not name:
        return None  # unnamed POIs are not actionable leads
    if not _looks_like_business(tags):
        return None

    # Ways/relations return a "center"; nodes have lat/lon directly.
    lat = element.get("lat") or element.get("center", {}).get("lat")
    lon = element.get("lon") or element.get("center", {}).get("lon")

    # Assemble a street address from OSM addr:* tags when present.
    parts = [tags.get("addr:housenumber"), tags.get("addr:street")]
    street = " ".join(p for p in parts if p) or None

    website = tags.get("website") or tags.get("contact:website") or tags.get("url")
    if website and not website.lower().startswith(("http://", "https://")):
        website = "https://" + website

    return FoundBusiness(
        osm_id=f"{element.get('type')}/{element.get('id')}",
        name=name,
        category=category,
        # OSM tags branded locations explicitly — a strong chain signal.
        is_chain="brand" in tags or "brand:wikidata" in tags,
        address=street,
        city=tags.get("addr:city"),
        state=tags.get("addr:state"),
        zip_code=tags.get("addr:postcode"),
        phone=tags.get("phone") or tags.get("contact:phone"),
        website=website,
        latitude=float(lat) if lat is not None else None,
        longitude=float(lon) if lon is not None else None,
    )


def _query_overpass(query: str, client_timeout: int = 90, rounds: int = 2) -> dict:
    """POST a query to Overpass, trying each mirror until one succeeds.

    Makes `rounds` passes over the mirror list (with a pause between rounds)
    because public instances rate-limit and recover within seconds.
    """
    last_error: Exception | None = None
    mirrors = OVERPASS_MIRRORS * rounds
    for attempt, url in enumerate(mirrors):
        if attempt and attempt % len(OVERPASS_MIRRORS) == 0:
            time.sleep(5)  # brief cool-down before the next round
        try:
            resp = requests.post(
                url,
                data={"data": query},
                headers={"User-Agent": config.OSM_USER_AGENT},
                timeout=client_timeout,
            )
            resp.raise_for_status()
            payload = resp.json()
            # Overpass can return HTTP 200 with a "remark" when the query
            # timed out server-side — the element list is then empty or
            # silently truncated. Either way, retry on the next mirror.
            remark = payload.get("remark", "")
            if "timed out" in remark or "error" in remark.lower():
                raise OverpassError(f"Server-side failure (partial result): {remark[:200]}")
            return payload
        except (requests.RequestException, ValueError, OverpassError) as exc:
            logger.warning("Overpass mirror %s failed: %s", url, exc)
            last_error = exc
    raise OverpassError(f"All Overpass mirrors failed. Last error: {last_error}") from last_error


def search_businesses(
    category: str,
    city: str = "",
    state: str = "",
    zip_code: str = "",
    radius_miles: float = 10.0,
    coords: tuple[float, float] | None = None,
) -> list[FoundBusiness]:
    """Find businesses of a category near a location.

    Pass `coords` (lat, lon) to skip geocoding when already resolved.
    Raises GeocodingError or OverpassError on service failures.
    """
    key = category.strip().lower()
    tags = CATEGORY_TAGS.get(key)
    if tags is None:
        # Fallback: search common business tag keys for the raw term so
        # unlisted categories still return something useful.
        logger.warning("Unknown category %r; falling back to generic tag search", category)
        tags = [("shop", key), ("craft", key), ("amenity", key), ("office", key)]

    lat, lon = coords if coords else geocode(city=city, state=state, zip_code=zip_code)
    radius_m = int(radius_miles * MILES_TO_METERS)

    # Primary query: indexed tag search — fast and reliable.
    payload = _query_overpass(_build_overpass_query(tags, lat, lon, radius_m))
    elements = list(payload.get("elements", []))

    # Bonus query: business-name regex search widens coverage (businesses
    # with missing category tags). It is expensive server-side, so a failure
    # here degrades gracefully instead of failing the scan.
    name_terms = _name_search_terms(key)
    if name_terms:
        try:
            name_payload = _query_overpass(
                _build_name_query(name_terms, lat, lon, radius_m),
                client_timeout=45, rounds=1,  # optional query: fail fast
            )
            elements.extend(name_payload.get("elements", []))
        except OverpassError as exc:
            logger.warning("Name-based search skipped (server busy): %s", exc)

    businesses: list[FoundBusiness] = []
    seen_ids: set[str] = set()
    for element in elements:
        parsed = _parse_element(element, category=key)
        if parsed and parsed.osm_id not in seen_ids:
            seen_ids.add(parsed.osm_id)
            businesses.append(parsed)

    logger.info(
        "Found %d %r businesses within %.1f miles of (%.4f, %.4f)",
        len(businesses), key, radius_miles, lat, lon,
    )
    return businesses


def save_businesses(businesses: list[FoundBusiness], city: str = "", state: str = "") -> int:
    """Insert found businesses into the DB, skipping duplicates by osm_id.

    Fills in city/state from the search parameters when OSM lacks addr tags.
    Returns the number of newly inserted rows.
    """
    from database.database import get_session, init_db
    from database.models import Business

    from scraper.chains import normalize_name

    init_db()
    inserted = 0
    with get_session() as session:
        existing_ids = {
            row[0] for row in session.query(Business.osm_id).filter(Business.osm_id.isnot(None))
        }
        # Cross-source dedupe: the same business often exists in both OSM and
        # Google Places under different ids — match on normalized name + area.
        existing_names = {
            (normalize_name(n), z or c)
            for n, z, c in session.query(Business.name, Business.zip_code, Business.city)
        }
        for fb in businesses:
            if fb.osm_id in existing_ids:
                continue
            name_key = (normalize_name(fb.name), fb.zip_code or fb.city or city or None)
            if name_key in existing_names:
                continue
            source = fb.osm_id.split("/")[0] if fb.osm_id else None  # 'google' or OSM element type
            session.add(
                Business(
                    osm_id=fb.osm_id,
                    name=fb.name,
                    category=fb.category,
                    address=fb.address,
                    city=fb.city or (city or None),
                    state=fb.state or (state or None),
                    zip_code=fb.zip_code,
                    phone=fb.phone,
                    website=fb.website,
                    website_source=("google" if source == "google" else "osm") if fb.website else None,
                    latitude=fb.latitude,
                    longitude=fb.longitude,
                    is_chain=fb.is_chain,
                )
            )
            existing_ids.add(fb.osm_id)
            existing_names.add(name_key)
            inserted += 1
    logger.info("Saved %d new businesses (skipped %d duplicates)", inserted, len(businesses) - inserted)
    return inserted
