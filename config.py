"""Central configuration for LeadFinder.

All settings are read from environment variables (optionally loaded from a
.env file) with sensible defaults, so the app runs out of the box.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv

# Project root = directory containing this file
BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")


def _path(env_var: str, default: str) -> Path:
    """Resolve a path setting relative to the project root."""
    raw = os.getenv(env_var, default)
    p = Path(raw)
    return p if p.is_absolute() else BASE_DIR / p


# --- Storage ---------------------------------------------------------------
DATABASE_PATH: Path = _path("DATABASE_PATH", "data/leads.db")
SCREENSHOT_DIR: Path = _path("SCREENSHOT_DIR", "data/screenshots")
DATABASE_URL = f"sqlite:///{DATABASE_PATH}"

# --- OpenStreetMap services ------------------------------------------------
OVERPASS_URL = os.getenv("OVERPASS_URL", "https://overpass-api.de/api/interpreter")
NOMINATIM_URL = os.getenv("NOMINATIM_URL", "https://nominatim.openstreetmap.org/search")
OSM_USER_AGENT = os.getenv("OSM_USER_AGENT", "LeadFinder/1.0 (local business audit tool)")

# --- Google Places (optional) ----------------------------------------------
# When set, scans also pull businesses from Google Maps via the official
# Places API (free monthly quota). Leave empty to use OSM + web search only.
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "").strip()

# --- Crawler ---------------------------------------------------------------
CRAWL_TIMEOUT_SECONDS = int(os.getenv("CRAWL_TIMEOUT_SECONDS", "30"))
CRAWL_CONCURRENCY = int(os.getenv("CRAWL_CONCURRENCY", "3"))

# --- Lighthouse ------------------------------------------------------------
LIGHTHOUSE_TIMEOUT_SECONDS = int(os.getenv("LIGHTHOUSE_TIMEOUT_SECONDS", "120"))

# --- Logging ---------------------------------------------------------------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
LOG_FILE: Path = _path("LOG_FILE", "data/leadfinder.log")


def ensure_dirs() -> None:
    """Create data directories if they do not exist."""
    DATABASE_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)


def setup_logging(name: str = "leadfinder") -> logging.Logger:
    """Configure a logger that writes to both console and the log file."""
    ensure_dirs()
    logger = logging.getLogger(name)
    if logger.handlers:  # already configured
        return logger

    logger.setLevel(getattr(logging, LOG_LEVEL.upper(), logging.INFO))
    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")

    stream = logging.StreamHandler()
    stream.setFormatter(fmt)
    logger.addHandler(stream)

    file_handler = logging.FileHandler(LOG_FILE, encoding="utf-8")
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    return logger
