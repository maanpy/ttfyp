# ── Base ──────────────────────────────────────────────────────────────────────
FROM python:3.11-slim

# Playwright / Chromium system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    wget curl gnupg ca-certificates \
    libnss3 libatk1.0-0 libatk-bridge2.0-0 libcups2 \
    libdrm2 libxkbcommon0 libxcomposite1 libxdamage1 \
    libxfixes3 libxrandr2 libgbm1 libasound2 \
    libpango-1.0-0 libcairo2 libatspi2.0-0 \
    fonts-liberation libappindicator3-1 \
    && rm -rf /var/lib/apt/lists/*

# ── App ───────────────────────────────────────────────────────────────────────
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install Playwright's bundled Chromium
RUN playwright install chromium --with-deps

COPY . .

# ── Run ───────────────────────────────────────────────────────────────────────
CMD ["python", "bot.py"]
