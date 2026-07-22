"""Lighthouse CLI integration.

Runs `lighthouse` (installed via npm) headlessly against a URL and extracts
the four category scores. Uses Playwright's bundled Chromium so no separate
Chrome installation is required.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import config

logger = config.setup_logging("leadfinder.lighthouse")


@dataclass
class LighthouseScores:
    performance_score: int | None = None
    seo_score: int | None = None
    accessibility_score: int | None = None
    best_practices_score: int | None = None
    error: str | None = None


@lru_cache(maxsize=1)
def _lighthouse_bin() -> str | None:
    """Locate the lighthouse executable (local npm install or global)."""
    local = config.BASE_DIR / "node_modules" / ".bin" / "lighthouse"
    if local.exists():
        return str(local)
    return shutil.which("lighthouse")


@lru_cache(maxsize=1)
def _chrome_path() -> str | None:
    """Locate a Chrome/Chromium binary, preferring Playwright's download."""
    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            path = p.chromium.executable_path
        if Path(path).exists():
            return path
    except Exception as exc:  # noqa: BLE001 - fall through to system chrome
        logger.warning("Could not resolve Playwright Chromium: %s", exc)
    for name in ("google-chrome", "chromium", "chromium-browser"):
        found = shutil.which(name)
        if found:
            return found
    return None


def is_available() -> bool:
    """True if both the Lighthouse CLI and a Chrome binary are present."""
    return _lighthouse_bin() is not None and _chrome_path() is not None


def _score_to_int(category: dict | None) -> int | None:
    """Lighthouse reports scores as 0-1 floats (or null on failure)."""
    if not category or category.get("score") is None:
        return None
    return round(category["score"] * 100)


def run_lighthouse(url: str) -> LighthouseScores:
    """Audit one URL. Never raises; failures are recorded in .error."""
    result = LighthouseScores()

    bin_path = _lighthouse_bin()
    chrome = _chrome_path()
    if not bin_path or not chrome:
        result.error = "Lighthouse CLI or Chrome not installed"
        logger.error(result.error)
        return result

    with tempfile.TemporaryDirectory(prefix="lighthouse_") as tmp:
        out_path = Path(tmp) / "report.json"
        cmd = [
            bin_path,
            url,
            "--output=json",
            f"--output-path={out_path}",
            "--only-categories=performance,seo,accessibility,best-practices",
            '--chrome-flags=--headless=new --no-sandbox --disable-gpu --disable-dev-shm-usage',
            "--quiet",
            "--max-wait-for-load=45000",
        ]
        env = {**os.environ, "CHROME_PATH": chrome}
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=config.LIGHTHOUSE_TIMEOUT_SECONDS,
                env=env,
            )
        except subprocess.TimeoutExpired:
            result.error = f"Lighthouse timed out after {config.LIGHTHOUSE_TIMEOUT_SECONDS}s"
            logger.warning("%s: %s", url, result.error)
            return result
        except OSError as exc:
            result.error = f"Failed to launch Lighthouse: {exc}"
            logger.error(result.error)
            return result

        if not out_path.exists():
            stderr_tail = (proc.stderr or "").strip().splitlines()[-1:] or ["no output"]
            result.error = f"Lighthouse failed (exit {proc.returncode}): {stderr_tail[0][:300]}"
            logger.warning("%s: %s", url, result.error)
            return result

        try:
            report = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            result.error = f"Could not parse Lighthouse report: {exc}"
            logger.warning("%s: %s", url, result.error)
            return result

    # A runtimeError in the report means the page never loaded properly.
    runtime_error = report.get("runtimeError")
    if runtime_error and runtime_error.get("code") not in (None, "NO_ERROR"):
        result.error = f"Lighthouse runtime error: {runtime_error.get('message', '')[:300]}"
        logger.warning("%s: %s", url, result.error)
        return result

    categories = report.get("categories", {})
    result.performance_score = _score_to_int(categories.get("performance"))
    result.seo_score = _score_to_int(categories.get("seo"))
    result.accessibility_score = _score_to_int(categories.get("accessibility"))
    result.best_practices_score = _score_to_int(categories.get("best-practices"))

    logger.info(
        "Lighthouse %s: perf=%s seo=%s a11y=%s bp=%s",
        url, result.performance_score, result.seo_score,
        result.accessibility_score, result.best_practices_score,
    )
    return result
