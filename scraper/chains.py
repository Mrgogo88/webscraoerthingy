"""Chain / franchise detection.

Big brands (Insomnia Cookies, MINT Dentistry, Great Clips…) are not
local-lead material: their web presence is owned by corporate, not the
location. Three signals mark a business as a chain:

1. OSM `brand` tag on the element (handled at parse time in osm_search)
2. Its name matches a known national/regional chain
3. The same name (or the same website domain across different names)
   appears 3+ times among scanned businesses — a multi-location operation
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

import config
from database.database import get_session
from database.models import Business

logger = config.setup_logging("leadfinder.chains")

# Names appearing this many times (or domains shared this widely) = chain.
MULTI_LOCATION_THRESHOLD = 3

# Known chains, matched as substrings of the normalized business name.
# Focused on categories LeadFinder targets; extend freely.
KNOWN_CHAINS = {
    # food
    "insomnia cookies", "mcdonald", "starbucks", "subway", "dominos",
    "domino s", "pizza hut", "papa john", "chipotle", "dunkin", "wendy s",
    "burger king", "taco bell", "chick fil a", "kfc", "panera",
    "jimmy john", "jersey mike", "whataburger", "sonic drive",
    "little caesars", "wingstop", "raising cane", "in n out", "five guys",
    "crumbl", "nothing bundt", "tropical smoothie", "smoothie king",
    "la la land", "black rifle coffee",
    # dental / medical
    "aspen dental", "mint dentistry", "brident", "western dental",
    "great expressions", "ideal dental", "jefferson dental", "monarch dental",
    "castle dental", "smile brands", "heartland dental", "pacific dental",
    "affordable dentures", "clearchoice", "smile direct",
    "concentra", "carenow", "nextcare", "minuteclinic",
    # hair / fitness / spa
    "sport clips", "supercuts", "great clips", "fantastic sams",
    "massage envy", "european wax", "planet fitness", "la fitness",
    "24 hour fitness", "anytime fitness", "orangetheory", "crunch fitness",
    "gold s gym", "ymca",
    # auto
    "jiffy lube", "valvoline", "midas", "meineke", "firestone", "autonation",
    "goodyear", "brakes plus", "kwik kar", "express oil",
    "discount tire", "pep boys", "aamco", "maaco", "christian brothers auto",
    "take 5", "grease monkey", "caliber collision", "safelite",
    # home services
    "servpro", "servicemaster", "stanley steemer", "merry maids",
    "molly maid", "terminix", "orkin", "mr rooter", "roto rooter",
    "mr electric", "one hour heating", "aire serv", "benjamin franklin plumbing",
    "mr handyman", "college hunks", "two men and a truck", "1 800 got junk",
    # retail / other
    "walgreens", "cvs", "walmart", "target", "costco", "home depot",
    "lowe s", "7 eleven", "dollar general", "dollar tree",
    "re max", "keller williams", "coldwell banker", "century 21",
    "allstate", "state farm", "farmers insurance", "geico",
    "h r block", "jackson hewitt", "liberty tax",
    "banfield", "vca animal", "petco", "petsmart",
    "the ups store", "fedex office", "batteries plus",
}


def normalize_name(name: str) -> str:
    """Lowercase, strip punctuation — 'Domino's Pizza' -> 'domino s pizza'."""
    return re.sub(r"[^a-z0-9]+", " ", name.lower()).strip()


def is_known_chain(name: str) -> bool:
    # Pad with spaces so matches respect word boundaries:
    # "subway" matches " subway " but not " subwayne plumbing ".
    normalized = f" {normalize_name(name)} "
    return any(f" {chain} " in normalized for chain in KNOWN_CHAINS)


def _domain(url: str | None) -> str | None:
    if not url:
        return None
    return urlparse(url).netloc.lower().removeprefix("www.") or None


def mark_chains(category: str | None = None) -> int:
    """Scan the businesses table and set is_chain where the signals say so.

    Returns the number of businesses newly marked. Safe to run repeatedly.
    """
    marked = 0
    with get_session() as session:
        query = session.query(Business)
        if category:
            query = query.filter(Business.category == category)
        businesses = query.all()

        # Count name and domain occurrences across the whole table slice.
        name_counts: dict[str, int] = {}
        domain_names: dict[str, set[str]] = {}
        for b in businesses:
            key = normalize_name(b.name)
            name_counts[key] = name_counts.get(key, 0) + 1
            dom = _domain(b.website)
            if dom:
                domain_names.setdefault(dom, set()).add(key)

        multi_names = {n for n, c in name_counts.items() if c >= MULTI_LOCATION_THRESHOLD}
        # A domain shared by 3+ differently-named businesses = franchise site.
        chain_domains = {
            d for d, names in domain_names.items() if len(names) >= MULTI_LOCATION_THRESHOLD
        }

        for b in businesses:
            if b.is_chain:
                continue
            key = normalize_name(b.name)
            reason = None
            if is_known_chain(b.name):
                reason = "known chain"
            elif key in multi_names:
                reason = f"{name_counts[key]} locations in area"
            elif _domain(b.website) in chain_domains:
                reason = "shared franchise website"
            if reason:
                b.is_chain = True
                marked += 1
                logger.info("Marked chain: %s (%s)", b.name, reason)

    if marked:
        logger.info("Marked %d businesses as chains/franchises", marked)
    return marked
