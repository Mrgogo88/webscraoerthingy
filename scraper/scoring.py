"""Website quality scoring.

Combines crawl signals and Lighthouse scores into a single 0-100 score,
then classifies the business as a redesign lead. Lower scores = better
prospects for website redesign services.

Scoring model (max 100):
    HTTPS enabled            +10   (missing: -20 penalty)
    Mobile viewport tag      +20   (missing: -20 penalty)
    SEO (Lighthouse)         +20 * seo/100
    Performance (Lighthouse) +20 * perf/100
    Accessibility            +10 * a11y/100
    Contact page found       +10
    Modern site indicators   +10   (meta description, social links,
                                    best-practices >= 80; ~3.33 each)
Penalties:
    Broken images            -10
    Very slow load (> 8s)    -10
Result is clamped to 0-100.
"""

from __future__ import annotations

from dataclasses import dataclass

VERY_SLOW_SECONDS = 8.0

# Classification bands (inclusive lower bound)
PRIORITY_BANDS = [
    (80, "Excellent"),
    (60, "Average"),
    (40, "Needs Improvement"),
    (0, "High Priority Lead"),
]


@dataclass
class ScoreBreakdown:
    """Final score plus per-criterion detail for the dashboard."""

    score: int
    priority: str
    details: dict[str, float]
    problems: list[str]
    recommendations: list[str]


def classify(score: int) -> str:
    for threshold, label in PRIORITY_BANDS:
        if score >= threshold:
            return label
    return "High Priority Lead"


def _lh_points(value: int | None, weight: float) -> float:
    """Scale a 0-100 Lighthouse score into its weighted contribution.

    Missing scores contribute 0 (the site couldn't be audited, which is
    itself a bad sign for the lead's web presence).
    """
    if value is None:
        return 0.0
    return weight * (max(0, min(100, value)) / 100)


def compute_score(
    https_enabled: bool | None = None,
    mobile_friendly: bool | None = None,
    seo_score: int | None = None,
    performance_score: int | None = None,
    accessibility_score: int | None = None,
    best_practices_score: int | None = None,
    contact_page_found: bool | None = None,
    meta_description: str | None = None,
    social_links_count: int = 0,
    broken_images: int | None = None,
    load_time: float | None = None,
    is_outdated: bool = False,
    copyright_year: int | None = None,
) -> ScoreBreakdown:
    """Compute the 0-100 website quality score with full breakdown."""
    details: dict[str, float] = {}
    problems: list[str] = []
    recommendations: list[str] = []

    # --- HTTPS ------------------------------------------------------------
    if https_enabled:
        details["https"] = 10
    else:
        details["https"] = -20
        problems.append("Site is not served over HTTPS")
        recommendations.append("Install an SSL certificate and redirect all traffic to HTTPS")

    # --- Mobile -----------------------------------------------------------
    if mobile_friendly:
        details["mobile"] = 20
    else:
        details["mobile"] = -20
        problems.append("No mobile viewport tag — site is likely not mobile-friendly")
        recommendations.append("Rebuild with a responsive, mobile-first layout")

    # --- Lighthouse-driven ------------------------------------------------
    details["seo"] = round(_lh_points(seo_score, 20), 1)
    if seo_score is not None and seo_score < 70:
        problems.append(f"Weak SEO fundamentals (Lighthouse SEO score {seo_score}/100)")
        recommendations.append("Fix meta tags, headings, link text, and crawlability issues")

    details["performance"] = round(_lh_points(performance_score, 20), 1)
    if performance_score is not None and performance_score < 50:
        problems.append(f"Poor page performance (Lighthouse score {performance_score}/100)")
        recommendations.append("Optimize images, reduce scripts, and enable caching/CDN")

    details["accessibility"] = round(_lh_points(accessibility_score, 10), 1)
    if accessibility_score is not None and accessibility_score < 70:
        problems.append(f"Accessibility issues (Lighthouse score {accessibility_score}/100)")
        recommendations.append("Add alt text, fix contrast, and label form controls")

    # --- Contact page -----------------------------------------------------
    if contact_page_found:
        details["contact_page"] = 10
    else:
        details["contact_page"] = 0
        problems.append("No contact page found from the homepage")
        recommendations.append("Add a clear contact page with a form and phone number")

    # --- Modern indicators (up to 10) -------------------------------------
    modern = 0.0
    if meta_description:
        modern += 10 / 3
    else:
        problems.append("Missing meta description")
        recommendations.append("Write a compelling meta description for search results")
    if social_links_count > 0:
        modern += 10 / 3
    else:
        problems.append("No social media links found")
        recommendations.append("Link the site to active social media profiles")
    if best_practices_score is not None and best_practices_score >= 80:
        modern += 10 / 3
    details["modern"] = round(modern, 1)

    # --- Penalties --------------------------------------------------------
    if broken_images and broken_images > 0:
        details["broken_images_penalty"] = -10
        problems.append(f"{broken_images} broken image(s) on the homepage")
        recommendations.append("Fix or remove broken images")
    if load_time is not None and load_time > VERY_SLOW_SECONDS:
        details["slow_load_penalty"] = -10
        problems.append(f"Very slow homepage load ({load_time:.1f}s)")
        recommendations.append("Reduce page weight and use faster hosting")
    if is_outdated:
        details["outdated_penalty"] = -15
        year_note = f" (copyright says {copyright_year})" if copyright_year else ""
        problems.append(f"Website design is badly outdated{year_note} — built with pre-2010 techniques")
        recommendations.append("Full redesign with a modern, mobile-first framework")

    raw = sum(details.values())
    score = int(round(max(0, min(100, raw))))
    return ScoreBreakdown(
        score=score,
        priority=classify(score),
        details=details,
        problems=problems,
        recommendations=recommendations,
    )


def score_from_analysis(analysis) -> ScoreBreakdown:
    """Convenience wrapper: score a WebsiteAnalysis ORM object."""
    return compute_score(
        https_enabled=analysis.https_enabled,
        mobile_friendly=analysis.mobile_friendly,
        seo_score=analysis.seo_score,
        performance_score=analysis.performance_score,
        accessibility_score=analysis.accessibility_score,
        best_practices_score=analysis.best_practices_score,
        contact_page_found=analysis.contact_page_found,
        meta_description=analysis.meta_description,
        social_links_count=len(analysis.social_links_list),
        broken_images=analysis.broken_images,
        load_time=analysis.load_time,
        is_outdated=bool(analysis.is_outdated),
        copyright_year=analysis.copyright_year,
    )
