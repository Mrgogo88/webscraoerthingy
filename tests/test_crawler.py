"""Tests for the crawler's HTML extraction logic (offline)."""

from scraper.crawler import CrawlResult, _extract_from_html

SAMPLE_HTML = """
<html>
<head>
  <title>  Ace Roofing - Plano TX  </title>
  <meta name="description" content="Best roofers in Plano.">
  <meta name="viewport" content="width=device-width, initial-scale=1">
</head>
<body>
  <img src="/a.png"><img src="/b.png"><img src="/c.png">
  <a href="/about">About</a>
  <a href="/contact-us">Contact</a>
  <a href="https://www.facebook.com/aceroofing">FB</a>
  <a href="https://www.google.com/maps">Map</a>
  <a href="mailto:info@aceroofing.com?subject=hi">Email us</a>
  <a href="#top">Top</a>
  <a href="javascript:void(0)">JS</a>
  <p>Reach us at SALES@aceroofing.com or call.</p>
  <p>Not an email: logo@2x.png</p>
</body>
</html>
"""


def _crawl(html: str, url: str = "https://aceroofing.com/") -> CrawlResult:
    result = CrawlResult(url=url)
    _extract_from_html(result, html, url)
    return result


def test_title_and_meta():
    r = _crawl(SAMPLE_HTML)
    assert r.title == "Ace Roofing - Plano TX"
    assert r.meta_description == "Best roofers in Plano."


def test_mobile_viewport_detected():
    assert _crawl(SAMPLE_HTML).mobile_friendly is True
    assert _crawl("<html><head></head><body></body></html>").mobile_friendly is False


def test_link_classification():
    r = _crawl(SAMPLE_HTML)
    assert r.internal_links == 2  # /about, /contact-us
    assert r.external_links == 2  # facebook, google maps
    assert r.social_links == ["https://www.facebook.com/aceroofing"]


def test_contact_page_detection():
    assert _crawl(SAMPLE_HTML).contact_page_found is True
    assert _crawl('<a href="/services">x</a>').contact_page_found is False


def test_emails_deduped_and_junk_filtered():
    r = _crawl(SAMPLE_HTML)
    assert r.emails_found == ["info@aceroofing.com", "sales@aceroofing.com"]


def test_image_count():
    assert _crawl(SAMPLE_HTML).image_count == 3


def test_www_treated_as_internal():
    html = '<a href="https://www.aceroofing.com/services">S</a>'
    r = _crawl(html, url="https://aceroofing.com/")
    assert r.internal_links == 1
    assert r.external_links == 0
