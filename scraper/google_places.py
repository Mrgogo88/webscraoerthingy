"""Google Places API (New) integration — optional, key-gated.

When GOOGLE_PLACES_API_KEY is configured, scans also pull businesses
straight from Google Maps. This is the authoritative source for "this
business exists on Google but has NO website connected" — exactly the
hottest lead type. The official API is used (scraping Google Maps violates
its ToS and breaks quickly); it has a free monthly quota that comfortably
covers local-area scans.

Docs: https://developers.google.com/maps/documentation/places/web-service/text-search
"""

from __future__ import annotations

import re
import time

import requests

import config
from scraper.osm_search import MILES_TO_METERS, FoundBusiness

logger = config.setup_logging("leadfinder.google")

SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"

# Fields we request per place. websiteUri is the crucial one: when Google
# has no website on file for a listing, the field is simply absent.
FIELD_MASK = ",".join(
    [
        "places.id",
        "places.displayName",
        "places.formattedAddress",
        "places.nationalPhoneNumber",
        "places.websiteUri",
        "places.location",
        "nextPageToken",
    ]
)

MAX_PAGES = 3  # 3 pages x 20 results = up to 60 businesses per scan
_ADDRESS_RE = re.compile(
    r"^(?P<street>[^,]+),\s*(?P<city>[^,]+),\s*(?P<state>[A-Z]{2})\s+(?P<zip>\d{5})"
)


class GooglePlacesError(Exception):
    """Raised when the Places API request fails."""


def is_configured() -> bool:
    return bool(config.GOOGLE_PLACES_API_KEY)


def _parse_address(formatted: str | None) -> dict:
    """Split '123 Main St, Dallas, TX 75231, USA' into components."""
    if not formatted:
        return {"street": None, "city": None, "state": None, "zip": None}
    m = _ADDRESS_RE.match(formatted)
    if not m:
        return {"street": formatted, "city": None, "state": None, "zip": None}
    return {
        "street": m.group("street"),
        "city": m.group("city"),
        "state": m.group("state"),
        "zip": m.group("zip"),
    }


def _parse_place(place: dict, category: str) -> FoundBusiness | None:
    """Convert one Places API result into a FoundBusiness."""
    name = (place.get("displayName") or {}).get("text")
    if not name:
        return None
    addr = _parse_address(place.get("formattedAddress"))
    location = place.get("location") or {}
    return FoundBusiness(
        osm_id=f"google/{place.get('id')}",  # namespaced to avoid OSM collisions
        name=name,
        category=category,
        address=addr["street"],
        city=addr["city"],
        state=addr["state"],
        zip_code=addr["zip"],
        phone=place.get("nationalPhoneNumber"),
        website=place.get("websiteUri"),  # absent => no site on their Google listing
        latitude=location.get("latitude"),
        longitude=location.get("longitude"),
    )


def search_places(
    category: str,
    lat: float,
    lon: float,
    radius_miles: float = 10.0,
    city: str = "",
    state: str = "",
) -> list[FoundBusiness]:
    """Find businesses of a category near a location via Google Places.

    Raises GooglePlacesError on API failures (bad key, quota exhausted…).
    """
    if not is_configured():
        raise GooglePlacesError("GOOGLE_PLACES_API_KEY is not configured")

    where = ", ".join(p for p in (city, state) if p) or "the area"
    body = {
        "textQuery": f"{category} in {where}",
        "pageSize": 20,
        "locationBias": {
            "circle": {
                "center": {"latitude": lat, "longitude": lon},
                "radius": min(radius_miles * MILES_TO_METERS, 50_000),  # API cap
            }
        },
    }
    headers = {
        "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK,
        "Content-Type": "application/json",
    }

    results: list[FoundBusiness] = []
    page_token: str | None = None
    for page in range(MAX_PAGES):
        payload = dict(body)
        if page_token:
            payload["pageToken"] = page_token
        try:
            resp = requests.post(SEARCH_URL, json=payload, headers=headers, timeout=30)
        except requests.RequestException as exc:
            raise GooglePlacesError(f"Places API request failed: {exc}") from exc

        if resp.status_code != 200:
            detail = ""
            try:
                detail = (resp.json().get("error") or {}).get("message", "")[:200]
            except ValueError:
                pass
            raise GooglePlacesError(f"Places API HTTP {resp.status_code}: {detail}")

        data = resp.json()
        for place in data.get("places", []):
            parsed = _parse_place(place, category=category.strip().lower())
            if parsed:
                results.append(parsed)

        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(1)  # tokens need a moment to become valid

    no_site = sum(1 for r in results if not r.website)
    logger.info(
        "Google Places: %d %r businesses (%d with no website on their listing)",
        len(results), category, no_site,
    )
    return results


def lookup_website(
    name: str,
    city: str = "",
    state: str = "",
    lat: float | None = None,
    lon: float | None = None,
) -> str | None:
    """Look up ONE business on Google and return its listed website.

    This is the authoritative website check: if Google's listing has no
    websiteUri, the business really has no site connected. Returns None
    both when no site is listed and when the business isn't found — callers
    treat None as "verified: no website on Google".

    Raises GooglePlacesError on API failures (so outages are never recorded
    as 'no website').
    """
    from scraper.chains import normalize_name  # local import avoids a cycle

    if not is_configured():
        raise GooglePlacesError("GOOGLE_PLACES_API_KEY is not configured")

    query = " ".join(p for p in (name, city, state) if p)
    body: dict = {"textQuery": query, "pageSize": 3}
    if lat is not None and lon is not None:
        body["locationBias"] = {
            "circle": {"center": {"latitude": lat, "longitude": lon}, "radius": 30_000}
        }
    headers = {
        "X-Goog-Api-Key": config.GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": "places.displayName,places.websiteUri",
        "Content-Type": "application/json",
    }
    try:
        resp = requests.post(SEARCH_URL, json=body, headers=headers, timeout=30)
    except requests.RequestException as exc:
        raise GooglePlacesError(f"Places lookup failed: {exc}") from exc
    if resp.status_code != 200:
        detail = ""
        try:
            detail = (resp.json().get("error") or {}).get("message", "")[:200]
        except ValueError:
            pass
        raise GooglePlacesError(f"Places lookup HTTP {resp.status_code}: {detail}")

    target = set(normalize_name(name).split())
    for place in resp.json().get("places", []):
        got_name = (place.get("displayName") or {}).get("text", "")
        got_tokens = set(normalize_name(got_name).split())
        # Guard against Google returning a different business: require
        # meaningful name overlap before trusting the result.
        overlap = len(target & got_tokens) / max(1, len(target))
        if overlap >= 0.5:
            website = place.get("websiteUri")
            logger.info("Google lookup %r -> %r (website=%s)", name, got_name, website or "none")
            return website
    logger.info("Google lookup %r: no matching listing found", name)
    return None
