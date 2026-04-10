"""
TikTok FYP Scraper — Telegram Bot Controller
=============================================
Control your TikTok scraper entirely from Telegram.
Deployed on Railway.
"""

import os
import csv
import re
import time
import asyncio
import logging
import threading
from io import StringIO
from datetime import datetime
from collections import defaultdict

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ─── ENV CONFIG ───────────────────────────────────────────────────────────────
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
ALLOWED_USERS_RAW = os.environ.get("ALLOWED_USERS", "")  # comma-separated telegram user IDs
ALLOWED_USERS = set(
    int(x.strip()) for x in ALLOWED_USERS_RAW.split(",") if x.strip()
)
# ──────────────────────────────────────────────────────────────────────────────

# Per-user scrape state
user_state: dict[int, dict] = defaultdict(lambda: {
    "running": False,
    "target": 50,
    "pause": 2.5,
    "results": [],
    "thread": None,
    "stop_event": None,
})

TIKTOK_VIDEO_PATTERN = re.compile(
    r"https://www\.tiktok\.com/@[\w.]+/video/\d+"
)

# ─── AUTH HELPER ──────────────────────────────────────────────────────────────
def is_allowed(user_id: int) -> bool:
    if not ALLOWED_USERS:
        return True  # open to all if no whitelist set
    return user_id in ALLOWED_USERS

def auth_required(func):
    async def wrapper(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
        uid = update.effective_user.id
        if not is_allowed(uid):
            await update.effective_message.reply_text("⛔ You are not authorized to use this bot.")
            return
        return await func(update, ctx)
    wrapper.__name__ = func.__name__
    return wrapper

# ─── SCRAPER (runs in background thread) ──────────────────────────────────────
def run_scraper(user_id: int, target: int, pause: float,
                stop_event: threading.Event,
                on_progress, on_done, on_error):
    try:
        from playwright.sync_api import sync_playwright

        collected = []
        seen = set()

        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ]
            )
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1280, "height": 900},
            )

            page = context.new_page()
            page.goto("https://www.tiktok.com/foryou",
                      wait_until="networkidle", timeout=30_000)

            # Dismiss banners
            for selector in [
                "button:has-text('Accept all')",
                "button:has-text('I am 18+')",
                "[data-e2e='cookie-banner-accept']",
            ]:
                try:
                    page.click(selector, timeout=2000)
                    time.sleep(0.5)
                except Exception:
                    pass

            scroll_attempts = 0
            max_attempts = target * 4

            while len(collected) < target and scroll_attempts < max_attempts:
                if stop_event.is_set():
                    break

                links = page.eval_on_selector_all(
                    "a[href*='/video/']",
                    "els => els.map(e => e.href)"
                )
                new_links = {
                    l.split("?")[0] for l in links
                    if TIKTOK_VIDEO_PATTERN.match(l.split("?")[0])
                } - seen

                for url in new_links:
                    if len(collected) >= target or stop_event.is_set():
                        break
                    seen.add(url)
                    entry = {
                        "index": len(collected) + 1,
                        "url": url,
                        "scraped_at": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
                    }
                    collected.append(entry)
                    # Progress update every 10 items
                    if len(collected) % 10 == 0 or len(collected) == target:
                        on_progress(len(collected), target)

                page.evaluate("window.scrollBy(0, window.innerHeight)")
                time.sleep(pause)
                scroll_attempts += 1

            browser.close()

        user_state[user_id]["results"] = collected
        on_done(collected)

    except Exception as e:
        logger.exception("Scraper error")
        on_error(str(e))


# ─── BOT COMMANDS ─────────────────────────────────────────────────────────────

@auth_required
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.first_name
    text = (
        f"👋 Welcome, *{uid}*!\n\n"
        "🎵 *TikTok FYP Scraper Bot*\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        "Use the commands below to control the scraper:\n\n"
        "▶️ /scrape — Start scraping\n"
        "⏹ /stop — Stop current scrape\n"
        "⚙️ /settings — View & change settings\n"
        "📥 /download — Download last results\n"
        "📊 /status — Check scraper status\n"
        "❓ /help — Show this message\n"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


@auth_required
async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await cmd_start(update, ctx)


@auth_required
async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    if state["running"]:
        count = len(state["results"])
        target = state["target"]
        pct = int(count / target * 100) if target else 0
        bar = "█" * (pct // 10) + "░" * (10 - pct // 10)
        msg = (
            f"🟢 *Scraper is running*\n\n"
            f"Progress: `{bar}` {pct}%\n"
            f"Collected: {count} / {target} videos\n\n"
            f"Use /stop to halt."
        )
    else:
        count = len(state["results"])
        msg = (
            f"⚪ *Scraper is idle*\n\n"
            f"Last run collected: {count} videos\n"
            f"Use /scrape to start a new run."
        )
    await update.message.reply_text(msg, parse_mode="Markdown")


@auth_required
async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    keyboard = [
        [
            InlineKeyboardButton("🎯 Target: 25",  callback_data="set_target_25"),
            InlineKeyboardButton("🎯 Target: 50",  callback_data="set_target_50"),
            InlineKeyboardButton("🎯 Target: 100", callback_data="set_target_100"),
        ],
        [
            InlineKeyboardButton("⏱ Pause: 1.5s", callback_data="set_pause_1.5"),
            InlineKeyboardButton("⏱ Pause: 2.5s", callback_data="set_pause_2.5"),
            InlineKeyboardButton("⏱ Pause: 4s",   callback_data="set_pause_4"),
        ],
        [
            InlineKeyboardButton("📄 Format: CSV",  callback_data="set_fmt_csv"),
            InlineKeyboardButton("📄 Format: TXT",  callback_data="set_fmt_txt"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    msg = (
        f"⚙️ *Settings*\n\n"
        f"• Target videos: `{state['target']}`\n"
        f"• Scroll pause: `{state['pause']}s`\n"
        f"• Output format: `{state.get('fmt', 'csv').upper()}`\n\n"
        f"Tap a button to change:"
    )
    await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=reply_markup)


@auth_required
async def cmd_scrape(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]

    if state["running"]:
        await update.message.reply_text(
            "⚠️ Scraper is already running! Use /stop to halt it first."
        )
        return

    target = state["target"]
    pause = state["pause"]

    state["running"] = True
    state["results"] = []
    stop_event = threading.Event()
    state["stop_event"] = stop_event

    await update.message.reply_text(
        f"🚀 *Scraper started!*\n\n"
        f"🎯 Target: {target} videos\n"
        f"⏱ Scroll pause: {pause}s\n\n"
        f"I'll update you every 10 videos. Use /stop to cancel.",
        parse_mode="Markdown"
    )

    loop = asyncio.get_event_loop()

    def on_progress(count, total):
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"📊 Progress: *{count}/{total}* videos collected...",
                parse_mode="Markdown",
            ),
            loop,
        )

    def on_done(results):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=(
                    f"✅ *Scrape complete!*\n\n"
                    f"Collected *{len(results)}* video links.\n"
                    f"Use /download to get the file."
                ),
                parse_mode="Markdown",
            ),
            loop,
        )

    def on_error(err):
        state["running"] = False
        asyncio.run_coroutine_threadsafe(
            ctx.bot.send_message(
                chat_id=uid,
                text=f"❌ *Scraper error:*\n`{err}`",
                parse_mode="Markdown",
            ),
            loop,
        )

    t = threading.Thread(
        target=run_scraper,
        args=(uid, target, pause, stop_event, on_progress, on_done, on_error),
        daemon=True,
    )
    state["thread"] = t
    t.start()


@auth_required
async def cmd_stop(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]

    if not state["running"]:
        await update.message.reply_text("ℹ️ No scraper is currently running.")
        return

    if state["stop_event"]:
        state["stop_event"].set()

    state["running"] = False
    count = len(state["results"])
    await update.message.reply_text(
        f"⏹ *Scraper stopped.*\n\n"
        f"Collected {count} videos so far.\n"
        f"Use /download to grab the partial results.",
        parse_mode="Markdown"
    )


@auth_required
async def cmd_download(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = user_state[uid]
    results = state["results"]

    if not results:
        await update.message.reply_text(
            "📭 No results yet. Run /scrape first!"
        )
        return

    fmt = state.get("fmt", "csv")

    if fmt == "csv":
        buf = StringIO()
        writer = csv.DictWriter(buf, fieldnames=["index", "url", "scraped_at"])
        writer.writeheader()
        writer.writerows(results)
        buf.seek(0)
        filename = f"tiktok_fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.csv"
        await update.message.reply_document(
            document=buf.getvalue().encode("utf-8"),
            filename=filename,
            caption=f"🎵 {len(results)} TikTok video links — CSV format",
        )
    else:
        content = "\n".join(r["url"] for r in results)
        filename = f"tiktok_fyp_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}.txt"
        await update.message.reply_document(
            document=content.encode("utf-8"),
            filename=filename,
            caption=f"🎵 {len(results)} TikTok video links — TXT format",
        )


# ─── INLINE KEYBOARD CALLBACKS ────────────────────────────────────────────────
async def button_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if not is_allowed(uid):
        return

    data = query.data
    state = user_state[uid]

    if data.startswith("set_target_"):
        val = int(data.split("_")[-1])
        state["target"] = val
        await query.edit_message_text(f"✅ Target set to *{val}* videos.", parse_mode="Markdown")

    elif data.startswith("set_pause_"):
        val = float(data.split("_")[-1])
        state["pause"] = val
        await query.edit_message_text(f"✅ Scroll pause set to *{val}s*.", parse_mode="Markdown")

    elif data.startswith("set_fmt_"):
        val = data.split("_")[-1]
        state["fmt"] = val
        await query.edit_message_text(f"✅ Output format set to *{val.upper()}*.", parse_mode="Markdown")


# ─── UNKNOWN COMMAND ──────────────────────────────────────────────────────────
async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "❓ Unknown command. Use /help to see available commands."
    )


# ─── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("help",     cmd_help))
    app.add_handler(CommandHandler("scrape",   cmd_scrape))
    app.add_handler(CommandHandler("stop",     cmd_stop))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("settings", cmd_settings))
    app.add_handler(CommandHandler("download", cmd_download))
    app.add_handler(CallbackQueryHandler(button_handler))
    app.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Bot is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
