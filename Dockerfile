# LeadFinder — Streamlit dashboard + Playwright crawler + Lighthouse CLI.
# Vercel-style serverless hosts can't run this (persistent server, headless
# Chromium, Node CLI shell-out, local SQLite file). Use a container host
# instead: Render, Railway, Fly.io, or any Docker-capable VPS.
FROM python:3.12-slim

# Node.js (Lighthouse CLI) + apt so Playwright can pull Chromium's OS deps.
RUN apt-get update && apt-get install -y --no-install-recommends \
        curl ca-certificates gnupg \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install deps first so these layers cache across code changes.
COPY requirements.txt package.json package-lock.json* ./
RUN pip install --no-cache-dir -r requirements.txt \
    && npm install \
    && python -m playwright install --with-deps chromium

COPY . .

# Data dir is where the SQLite DB, screenshots, and log file live — mount a
# persistent volume/disk here on Render/Railway or the leads DB resets on
# every restart.
RUN mkdir -p /data/screenshots
ENV DATABASE_PATH=/data/leads.db \
    SCREENSHOT_DIR=/data/screenshots \
    LOG_FILE=/data/leadfinder.log \
    PORT=8501

EXPOSE 8501

CMD ["sh", "-c", "streamlit run dashboard/streamlit_app.py --server.port=${PORT} --server.address=0.0.0.0 --server.headless=true"]
