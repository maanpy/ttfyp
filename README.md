# 🎵 TikTok FYP Scraper — Telegram Bot

Control your TikTok For You Page scraper entirely from Telegram, deployed on Railway.

---

## ✨ Features

| Command | Description |
|---|---|
| `/scrape` | Start scraping the TikTok FYP |
| `/stop` | Stop the current scrape |
| `/status` | Check progress |
| `/settings` | Change target count, scroll speed, output format |
| `/download` | Download results as CSV or TXT |

---

## 🚀 Deployment Guide

### Step 1 — Create your Telegram Bot

1. Open Telegram and message **@BotFather**
2. Send `/newbot` and follow the prompts
3. Copy your **Bot Token** (looks like `123456:ABC-DEF...`)

### Step 2 — Get your Telegram User ID

1. Message **@userinfobot** on Telegram
2. It will reply with your numeric User ID (e.g. `987654321`)

### Step 3 — Push to GitHub

```bash
git init
git add .
git commit -m "Initial commit"
git remote add origin https://github.com/YOUR_USERNAME/tiktok-fyp-bot.git
git push -u origin main
```

### Step 4 — Deploy on Railway

1. Go to [railway.app](https://railway.app) and create a new project
2. Choose **"Deploy from GitHub repo"** → select your repo
3. Railway will auto-detect the Dockerfile

### Step 5 — Set Environment Variables on Railway

In your Railway project → **Variables** tab, add:

| Variable | Value | Required |
|---|---|---|
| `TELEGRAM_TOKEN` | Your bot token from BotFather | ✅ |
| `ALLOWED_USERS` | Your Telegram user ID(s), comma-separated | Optional |
| `TIKTOK_COOKIES_JSON` | Full JSON cookie array from browser export | Optional* |
| `TIKTOK_SESSIONID` | Just the `sessionid` cookie value | Optional* |

*At least one cookie option should be set for the bot to work.

#### 🍪 Cookie Setup Options

**Option A: Full JSON (Recommended)**
1. Export your TikTok cookies as JSON from Chrome/Firefox (see /cookiehelp in bot)
2. In Railway Variables, add:
   ```
   TIKTOK_COOKIES_JSON=[{"name":"sessionid","value":"..."},{"name":"sid_tt","value":"..."}]
   ```

**Option B: Just SessionID**
1. Get your TikTok sessionid from browser DevTools
2. In Railway Variables, add:
   ```
   TIKTOK_SESSIONID=your_sessionid_value_here
   ```

**Option C: Runtime (via Telegram)**
- Use `/setcookies` in Telegram to upload cookies without modifying Railway variables

Example Railway variables:
```
TELEGRAM_TOKEN=123456:ABC-DEFxxxxx
ALLOWED_USERS=987654321
TIKTOK_SESSIONID=xxxxxxx_your_sessionid_xxxxxxx
```

### Step 6 — Deploy!

Railway will build and deploy automatically. Once live, open Telegram and send `/start` to your bot.

---

## ⚙️ Local Development

```bash
# Install dependencies
pip install -r requirements.txt
playwright install chromium

# Set env vars (example with Railway Cookie JSON)
export TELEGRAM_TOKEN=your_token_here
export ALLOWED_USERS=your_user_id
export TIKTOK_COOKIES_JSON='[{"name":"sessionid","value":"your_value_here"}]'

# Or just use sessionid
export TIKTOK_SESSIONID=your_sessionid_here

# Run
python bot.py
```

---

## 📝 Cookie Refresh Guide

TikTok cookies expire after ~60 days. To refresh:

1. Go to [tiktok.com](https://tiktok.com) and log in
2. Export cookies again (see /cookiehelp in bot for step-by-step)
3. **Option A**: Send to bot via `/setcookies` (persists until next restart)
4. **Option B**: Update Railway Variables with new cookie value and redeploy

The bot will auto-load cookies from Railway variables on startup.

---

## ⚠️ Important Notes

- **TikTok Login Wall**: TikTok may show a login page instead of the FYP. If scraping returns 0 results, TikTok's anti-bot detection blocked the request. This is expected behavior — TikTok actively fights scrapers.
- **Terms of Service**: Scraping TikTok violates their ToS. Use for personal/research purposes only.
- **Railway Plan**: The free tier should work for testing. For continuous use, upgrade to a paid plan to avoid sleep/shutdown.
