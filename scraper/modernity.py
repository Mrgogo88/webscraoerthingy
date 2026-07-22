"""Website era / modernity detection.

Answers: "does this site look like it was built before ~2010 and never
redesigned?" — sites like that are prime redesign leads even though they
technically work.

Pure function over the rendered HTML so it is trivially testable:
    analyze_html(html) -> EraSignals

Signals (each explainable, shown in the dashboard):
    legacy  - <font>/<center>/<marquee> tags, framesets, Flash embeds,
              bgcolor attributes, table-based layout, FrontPage/Dreamweaver
              generator tags, ancient jQuery, HTML4/XHTML or missing doctype,
              no viewport meta
    modern  - HTML5 doctype, semantic tags, modern builders (Wix,
              Squarespace, WordPress themes, React/Next), webp images
    year    - latest copyright year found in the page footer/text
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime

# A site whose newest copyright year is older than this (and shows no modern
# markers) is considered abandoned/outdated.
STALE_YEAR_CUTOFF = 2015


@dataclass
class EraSignals:
    copyright_year: int | None = None
    legacy_signals: list[str] = field(default_factory=list)
    modern_signals: list[str] = field(default_factory=list)
    is_outdated: bool = False


# (needle in lowercased html, human-readable signal). Hard signals are
# technologies nobody has shipped since ~2010.
_LEGACY_MARKERS = [
    ("<frameset", "frameset layout (pre-2005 technique)"),
    ("<font ", "<font> tags (obsolete since ~2008)"),
    ("<marquee", "<marquee> tag"),
    ("<blink", "<blink> tag"),
    (".swf", "Adobe Flash content (dead since 2020)"),
    ("microsoft frontpage", "built with Microsoft FrontPage"),
    ("macromedia dreamweaver", "built with Macromedia Dreamweaver"),
    ("bgcolor=", "bgcolor attributes (pre-CSS styling)"),
    ("cellpadding=", "table cellpadding layout"),
    ("jquery-1.", "ancient jQuery 1.x"),
    ("jquery/1.", "ancient jQuery 1.x"),
    ("prototype.js", "Prototype.js library (~2007)"),
    ("scriptaculous", "script.aculo.us library (~2007)"),
]

_MODERN_MARKERS = [
    ("wp-content/themes", "WordPress theme"),
    ("wix.com", "Wix builder"),
    ("squarespace", "Squarespace builder"),
    ("shopify", "Shopify"),
    ("webflow", "Webflow builder"),
    ("data-reactroot", "React"),
    ("__next", "Next.js"),
    ("data-v-", "Vue.js"),
    (".webp", "WebP images"),
    ("tailwind", "Tailwind CSS"),
    ("<nav", "HTML5 semantic tags"),
    ("<article", "HTML5 semantic tags"),
    ("srcset=", "responsive images"),
]

# "© 2009", "Copyright 2004-2011", "&copy; 1998" — capture every year in a
# copyright context, use the newest.
_COPYRIGHT_RE = re.compile(
    r"(?:©|&copy;|\bcopyright\b)[\s:]*(?:\d{4}\s*[-–]\s*)?((?:19|20)\d{2})",
    re.IGNORECASE,
)
_YEAR_RANGE_RE = re.compile(
    r"(?:©|&copy;|\bcopyright\b)[\s:]*((?:19|20)\d{2})\s*[-–]\s*((?:19|20)\d{2})",
    re.IGNORECASE,
)


def _find_copyright_year(html: str) -> int | None:
    """Newest year mentioned in a copyright context."""
    years = [int(y) for y in _COPYRIGHT_RE.findall(html)]
    for start, end in _YEAR_RANGE_RE.findall(html):
        years.extend([int(start), int(end)])
    current = datetime.now().year
    years = [y for y in years if 1990 <= y <= current + 1]
    return max(years) if years else None


def _detect_table_layout(html_lower: str) -> bool:
    """Heuristic for pre-CSS table-based page layout: lots of tables and
    comparatively few divs."""
    tables = html_lower.count("<table")
    divs = html_lower.count("<div")
    return tables >= 4 and divs < tables * 2


def _detect_legacy_doctype(html: str) -> str | None:
    head = html[:300].lower()
    if "<!doctype html>" in head:
        return None  # HTML5
    if "html 4.0" in head or "xhtml 1" in head:
        return "HTML4/XHTML doctype (pre-2010)"
    if "<!doctype" not in head:
        return "missing doctype"
    return None


def analyze_html(html: str) -> EraSignals:
    """Classify a page's design era from its rendered HTML."""
    result = EraSignals()
    if not html:
        return result
    lower = html.lower()

    for needle, label in _LEGACY_MARKERS:
        if needle in lower and label not in result.legacy_signals:
            result.legacy_signals.append(label)
    if _detect_table_layout(lower):
        result.legacy_signals.append("table-based page layout (pre-CSS era)")
    doctype_issue = _detect_legacy_doctype(html)
    if doctype_issue:
        result.legacy_signals.append(doctype_issue)
    if '<meta name="viewport"' not in lower and "<meta name='viewport'" not in lower:
        result.legacy_signals.append("no mobile viewport (pre-smartphone design)")

    for needle, label in _MODERN_MARKERS:
        if needle in lower and label not in result.modern_signals:
            result.modern_signals.append(label)

    result.copyright_year = _find_copyright_year(html)

    # --- verdict ----------------------------------------------------------
    legacy_n = len(result.legacy_signals)
    modern_n = len(result.modern_signals)
    year = result.copyright_year

    hard_signals = {
        "frameset layout (pre-2005 technique)",
        "Adobe Flash content (dead since 2020)",
        "built with Microsoft FrontPage",
        "built with Macromedia Dreamweaver",
    }
    has_hard = any(s in hard_signals for s in result.legacy_signals)

    result.is_outdated = (
        has_hard
        or (legacy_n >= 3 and modern_n == 0)
        or (year is not None and year <= 2012)
        or (year is not None and year <= STALE_YEAR_CUTOFF and legacy_n >= 2 and modern_n <= 1)
    )
    return result
