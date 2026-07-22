"""Tests for design-era detection, phone extraction, and reverse geocoding."""

from unittest.mock import patch

from scraper.crawler import CrawlResult, _extract_from_html
from scraper.modernity import analyze_html

OLD_SITE = """<html>
<head><title>Smith Roofing</title></head>
<body bgcolor="#FFFFFF">
<table width="760" cellpadding="0" cellspacing="0"><tr><td>
<font face="Arial" size="2">Welcome to our website!</font>
<table><tr><td>menu</td></tr></table>
<table><tr><td>content</td></tr></table>
<table><tr><td>sidebar</td></tr></table>
</td></tr></table>
<p>&copy; 2008 Smith Roofing. All rights reserved.</p>
</body></html>"""

MODERN_SITE = """<!doctype html>
<html>
<head>
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Fresh Dental</title>
</head>
<body>
<nav>menu</nav>
<article>content <img srcset="a.webp 1x" src="a.webp"></article>
<footer>© 2024 Fresh Dental</footer>
<script src="/wp-content/themes/fresh/app.js"></script>
</body></html>"""

FRONTPAGE_SITE = """<html>
<head><meta name="GENERATOR" content="Microsoft FrontPage 4.0"><title>x</title></head>
<body><p>Copyright 2003-2006</p></body></html>"""


def test_old_site_detected_as_outdated():
    era = analyze_html(OLD_SITE)
    assert era.is_outdated
    assert era.copyright_year == 2008
    assert any("font" in s for s in era.legacy_signals)
    assert any("table-based" in s for s in era.legacy_signals)
    assert any("viewport" in s for s in era.legacy_signals)


def test_modern_site_not_outdated():
    era = analyze_html(MODERN_SITE)
    assert not era.is_outdated
    assert era.copyright_year == 2024
    assert "WordPress theme" in era.modern_signals
    assert "WebP images" in era.modern_signals


def test_frontpage_generator_is_hard_signal():
    era = analyze_html(FRONTPAGE_SITE)
    assert era.is_outdated
    assert era.copyright_year == 2006  # newest year in the range


def test_empty_html():
    era = analyze_html("")
    assert not era.is_outdated
    assert era.copyright_year is None


def test_recent_copyright_alone_is_not_modern_proof():
    """A 2024 copyright with several legacy signals & no modern markers -> outdated."""
    html = OLD_SITE.replace("2008", "2024")
    era = analyze_html(html)
    assert era.copyright_year == 2024
    assert era.is_outdated  # 3+ legacy signals, zero modern markers


# --- phone extraction (crawler) ---------------------------------------------
PHONE_HTML = """<html><head><title>x</title></head><body>
<a href="tel:+12143686792">Call us</a>
<p>Office: (972) 555-0100 or 214.368.6792</p>
<p>Not a phone: 123-45-6789 or 1234567</p>
</body></html>"""


def test_phone_extraction():
    result = CrawlResult(url="https://x.com")
    _extract_from_html(result, PHONE_HTML, "https://x.com")
    assert "+12143686792" in result.phones_found
    assert "(972) 555-0100" in result.phones_found
    assert "(214) 368-6792" in result.phones_found
    assert len(result.phones_found) == 3


def test_crawler_era_integration():
    result = CrawlResult(url="https://x.com")
    _extract_from_html(result, OLD_SITE, "https://x.com")
    assert result.is_outdated
    assert result.copyright_year == 2008
    assert result.legacy_signals


# --- reverse geocoding -------------------------------------------------------
def test_reverse_geocode_formats_address():
    from scraper import osm_search

    mock_payload = {
        "address": {
            "house_number": "8345", "road": "Walnut Hill Lane",
            "city": "Dallas", "state": "Texas", "postcode": "75231",
        }
    }
    with patch.object(osm_search.requests, "get") as mock_get, \
         patch.object(osm_search.time, "sleep"):
        mock_get.return_value.json.return_value = mock_payload
        mock_get.return_value.raise_for_status.return_value = None
        addr = osm_search.reverse_geocode(32.87, -96.75)
    assert addr == "8345 Walnut Hill Lane, Dallas, Texas, 75231"


def test_reverse_geocode_failure_returns_none():
    import requests as req

    from scraper import osm_search

    with patch.object(osm_search.requests, "get", side_effect=req.exceptions.ConnectionError()), \
         patch.object(osm_search.time, "sleep"):
        assert osm_search.reverse_geocode(0, 0) is None
