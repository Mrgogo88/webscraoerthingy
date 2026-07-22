"""Tests for website discovery/verification (offline; HTTP is mocked)."""

from unittest.mock import MagicMock, patch

import requests

from scraper import website_finder as wf


# --- social-only detection --------------------------------------------------
def test_social_urls_detected():
    assert wf.is_social_url("https://www.facebook.com/acme")
    assert wf.is_social_url("https://m.facebook.com/acme")
    assert wf.is_social_url("https://instagram.com/acme")
    assert wf.is_social_url("https://linktr.ee/acme")
    assert wf.is_social_url("https://acme-roofing.business.site")


def test_real_sites_not_social():
    assert not wf.is_social_url("https://acmeroofing.com")
    assert not wf.is_social_url("https://facebookmarketingagency.com")  # not facebook.com
    assert not wf.is_social_url(None)


# --- domain/name matching ---------------------------------------------------
def test_distinctive_token_matches():
    assert wf._match_score("https://delicatedental.com", "Delicate Dental") >= 1.0
    assert wf._match_score("https://triumphroofing.net", "Triumph Roofing and Construction") >= 1.0


def test_generic_words_do_not_match():
    # "dental" is generic — a membership site must not match a dentist's name
    assert wf._match_score("https://mydentalmembership.com", "Delicate Dental") < 1.0
    assert wf._match_score("https://bestroofingdeals.com", "Triumph Roofing") < 1.0


def test_all_generic_name_uses_fuzzy_full_match():
    assert wf._match_score("https://dallasdentalcare.com", "Dallas Dental Care") >= 1.0


# --- reachability probe -----------------------------------------------------
def _mock_response(code: int, url: str = "https://x.com"):
    resp = MagicMock()
    resp.status_code = code
    resp.url = url
    return resp


def test_status_live():
    with patch.object(wf.requests, "get", return_value=_mock_response(200)):
        assert wf.check_website_status("https://x.com").status == "live"


def test_status_dead_on_404():
    with patch.object(wf.requests, "get", return_value=_mock_response(404)):
        st = wf.check_website_status("https://x.com")
        assert st.status == "dead"
        assert st.http_code == 404


def test_status_blocked_on_403():
    with patch.object(wf.requests, "get", return_value=_mock_response(403)):
        assert wf.check_website_status("https://x.com").status == "blocked"


def test_status_dead_on_connection_error():
    with patch.object(wf.requests, "get", side_effect=requests.exceptions.ConnectionError("dns")):
        assert wf.check_website_status("https://x.com").status == "dead"


def test_status_dead_on_timeout():
    with patch.object(wf.requests, "get", side_effect=requests.exceptions.Timeout()):
        assert wf.check_website_status("https://x.com").status == "dead"


# --- DDG result parsing -----------------------------------------------------
DDG_SAMPLE = """
<html><body>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.yelp.com%2Fbiz%2Facme&rut=x">Yelp</a>
<a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Facmeroofing.com%2F&rut=y">Acme</a>
<a class="result__a" href="https://direct-link.com/page">Direct</a>
</body></html>
"""


def test_extract_result_urls_unwraps_redirects():
    urls = wf._extract_result_urls(DDG_SAMPLE)
    assert urls == [
        "https://www.yelp.com/biz/acme",
        "https://acmeroofing.com/",
        "https://direct-link.com/page",
    ]


def test_find_website_skips_directories_and_matches_name():
    resp = MagicMock()
    resp.text = DDG_SAMPLE
    resp.raise_for_status.return_value = None
    with patch.object(wf.requests, "post", return_value=resp), patch.object(wf.time, "sleep"):
        assert wf.find_website("Acme Roofing", "Plano", "TX") == "https://acmeroofing.com/"


def test_find_website_raises_on_network_error():
    """Outages must raise, not return None — None means 'verified absent'."""
    import pytest

    with patch.object(wf.requests, "post", side_effect=requests.exceptions.ConnectionError()), \
         patch.object(wf.time, "sleep"):
        with pytest.raises(wf.DiscoveryUnavailable):
            wf.find_website("Acme Roofing")


def test_find_website_raises_on_empty_result_page():
    """A blank DDG page is a rate-limit challenge, not 'no results'."""
    import pytest

    resp = MagicMock()
    resp.text = "<html><body>rate limited</body></html>"
    resp.raise_for_status.return_value = None
    with patch.object(wf.requests, "post", return_value=resp), patch.object(wf.time, "sleep"):
        with pytest.raises(wf.DiscoveryUnavailable):
            wf.find_website("Acme Roofing")


def test_find_website_rejects_unrelated_domains():
    resp = MagicMock()
    # Only a directory and an unrelated business in results; page-content
    # check is mocked to fail for the unrelated candidate.
    resp.text = """
    <a class="result__a" href="https://www.yelp.com/biz/acme">Yelp</a>
    <a class="result__a" href="https://totallydifferentbiz.com/">Other</a>
    """
    resp.raise_for_status.return_value = None
    with patch.object(wf.requests, "post", return_value=resp), \
         patch.object(wf, "_page_mentions_name", return_value=False), \
         patch.object(wf.time, "sleep"):
        assert wf.find_website("Acme Roofing") is None


# --- migration --------------------------------------------------------------
def test_migration_adds_columns(tmp_path, monkeypatch):
    """An old-schema DB gains the new columns on init_db()."""
    import sqlite3

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    conn.execute(
        "CREATE TABLE businesses (id INTEGER PRIMARY KEY, osm_id VARCHAR(64), "
        "name VARCHAR(255) NOT NULL, category VARCHAR(120), address VARCHAR(500), "
        "city VARCHAR(120), state VARCHAR(60), zip_code VARCHAR(20), phone VARCHAR(60), "
        "website VARCHAR(500), latitude FLOAT, longitude FLOAT, created_at DATETIME)"
    )
    conn.commit()
    conn.close()

    import config
    import database.database as db

    monkeypatch.setattr(config, "DATABASE_URL", f"sqlite:///{db_path}")
    monkeypatch.setattr(db, "_engine", None)
    monkeypatch.setattr(db, "_SessionFactory", None)

    db.init_db()

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(businesses)")}
    conn.close()
    assert "website_source" in cols
    assert "website_status" in cols
