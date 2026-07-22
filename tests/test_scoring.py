"""Tests for the website quality scoring algorithm."""

from scraper.scoring import classify, compute_score


def test_perfect_site_scores_100():
    r = compute_score(
        https_enabled=True,
        mobile_friendly=True,
        seo_score=100,
        performance_score=100,
        accessibility_score=100,
        best_practices_score=100,
        contact_page_found=True,
        meta_description="Great roofing company",
        social_links_count=3,
    )
    assert r.score == 100
    assert r.priority == "Excellent"
    assert r.problems == []


def test_terrible_site_scores_0():
    r = compute_score(
        https_enabled=False,
        mobile_friendly=False,
        seo_score=10,
        performance_score=5,
        accessibility_score=20,
        best_practices_score=30,
        contact_page_found=False,
        meta_description=None,
        social_links_count=0,
        broken_images=4,
        load_time=15.0,
    )
    assert r.score == 0  # clamped
    assert r.priority == "High Priority Lead"
    assert len(r.problems) >= 5
    assert len(r.recommendations) >= 5


def test_missing_lighthouse_contributes_zero():
    with_lh = compute_score(https_enabled=True, mobile_friendly=True, seo_score=100,
                            performance_score=100, accessibility_score=100)
    without_lh = compute_score(https_enabled=True, mobile_friendly=True)
    assert with_lh.score - without_lh.score == 50  # 20+20+10


def test_penalties_applied():
    base = compute_score(https_enabled=True, mobile_friendly=True)
    slow = compute_score(https_enabled=True, mobile_friendly=True, load_time=12.0)
    broken = compute_score(https_enabled=True, mobile_friendly=True, broken_images=2)
    assert base.score - slow.score == 10
    assert base.score - broken.score == 10


def test_load_time_boundary():
    at_limit = compute_score(https_enabled=True, mobile_friendly=True, load_time=8.0)
    over_limit = compute_score(https_enabled=True, mobile_friendly=True, load_time=8.1)
    assert at_limit.score - over_limit.score == 10


def test_classification_bands():
    assert classify(100) == "Excellent"
    assert classify(80) == "Excellent"
    assert classify(79) == "Average"
    assert classify(60) == "Average"
    assert classify(59) == "Needs Improvement"
    assert classify(40) == "Needs Improvement"
    assert classify(39) == "High Priority Lead"
    assert classify(0) == "High Priority Lead"


def test_score_never_out_of_bounds():
    r = compute_score()  # everything missing/False
    assert 0 <= r.score <= 100
